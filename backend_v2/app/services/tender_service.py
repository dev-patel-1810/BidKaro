from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.core.errors import BadRequest, NotFound
from app.core.settings import Settings, get_settings
from app.core.time import utcnow
from app.models.corpus import Tender
from app.repositories.corpus import (
    DEFAULT_TENDER_ORDER,
    MATCH_ORDER,
    TENDER_ORDERS,
    TENDER_SORTS,
    DocumentRepository,
    RequirementRepository,
    TenderAuxRepository,
    TenderRepository,
    like_pattern,
)
from app.repositories.assessment import CompanyTenderRepository
from app.repositories.understanding import (
    CompanyUnderstandingRepository,
    SimilarityRepository,
)
from app.services.job_events import notify_bidding
from app.repositories.identity import AuditRepository
from app.services import storage
from app.services.corpus_rules import manual_dedupe_key, trim
from app.services.company_brief import CompanyBriefService, payload_terms
from app.services.document_service import DocumentService
from app.services.similarity import adjust_verdict, judgment_payload
from app.services.workflow import WorkflowService


class TenderService:
    def __init__(self, session: Session, settings: Settings | None = None) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self.tenders = TenderRepository(session)
        self.documents = DocumentRepository(session)
        self.requirements = RequirementRepository(session)
        self.aux = TenderAuxRepository(session)
        self.company_tenders = CompanyTenderRepository(session)
        self.audit = AuditRepository(session)
        #: Verdicts computed by the last ``list`` call, keyed by tender id.
        #: The route reads them so the badge it shows is the same one the gate
        #: used; nothing is recomputed a second time.
        self.last_verdicts: dict[uuid.UUID, dict[str, Any]] = {}
        #: Human-confirmed capability/experience phrases, cached for the
        #: request so the card's reasons do not re-query per tender. Raw
        #: about-company terms are deliberately not read here.
        self._understanding_terms_cache: dict[uuid.UUID, list[str]] = {}
        #: The company's own row for each tender in the last ``list`` call, so
        #: the card can say "saved" / "bidding" without a query per row.
        self.last_links: dict[uuid.UUID, Any] = {}
        #: The cached similarity answer shown on the last ``list`` call, keyed
        #: by tender id. ``None`` means "no answer yet", not "not similar".
        self.last_similarity: dict[uuid.UUID, dict[str, Any] | None] = {}
        self.understanding = CompanyUnderstandingRepository(session)
        self.similarity = SimilarityRepository(session)
        self.briefs = CompanyBriefService(session)
        self._capability_names: dict[uuid.UUID, str] | None = None
        self._brief_terms_cache: dict[uuid.UUID, dict[str, list[str]]] = {}
        self._brief_signature_cache: dict[uuid.UUID, str] = {}

    def list(
        self,
        *,
        company=None,
        closing_after: datetime | None = None,
        saved: bool = False,
        bidding: bool = False,
        mine: bool = False,
        include_dismissed: bool = False,
        #: Also return the rows the gate called ``hidden``. The default view is
        #: the gate's answer; this is the escape hatch for a user who thinks it
        #: was wrong, so the rows come back labelled rather than disappearing.
        include_hidden: bool = False,
        #: Closed tenders are hidden by default. A saved/bidding row keeps them
        #: visible because the bid is work in progress; this escape hatch is for
        #: an explicit audit or API consumer.
        include_closed: bool = False,
        query: str | None = None,
        sort: str = DEFAULT_TENDER_ORDER,
        limit: int = 10,
        offset: int = 0,
    ) -> tuple[list[Tender], int]:
        """One page of the visible corpus, plus the number of visible rows.

        The profile gate is per-company and read-time, so it cannot be a SQL
        filter and a SQL page cannot be sliced before it runs. Two things keep
        that honest and cheap: the query narrows to rows whose work words
        mention something the company declared (a superset of the gate), and
        the gate then classifies what is left as strong, possible or hidden.
        Only strong and possible are listed; hidden rows are counted out of
        ``total`` so the page and its count always agree. Asking for
        ``include_hidden`` widens both the query and the count, so the page is
        the whole corpus again.
        """

        if sort not in TENDER_SORTS:
            raise BadRequest("Unknown tender sort order.")

        # A closed tender is not a discovery candidate. Saved and bidding rows
        # stay visible because the bid is work in progress; an explicit
        # ``include_closed`` is the only other way back in.
        if closing_after is None and not (
            saved or bidding or mine or include_closed
        ):
            closing_after = utcnow()

        if company is None:
            rows = self.tenders.list(
                closing_after=closing_after, sort=sort, limit=None
            )
            return list(rows)[offset : offset + limit], len(rows)

        from app.discovery.relevance import (
            RULES_VERSION,
            classify,
            company_profile_dict,
            company_terms,
            prefilter_terms,
            profile_signature,
            rarity_from_titles,
        )
        from app.repositories.intelligence import (
            FeedbackRepository,
            VerdictRepository,
            stable_hash,
            tender_content_hash,
            verdict_input_hash,
        )

        understanding_terms = self.understanding_terms(company)
        brief_terms = self.brief_terms(company)
        profile = company_profile_dict(
            company,
            understanding_terms=understanding_terms,
            brief_terms=brief_terms,
        )
        groups = company_terms(profile)
        terms = (
            groups["core"]
            + groups["adjacent"]
            + groups["support"]
            + groups["generic"]
        )
        # Dedupe: two terms can share a plural, a spelling or a reversed word
        # order, and a repeated LIKE only costs the query plan. The list is a
        # *superset* narrowing, so it must not silently lose a term to a
        # ceiling: a profile with many confirmed phrases can have many terms,
        # and a tender that matches only the last one still has to reach the
        # gate. If the list is too long for a cheap prefilter, load the corpus
        # and let the gate decide instead of dropping the tail.
        patterns = list(
            dict.fromkeys(
                f"%{like_pattern(variant)[1:-1]}%"
                for variant in prefilter_terms(terms)
            )
        )
        prefilter = (
            None
            if include_hidden or len(patterns) > 800
            else (patterns or None)
        )

        rows = self.tenders.list_for_company(
            company.id,
            closing_after=closing_after,
            saved=saved,
            bidding=bidding,
            mine=mine,
            include_dismissed=include_dismissed,
            query=query,
            sort=sort,
            match_patterns=prefilter,
        )

        # Rarity only explains a reason; it never promotes a rung. A small
        # sample is enough for that, and it keeps the homepage from scanning
        # every title in a 2,600-row corpus on each page request.
        titles = self.tenders.recent_titles(limit=400) or [
            tender.work_title or "" for tender, _link in rows
        ]
        rarity = rarity_from_titles(titles, terms)
        feedback_repo = FeedbackRepository(self.session)
        negative = feedback_repo.negative_work_types(company.id)

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
                for item in feedback_repo.list_for_company(company.id, limit=500)
            ]
        )
        cached = VerdictRepository(self.session).get_many(
            company.id, [tender.id for tender, _link in rows]
        )
        # The similarity answer is a later, human-revocable signal. It is read
        # once per page and applied on top of the rules verdict, never written
        # into the verdict cache: a revoked capability must be able to change
        # the answer without a migration of stored scores.
        similarity_rows = self.similarity.get_many(
            company.id, [tender.id for tender, _link in rows]
        )
        similarity_names = self.capability_names(company)

        self.last_verdicts: dict[uuid.UUID, dict[str, Any]] = {}
        self.last_links: dict[uuid.UUID, Any] = {}
        visible: list[Tender] = []
        for tender, link in rows:
            self.last_links[tender.id] = link
            verdict = self._verdict(
                company,
                tender,
                rarity=rarity,
                negative=negative,
                profile_hash=profile_hash,
                feedback_hash=feedback_hash,
                cached=cached.get(tender.id),
                classify=classify,
                rules_version=RULES_VERSION,
                verdict_input_hash=verdict_input_hash,
                understanding_terms=understanding_terms,
                brief_terms=brief_terms,
            )
            judgment = similarity_rows.get(tender.id)
            if judgment is not None:
                verdict = adjust_verdict(verdict, judgment, similarity_names)
            self.last_verdicts[tender.id] = verdict
            self.last_similarity[tender.id] = (
                judgment_payload(judgment, similarity_names)
                if judgment is not None
                else None
            )
            if include_hidden:
                visible.append(tender)
            elif link is not None and (link.saved_at or link.bidding_at):
                visible.append(tender)
            elif tender.source_type == "manual_upload" or verdict["bucket"] != "hidden":
                visible.append(tender)

        if sort == MATCH_ORDER:
            # The fit is a read-time verdict, so the only honest place to order
            # by it is here, after the gate and before the page is cut. Newest
            # first breaks a tie: a 55% tender found on the 13th still sits
            # above a 50% one found today, and two equal fits sit newest first.
            visible.sort(key=self._match_order_key)
        return visible[offset : offset + limit], len(visible)

    def _match_order_key(self, tender: Tender) -> tuple[int, float, float]:
        verdict = self.last_verdicts.get(tender.id, {})
        tier = int(verdict.get("tier") or 7)
        score = float(verdict.get("score") or 0.0)
        created = tender.created_at
        return (tier, -score, -(created.timestamp() if created is not None else 0.0))

    def understanding_terms(self, company) -> list[str]:
        """The confirmed capability/experience phrases that may feed fit.

        Raw words harvested from the about-company PDF are not read here. A
        human confirms a capability first, so a project name or a department
        acronym can never become work evidence on its own.
        """

        from app.discovery.relevance import understanding_terms_from_rows

        if company is None:
            return []
        company_id = getattr(company, "id", None)
        if company_id is None:
            return []
        if company_id not in self._understanding_terms_cache:
            self._understanding_terms_cache[company_id] = (
                understanding_terms_from_rows(
                    self.understanding.confirmed_capabilities(company_id),
                    self.understanding.confirmed_experiences(company_id),
                )
            )
        return self._understanding_terms_cache[company_id]

    def brief_terms(self, company) -> dict[str, list[str]]:
        """The current brief's four role groups, cached for one request.

        A stored brief wins. Before one is generated, the same deterministic
        role rules are applied to the profile and confirmed understanding. This
        is what makes ``technical support`` support and ``document scanning``
        core from the first list request, without a migration step.
        """

        company_id = getattr(company, "id", None)
        if company_id is None:
            return {"core": [], "adjacent": [], "support": [], "generic": []}
        if company_id not in self._brief_terms_cache:
            payload = self.briefs.current_payload(company_id)
            if payload is None:
                payload = self.briefs.derive_payload(company)
            self._brief_terms_cache[company_id] = payload_terms(payload)
        return self._brief_terms_cache[company_id]

    def brief_signature(self, company) -> str:
        company_id = getattr(company, "id", None)
        if company_id is None:
            return ""
        if company_id not in self._brief_signature_cache:
            self._brief_signature_cache[company_id] = self.briefs.signature(company)
        return self._brief_signature_cache[company_id]

    def capability_names(self, company) -> dict[uuid.UUID, str]:
        """The human-confirmed capability names, cached for the request."""

        company_id = getattr(company, "id", None)
        if company_id is None:
            return {}
        if self._capability_names is None:
            self._capability_names = {
                row.id: row.name
                for row in self.understanding.confirmed_capabilities(company_id)
            }
        return self._capability_names

    def similarity_for(self, company, tender) -> dict[str, Any] | None:
        """The cached similarity answer for one tender, with capability names."""

        company_id = getattr(company, "id", None)
        if company_id is None:
            return None
        if tender.id in self.last_similarity:
            return self.last_similarity[tender.id]
        judgment = self.similarity.get(
            company_id=company_id, tender_id=tender.id
        )
        payload = (
            judgment_payload(judgment, self.capability_names(company))
            if judgment is not None
            else None
        )
        self.last_similarity[tender.id] = payload
        return payload

    def _verdict(
        self,
        company,
        tender,
        *,
        rarity: dict[str, float],
        negative: dict[str, int],
        profile_hash: str,
        feedback_hash: str,
        cached,
        classify,
        rules_version: str,
        verdict_input_hash,
        understanding_terms: list[str] | None = None,
        brief_terms: dict[str, list[str]] | None = None,
    ) -> dict[str, Any]:
        """The company's verdict on one tender, reused only when still valid.

        The cache key covers everything that can move the answer. A profile
        edit, a save, a "not relevant", a re-published notice or a new rules
        version all change it, so a stale verdict is never shown.
        """

        from app.repositories.intelligence import tender_content_hash

        content_hash = tender_content_hash(tender)
        key = verdict_input_hash(
            profile_hash=profile_hash,
            tender_content_hash=content_hash,
            feedback_hash=feedback_hash,
            rules_version=rules_version,
        )
        if (
            cached is not None
            and cached.input_hash == key
            and cached.rules_version == rules_version
        ):
            reasons = list(cached.reasons or [])
            return {
                "bucket": cached.bucket,
                "tier": int(getattr(cached, "tier", 7) or 7),
                "score": float(cached.score),
                "matched_terms": list(cached.matched_terms or []),
                "reasons": reasons,
                "profile_complete": True,
                # The cache stores the reason text, not the flag. Recover the
                # flag from the reason that the rules wrote, so a warmed cache
                # does not silently drop the "in your region" badge.
                "region_match": any("your region" in reason for reason in reasons),
                "source": cached.source,
            }
        verdict = classify(
            company,
            tender,
            rarity=rarity,
            negative=negative,
            understanding_terms=understanding_terms,
            brief_terms=brief_terms,
        )
        verdict["source"] = "rules"
        return verdict

    def verdict_for(self, company, tender) -> dict[str, Any]:
        """Classify a single tender for a company (used by the detail route).

        A cached similarity answer is applied on top of the rules verdict, so
        the detail page's fit is the same fit the list showed.
        """

        from app.discovery.relevance import classify

        understanding_terms = self.understanding_terms(company)
        brief_terms = self.brief_terms(company)
        verdict = classify(
            company,
            tender,
            understanding_terms=understanding_terms,
            brief_terms=brief_terms,
        )
        judgment = self.similarity.get(
            company_id=company.id, tender_id=tender.id
        )
        return adjust_verdict(verdict, judgment, self.capability_names(company))

    def profile_complete(self, company) -> bool:
        """True when the profile says what work the company does, not only where.

        A company with no services, sectors, keywords or confirmed
        capability/experience has nothing to match a tender's work against, so
        the listing shows the corpus under an "all tenders" heading and asks
        for a profile instead of pretending the corpus is relevant. Raw words
        from an uploaded about-company PDF do not count until a human confirms
        a capability from them.
        """

        understanding_terms = self.understanding_terms(company)
        brief_terms = self.brief_terms(company)
        try:
            from app.discovery.relevance import profile_is_complete

            return profile_is_complete(
                company,
                understanding_terms=understanding_terms,
                brief_terms=brief_terms,
            )
        except ImportError:  # pragma: no cover - base without the module
            from app.services.relevance import company_profile_terms

            return bool(
                company_profile_terms(
                    company,
                    understanding_terms=understanding_terms,
                    brief_terms=brief_terms,
                )
            )

    def get(self, tender_id: uuid.UUID) -> Tender:
        tender = self.tenders.get(tender_id)
        if tender is None:
            raise NotFound("Tender not found.")
        return tender

    def counts(self, tender_id: uuid.UUID) -> dict[str, int]:
        return {
            "documents": self.documents.count_current(tender_id),
            "requirements": self.requirements.count_active(tender_id),
        }

    def _record_feedback(
        self,
        company_id: uuid.UUID,
        company,
        tender: Tender,
        verdict: str,
    ) -> None:
        """Remember the human signal behind a save or a "not relevant".

        The words stored are the work types the classifier found and the
        profile terms that made this tender match. They are a small negative
        weight on similar work later, never a ban: the matcher still matches
        the same word on a different tender.
        """

        from app.discovery.relevance import classify
        from app.repositories.intelligence import (
            ClassificationRepository,
            FeedbackRepository,
        )

        work_types: list[str] = []
        classification = ClassificationRepository(self.session).get(tender.id)
        if classification is not None:
            work_types = list(classification.work_types or [])
        matched: list[str] = []
        if company is not None:
            try:
                matched = list(
                    classify(
                        company,
                        tender,
                        understanding_terms=self.understanding_terms(company),
                        brief_terms=self.brief_terms(company),
                    )["matched_terms"]
                )
            except Exception:  # noqa: BLE001 - feedback must not fail a save
                matched = []
        FeedbackRepository(self.session).upsert(
            company_id,
            tender.id,
            verdict=verdict,
            work_types=work_types,
            matched_terms=matched,
        )

    def save(
        self,
        *,
        tender_id: uuid.UUID,
        company_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        company=None,
    ) -> Tender:
        tender = self.get(tender_id)
        try:
            self.last_links[tender.id] = self.company_tenders.upsert(
                company_id=company_id,
                tender_id=tender.id,
                values={"saved_at": utcnow(), "dismissed_at": None},
            )
            self._record_feedback(company_id, company, tender, "relevant")
            self.audit.log(
                company_id=company_id,
                actor_user_id=actor_user_id,
                action="tender.saved",
                entity_type="tender",
                entity_id=tender.id,
                after={"saved": True},
            )
            # The first save is a useful baseline even before a manual snapshot.
            WorkflowService(self.session).capture_snapshot(
                tender.id,
                company_id=company_id,
                actor_user_id=actor_user_id,
                commit=False,
            )
            self.session.commit()
        except Exception:
            self.session.rollback()
            raise
        return tender

    def unsave(
        self,
        *,
        tender_id: uuid.UUID,
        company_id: uuid.UUID,
        actor_user_id: uuid.UUID,
    ) -> Tender:
        """Take a tender out of the saved list without calling it irrelevant.

        Only the save is undone: the tender goes back to being an ordinary
        candidate the gate decides about, and the positive "saved" signal it
        recorded is withdrawn with it (a removed save is not a preference the
        matcher should keep using). A bid already started is not cancelled --
        the row stays in My tenders because a bid is work in progress, and the
        response says so -- and nothing is dismissed, so the tender can be
        found and saved again. Calling it twice is harmless.
        """

        tender = self.get(tender_id)
        try:
            link = self.company_tenders.get(company_id, tender.id)
            if link is not None and link.saved_at is not None:
                link.saved_at = None
                link.updated_at = utcnow()
                self.session.flush()
            from app.repositories.intelligence import FeedbackRepository

            feedback = FeedbackRepository(self.session)
            row = feedback.get(company_id, tender.id)
            if row is not None and row.verdict == "relevant":
                feedback.delete(company_id, tender.id)
            self.audit.log(
                company_id=company_id,
                actor_user_id=actor_user_id,
                action="tender.unsaved",
                entity_type="tender",
                entity_id=tender.id,
                after={"saved": False, "bidding": bool(link and link.bidding_at)},
            )
            self.session.commit()
        except Exception:
            self.session.rollback()
            raise
        self.last_links[tender.id] = link
        return tender

    def dismiss(
        self,
        *,
        tender_id: uuid.UUID,
        company_id: uuid.UUID,
        actor_user_id: uuid.UUID,
    ) -> Tender:
        tender = self.get(tender_id)
        try:
            self.company_tenders.upsert(
                company_id=company_id,
                tender_id=tender.id,
                values={"dismissed_at": utcnow()},
            )
            self.audit.log(
                company_id=company_id,
                actor_user_id=actor_user_id,
                action="tender.dismissed",
                entity_type="tender",
                entity_id=tender.id,
                after={"dismissed": True},
            )
            self.session.commit()
        except Exception:
            self.session.rollback()
            raise
        return tender

    def mark_not_relevant(
        self,
        *,
        tender_id: uuid.UUID,
        company_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        company=None,
    ) -> Tender:
        """Hide one tender for this company and remember why.

        Hiding is the same dismissal the list already honours; the extra fact
        is the negative signal, which demotes similar work later. Nothing is
        banned: the company's declared words still match, they just rank lower
        while the signal is recent.
        """

        tender = self.get(tender_id)
        try:
            # "Not relevant" is the stronger form of "remove from saved": it
            # withdraws the bookmark as well as dismissing the tender, so no
            # stale save survives behind the negative verdict.
            link = self.company_tenders.upsert(
                company_id=company_id,
                tender_id=tender.id,
                values={"dismissed_at": utcnow(), "saved_at": None},
            )
            self._record_feedback(company_id, company, tender, "not_relevant")
            self.audit.log(
                company_id=company_id,
                actor_user_id=actor_user_id,
                action="tender.not_relevant",
                entity_type="tender",
                entity_id=tender.id,
                after={"dismissed": True, "saved": False, "feedback": "not_relevant"},
            )
            self.session.commit()
        except Exception:
            self.session.rollback()
            raise
        self.last_links[tender.id] = link
        return tender

    def start_bidding(
        self,
        *,
        tender_id: uuid.UUID,
        company_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        company=None,
    ) -> Tender:
        tender = self.get(tender_id)
        try:
            self.last_links[tender.id] = self.company_tenders.upsert(
                company_id=company_id,
                tender_id=tender.id,
                values={"bidding_at": utcnow(), "dismissed_at": None},
            )
            self._record_feedback(company_id, company, tender, "relevant")
            self.audit.log(
                company_id=company_id,
                actor_user_id=actor_user_id,
                action="tender.bidding_started",
                entity_type="tender",
                entity_id=tender.id,
                after={"bidding": True},
            )
            self.session.commit()
        except Exception:
            self.session.rollback()
            raise
        if self.settings.doc_fetch_auto_on and tender.source_type != "manual_upload":
            notify_bidding(tender.id, company_id, actor_user_id)
        return tender

    def summary(self, tender_id: uuid.UUID) -> dict[str, int]:
        return self.requirements.summary(tender_id)

    def create_manual(
        self,
        *,
        work_title: str,
        data: bytes,
        filename: str | None,
        authority: str | None = None,
        reference: str | None = None,
        location: str | None = None,
        region: str | None = None,
        work_type: str | None = None,
        closing_at: datetime | None = None,
        published_at: datetime | None = None,
        value_amount: Decimal | None = None,
        source_url: str | None = None,
        company_id: uuid.UUID | None = None,
        actor_user_id: uuid.UUID | None = None,
    ) -> tuple[Tender, bool]:
        """Create a manual tender from its first PDF, or reuse a duplicate.

        The manual dedupe key includes the first document's sha256, so the same
        PDF and title is one tender. A corrupt PDF never creates a tender: the
        document is parsed before the call is committed.
        """

        title = trim(work_title, 500)
        if not title:
            raise BadRequest("A tender needs a work title.")
        mime = storage.validate_upload(data, self.settings)
        if mime != "application/pdf":
            raise BadRequest("Tender documents must be PDF files.")
        digest = storage.sha256_bytes(data)
        dedupe_key = manual_dedupe_key(work_title=title, first_document_sha=digest)

        existing = self.tenders.find_by_dedupe_key(dedupe_key)
        if existing is not None:
            document_service = DocumentService(self.session, self.settings)
            document_service.attach(
                tender_id=existing.id,
                filename=filename,
                data=data,
                source_url=source_url,
                fetched_at=utcnow(),
            )
            document_service.sync_fetch_state(existing.id)
            self.session.commit()
            return existing, False

        tender = self.tenders.create(
            {
                "portal_id": None,
                "portal_tender_id": None,
                "dedupe_key": dedupe_key,
                "reference": trim(reference, 300),
                "reference_normalised": (
                    trim(reference, 300).casefold()
                    if trim(reference, 300)
                    else None
                ),
                "authority": trim(authority, 300),
                "organisation_chain": trim(authority, 300),
                "work_title": title,
                "work_type": trim(work_type, 120),
                "location": trim(location, 200),
                "region": trim(region, 200),
                "closing_at": closing_at,
                "published_at": published_at,
                "value_amount": value_amount,
                "currency": "INR",
                "source_type": "manual_upload",
                "extracted_from": "manual_upload",
                "source_url": trim(source_url, 2000),
                "retrieved_at": utcnow(),
                "content_hash": dedupe_key,
                "unverified": [],
                "raw": {
                    "created_by_user_id": str(actor_user_id)
                    if actor_user_id is not None
                    else None
                },
            }
        )
        document_service = DocumentService(self.session, self.settings)
        document_service.attach(
            tender_id=tender.id,
            filename=filename,
            data=data,
            source_url=source_url,
            fetched_at=utcnow(),
        )
        document_service.sync_fetch_state(tender.id)
        # A re-upload with every identity field and a later publication date is
        # a corrigendum candidate, never an automatic replacement.
        WorkflowService(self.session).propose_lineage(
            tender.id,
            company_id=company_id,
            actor_user_id=actor_user_id,
            commit=False,
        )
        self.audit.log(
            action="tender.created",
            entity_type="tender",
            company_id=company_id,
            actor_user_id=actor_user_id,
            entity_id=tender.id,
            after={
                "source_type": "manual_upload",
                "work_title": title,
                "dedupe_key": dedupe_key,
            },
        )
        self.session.commit()
        return tender, True

    def out(
        self,
        tender: Tender,
        *,
        summary: dict[str, int] | None = None,
        relevance: dict[str, Any] | None = None,
        company=None,
    ) -> dict[str, Any]:
        counts = self.counts(tender.id)
        state = self.aux.fetch_state(tender.id)
        link = self.last_links.get(tender.id)
        similarity = (
            self.similarity_for(company, tender)
            if company is not None
            else self.last_similarity.get(tender.id)
        )
        return {
            "id": tender.id,
            "portal_id": tender.portal_id,
            "portal_tender_id": tender.portal_tender_id,
            "reference": tender.reference,
            "authority": tender.authority,
            "organisation_chain": tender.organisation_chain,
            "work_title": tender.work_title,
            "work_type": tender.work_type,
            "location": tender.location,
            "region": tender.region,
            "closing_at": tender.closing_at,
            "published_at": tender.published_at,
            "value_amount": tender.value_amount,
            "currency": tender.currency,
            "source_type": tender.source_type,
            "extracted_from": tender.extracted_from,
            "source_url": tender.source_url,
            "retrieved_at": tender.retrieved_at,
            "cycle_state": tender.cycle_state or "active",
            "termination_excerpt": tender.termination_excerpt,
            "termination_observed_at": tender.termination_observed_at,
            "unverified": tender.unverified or [],
            "supersedes_tender_id": tender.supersedes_tender_id,
            "supersedes_state": tender.supersedes_state,
            "merged_into_tender_id": tender.merged_into_tender_id,
            "document_count": counts["documents"],
            "requirement_count": counts["requirements"],
            "requirement_summary": summary or self.summary(tender.id),
            "fetch_state": {
                "last_requested_at": state.last_requested_at,
                "last_attempt_at": state.last_attempt_at,
                "attempts": state.attempts,
                "expected_covers": state.expected_covers,
                "documents_present": state.documents_present,
                "missing_covers": state.missing_covers or [],
                "last_error": state.last_error,
                "updated_at": state.updated_at,
            }
            if state is not None
            else None,
            "relevance": relevance,
            "similarity": similarity,
            "saved": bool(link is not None and link.saved_at is not None),
            "bidding": bool(link is not None and link.bidding_at is not None),
            "created_at": tender.created_at,
            "updated_at": tender.updated_at,
        }
