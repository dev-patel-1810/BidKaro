"""Fill the corpus intelligence tables in the background.

The read path never classifies a whole corpus and never calls a model. It
reads what this service has already stored: a tender's work type (global) and
a company's verdict on it (per company). When a stored row is missing or stale
the read path classifies that page itself with the same functions, so the
lister is always correct, only slower on a cold corpus.

This service exists so the warm path is the normal one:

1. classify every tender that has no current classification (rules only);
2. compute every active company's verdict on the recent corpus;
3. optionally ask the model about the ambiguous residue, one bounded batch at
   a time, and store its answers as model-sourced verdicts.

Each step is idempotent and commits as it goes, so a cancelled job leaves the
tables consistent and the next run continues from what is still stale.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.settings import Settings, get_settings
from app.core.time import utcnow
from app.discovery import classification
from app.discovery.adjudication import adjudicate
from app.discovery.relevance import (
    RULES_VERSION,
    band_score,
    classify,
    company_profile_dict,
    profile_signature,
    rarity_from_titles,
    understanding_terms_from_rows,
)
from app.discovery.work_text import contains, work_text
from app.models.corpus import Tender
from app.models.identity import Company
from app.models.intelligence import CompanyTenderVerdict
from app.repositories.identity import AuditRepository
from app.repositories.intelligence import (
    ClassificationRepository,
    FeedbackRepository,
    ObservationRepository,
    VerdictRepository,
    stable_hash,
    tender_content_hash,
    verdict_input_hash,
)
from app.repositories.understanding import CompanyUnderstandingRepository
from app.services.company_brief import CompanyBriefService, payload_terms
from app.services.relevance import company_profile_terms


class CorpusIntelligenceService:
    def __init__(self, session: Session, settings: Settings | None = None) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self.classifications = ClassificationRepository(session)
        self.verdicts = VerdictRepository(session)
        self.feedback = FeedbackRepository(session)
        self.observations = ObservationRepository(session)

    # -- step 1: company-independent labels -------------------------------

    def classify_pending(self, *, limit: int = 500) -> int:
        tenders = list(
            self.session.scalars(
                select(Tender)
                .where(
                    Tender.deleted_at.is_(None),
                    Tender.merged_into_tender_id.is_(None),
                )
                .order_by(Tender.created_at.desc())
                .limit(limit)
            ).all()
        )
        if not tenders:
            return 0
        existing = self.classifications.get_many([tender.id for tender in tenders])
        written = 0
        for tender in tenders:
            digest = classification.content_hash(tender)
            current = existing.get(tender.id)
            if (
                current is not None
                and current.input_hash == digest
                and current.rules_version == classification.CLASSIFIER_VERSION
            ):
                continue
            label = classification.classify_title(tender.work_title)
            self.classifications.upsert(
                tender.id,
                primary_work_type=label["primary_work_type"],
                work_types=label["work_types"],
                confidence=label["confidence"],
                method="rules",
                rules_version=classification.CLASSIFIER_VERSION,
                input_hash=digest,
                reason=label["reason"],
            )
            written += 1
        self.session.commit()
        return written

    # -- step 2: per-company verdicts -------------------------------------

    def refresh_verdicts(self, *, limit: int = 500) -> int:
        companies = list(
            self.session.scalars(
                select(Company).where(Company.is_active.is_(True))
            ).all()
        )
        if not companies:
            return 0
        tenders = list(
            self.session.scalars(
                select(Tender)
                .where(
                    Tender.deleted_at.is_(None),
                    Tender.merged_into_tender_id.is_(None),
                )
                .order_by(Tender.created_at.desc())
                .limit(limit)
            ).all()
        )
        if not tenders:
            return 0
        titles = [tender.work_title or "" for tender in tenders]
        understanding = CompanyUnderstandingRepository(self.session)
        briefs = CompanyBriefService(self.session)
        written = 0
        for company in companies:
            understanding_terms = understanding_terms_from_rows(
                understanding.confirmed_capabilities(company.id),
                understanding.confirmed_experiences(company.id),
            )
            brief_payload = briefs.current_payload(company.id)
            if brief_payload is None:
                brief_payload = briefs.derive_payload(company)
            brief_terms = payload_terms(brief_payload)
            terms = company_profile_terms(
                company,
                understanding_terms=understanding_terms,
                brief_terms=brief_terms,
            )
            if not terms:
                continue
            rarity = rarity_from_titles(titles, terms)
            negative = self.feedback.negative_work_types(company.id)
            profile_hash = stable_hash(
                profile_signature(
                    company,
                    understanding_terms=understanding_terms,
                    brief_terms=brief_terms,
                )
            )
            feedback_hash = stable_hash(
                [
                    (str(item.tender_id), item.verdict)
                    for item in self.feedback.list_for_company(company.id, limit=500)
                ]
            )
            cached = self.verdicts.get_many(
                company.id, [tender.id for tender in tenders]
            )
            for tender in tenders:
                key = verdict_input_hash(
                    profile_hash=profile_hash,
                    tender_content_hash=tender_content_hash(tender),
                    feedback_hash=feedback_hash,
                    rules_version=RULES_VERSION,
                )
                current = cached.get(tender.id)
                if (
                    current is not None
                    and current.input_hash == key
                    and current.rules_version == RULES_VERSION
                ):
                    continue
                verdict = classify(
                    company,
                    tender,
                    rarity=rarity,
                    negative=negative,
                    understanding_terms=understanding_terms,
                    brief_terms=brief_terms,
                )
                self.verdicts.upsert(
                    company.id,
                    tender.id,
                    bucket=verdict["bucket"],
                    tier=verdict["tier"],
                    score=verdict["score"],
                    matched_terms=verdict["matched_terms"],
                    reasons=verdict["reasons"],
                    source="rules",
                    input_hash=key,
                    rules_version=RULES_VERSION,
                )
                written += 1
        self.session.commit()
        return written

    # -- step 3: adjudicate the ambiguous residue --------------------------

    def adjudicate_possible(
        self, provider, *, limit: int = 200, batch_size: int | None = None,
        max_batches: int | None = None,
    ) -> dict[str, int]:
        """Ask the model about rows whose only evidence is a common word."""

        size = batch_size or self.settings.classification_batch_size
        batches = (
            max_batches
            if max_batches is not None
            else self.settings.classification_max_batches
        )
        if provider is None or not getattr(provider, "configured", False) or batches == 0:
            return {"judged": 0, "hidden": 0, "batches": 0}

        companies = list(
            self.session.scalars(
                select(Company).where(Company.is_active.is_(True))
            ).all()
        )
        judged = 0
        hidden = 0
        demoted = 0
        used = 0
        understanding = CompanyUnderstandingRepository(self.session)
        for company in companies:
            understanding_terms = understanding_terms_from_rows(
                understanding.confirmed_capabilities(company.id),
                understanding.confirmed_experiences(company.id),
            )
            declared = company_profile_terms(
                company, understanding_terms=understanding_terms
            )
            if not declared:
                continue
            rows = list(
                self.session.scalars(
                    select(Tender)
                    .join(
                        CompanyTenderVerdict,
                        CompanyTenderVerdict.tender_id == Tender.id,
                    )
                    .where(
                        CompanyTenderVerdict.company_id == company.id,
                        CompanyTenderVerdict.bucket == "possible",
                        CompanyTenderVerdict.source == "rules",
                    )
                    .order_by(Tender.created_at.desc())
                    .limit(limit)
                ).all()
            )
            if not rows:
                continue
            verdict_by_id = self.verdicts.get_many(
                company.id, [row.id for row in rows]
            )
            profile_hash = stable_hash(
                profile_signature(company, understanding_terms=understanding_terms)
            )
            feedback_hash = stable_hash(
                [
                    (str(item.tender_id), item.verdict)
                    for item in self.feedback.list_for_company(company.id, limit=500)
                ]
            )
            for start in range(0, len(rows), size):
                if used >= batches:
                    break
                chunk = rows[start : start + size]
                items = [
                    {
                        "id": str(tender.id),
                        "title": tender.work_title or "",
                        "matched": list(
                            (verdict_by_id.get(tender.id).matched_terms or [])
                            if verdict_by_id.get(tender.id)
                            else []
                        ),
                    }
                    for tender in chunk
                ]
                answers = adjudicate(
                    provider, declared=declared, items=items
                )
                used += 1
                for tender in chunk:
                    answer = answers.get(str(tender.id), {})
                    relevant = answer.get("relevant", "unknown")
                    if relevant == "unknown":
                        # Shown, and left as a rules verdict so a later model
                        # run, or a better matcher, may look at it again.
                        continue
                    verdict = self.verdicts.get(company.id, tender.id)
                    if verdict is None:
                        continue
                    reasons = list(verdict.reasons or [])
                    matched = list(verdict.matched_terms or [])
                    if relevant == "yes":
                        anchor = str(answer.get("anchor") or "").strip()
                        work = work_text(tender.work_title, tender.work_type)
                        anchored = bool(anchor) and contains(work, anchor)
                        if anchored:
                            tier = 1 if matched else 2
                            verdict.bucket = "strong"
                            verdict.tier = tier
                            verdict.score = band_score(tier, max(1, len(matched)))
                            reasons.append(
                                "The model judged this work relevant and quoted "
                                f'"{anchor}" from the notice, so it moved up the '
                                "fit ladder."
                            )
                        else:
                            # A model opinion without a verbatim anchor is not
                            # an understanding match. Keep the rules verdict and
                            # say why it was not promoted.
                            reasons.append(
                                "The model judged this work relevant, but its "
                                "quoted words are not in the notice; it is not "
                                "promoted."
                            )
                    else:
                        # A model "no" is the bottom of the visible list, never
                        # a silent hide: a human can still disagree, and the
                        # rules that matched are still the rules that matched.
                        tier = 6
                        verdict.bucket = "possible"
                        verdict.tier = tier
                        verdict.score = band_score(tier, max(1, len(matched)))
                        reasons.append(
                            "The model judged this work not relevant, so it is "
                            "at the bottom of the list; it is still shown."
                        )
                        demoted += 1
                    verdict.source = "model"
                    verdict.reasons = reasons
                    verdict.input_hash = verdict_input_hash(
                        profile_hash=profile_hash,
                        tender_content_hash=tender_content_hash(tender),
                        feedback_hash=feedback_hash,
                        rules_version=RULES_VERSION,
                    )
                    verdict.updated_at = utcnow()
                    judged += 1
                    if verdict.bucket == "hidden":
                        hidden += 1
                self.session.commit()
        return {
            "judged": judged,
            "hidden": hidden,
            "demoted": demoted,
            "batches": used,
        }

    # -- step 3b: judge every visible tier ---------------------------------

    def adjudicate_candidates(
        self,
        provider,
        *,
        limit: int = 200,
        batch_size: int | None = None,
        max_batches: int | None = None,
    ) -> dict[str, int]:
        """Ask the model about every candidate, not only the ambiguous residue.

        The model never sees the rules tier. It receives the company brief and
        exact evidence lines from both sides, and its answer is stored on the
        judgment row. The rules verdict stays rules-only; the read path applies
        the evidence-aware adjustment afterwards.

        The pass is bounded by ``batch_size * max_batches`` candidates per
        invocation so a cold corpus cannot turn one job into an unbounded model
        bill. A later run continues from what is still stale because the input
        hash includes the brief signature.
        """

        size = max(batch_size or self.settings.similarity_batch_size, 1)
        batches = (
            max_batches
            if max_batches is not None
            else self.settings.classification_max_batches
        )
        if (
            provider is None
            or not getattr(provider, "configured", False)
            or batches == 0
        ):
            return {"judged": 0, "yes": 0, "no": 0, "unsure": 0, "batches": 0}

        # Imported lazily: the base install must not need the AI stack just to
        # import the discovery package.
        from app.services.similarity import TenderSimilarityService

        companies = list(
            self.session.scalars(
                select(Company).where(Company.is_active.is_(True))
            ).all()
        )
        service = TenderSimilarityService(
            self.session, ai=provider, settings=self.settings
        )
        briefs = CompanyBriefService(self.session)
        judged = yes = no = unsure = used = 0
        ceiling = size * max(int(batches), 1)

        for company in companies:
            brief_payload = briefs.current_payload(company.id)
            if brief_payload is None:
                brief_payload = briefs.derive_payload(company)
            terms = payload_terms(brief_payload)
            if not any(terms.values()):
                continue
            rows = list(
                self.session.scalars(
                    select(Tender)
                    .join(
                        CompanyTenderVerdict,
                        CompanyTenderVerdict.tender_id == Tender.id,
                    )
                    .where(
                        CompanyTenderVerdict.company_id == company.id,
                        Tender.deleted_at.is_(None),
                        Tender.merged_into_tender_id.is_(None),
                        Tender.cycle_state == "active",
                    )
                    .order_by(Tender.created_at.desc())
                    .limit(min(max(int(limit), 1), ceiling))
                ).all()
            )
            if not rows:
                continue
            for start in range(0, len(rows), size):
                if used >= batches:
                    break
                chunk = rows[start : start + size]
                try:
                    result = service.judge_many(
                        company=company, tenders=chunk, force=False
                    )
                except Exception:
                    # One company's missing key or malformed profile must not
                    # stop the other companies. The job result says what ran.
                    continue
                used += 1
                for judgment in result.values():
                    judged += 1
                    answer = str(judgment.get("answer") or "")
                    if answer == "yes":
                        yes += 1
                    elif answer == "no":
                        no += 1
                    else:
                        unsure += 1

        return {
            "judged": judged,
            "yes": yes,
            "no": no,
            "unsure": unsure,
            "batches": used,
        }

    # -- the whole pass ----------------------------------------------------

    def run(self, *, limit: int = 500, provider=None) -> dict[str, Any]:
        classified = self.classify_pending(limit=limit)
        verdicts = self.refresh_verdicts(limit=limit)
        adjudicated = self.adjudicate_candidates(provider)
        return {
            "classified": classified,
            "verdicts": verdicts,
            **adjudicated,
        }

