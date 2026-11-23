"""One constrained question: is this tender the company's line of work?

The rules matcher already decides most of the corpus. The AI is given every
candidate that reaches it, without being told its rules tier, and may answer
**only** ``yes``, ``no`` or ``not_sure``. It must quote one exact tender line
and one exact company line. The quotes are validated against stored evidence
before they are shown; an unanchored ``yes`` becomes ``unsure`` with
``needs_confirmation`` and changes no score.

The evidence hierarchy is deterministic:

* an exact core line from the company brief is strongest;
* a semantically matching core line is next;
* an adjacent declared work line is next;
* a support-only line (``technical support``) is next;
* a generic-only line is recall, not a match;
* no line on either side is a valid ``no_evidence`` answer.

This module never decides eligibility, money, deadlines or whether the company
should bid. Only the deterministic assessment can pass a mandatory criterion.
"""

from __future__ import annotations

import uuid
from typing import Any, Iterable

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.errors import BadRequest, ServiceUnavailable
from app.core.settings import Settings, get_settings
from app.core.time import utcnow
from app.discovery.relevance import band_score
from app.discovery.roles import classify_term
from app.integrations.ai import AIError, AIProvider
from app.models.corpus import Tender
from app.models.identity import Company
from app.repositories.intelligence import stable_hash, tender_content_hash
from app.repositories.understanding import (
    CompanyUnderstandingRepository,
    SimilarityRepository,
)
from app.services.company_brief import (
    CompanyBriefService,
    payload_signature,
    payload_terms,
)
from app.services.corpus_rules import normalize_for_match, trim
from app.services.evidence import (
    MATCH_ADJACENT,
    MATCH_EXACT_CORE,
    MATCH_GENERIC,
    MATCH_NONE,
    MATCH_RANK,
    MATCH_SEMANTIC_CORE,
    MATCH_SUPPORT,
    EvidenceLine,
    EvidenceRetriever,
)

SIMILARITY_VERSION = "2026-11-22.1"
MAX_ANCHOR_CHARS = 1200
MAX_REASON_CHARS = 600

SYSTEM_PROMPT = """You compare tender notices with a company's editable work brief.
Return JSON only, in exactly this shape:
{"judgments": [{"tender_id": "...", "title": "...", "answer": "yes",
  "tender_evidence": "...", "company_evidence": "...",
  "match_type": "exact_core", "reason": "..."}]}

Rules, all mandatory:
- The company brief has four kinds of terms. Treat them as evidence, not as a
  ranking you are asked to invent:
  * core = the company's specific confirmed work or delivered projects;
  * adjacent = specific work the company declared but has not confirmed;
  * support = delivery words such as technical support, document retrieval,
    version control; they are not core work;
  * generic = recall words such as technology or web pages; they never make a
    notice the company's work on their own.
- Answer "yes" only when the tender's own work belongs to the same family as a
  core or adjacent term. A support-only match can be "yes" only if the work is
  genuinely a support service the company provides; use match_type
  "support_only". A generic-only match is never enough for yes.
- "not_sure" is a valid answer. Use it when the notice or the company side does
  not state enough to tell. "no_evidence" is a valid match_type when neither
  side has a usable line; never invent one.
- "tender_evidence" must copy an exact line from the tender title, work type or
  requirement lines supplied. "company_evidence" must copy an exact line from
  the company brief supplied. Never paraphrase either quote.
- "match_type" must be exactly one of: exact_core, semantic_core, adjacent,
  support_only, generic_only, no_evidence.
  * exact_core: the company evidence is a core line and the tender uses that
    work phrase or an exact equivalent;
  * semantic_core: the same core family of work, different wording;
  * adjacent: a specific declared term, not a confirmed core capability;
  * support_only: only a delivery/support term matches;
  * generic_only: only a generic recall term matches;
  * no_evidence: no usable line on at least one side.
- "title" is the notice's title exactly as supplied.
- "reason" is one short sentence explaining the answer. It is not a score and
  must not mention eligibility, money, deadlines or whether to bid.
- One judgment per tender id, using the id exactly as given. No other text.
- A place, department name, college name, project acronym (NHAI, DCTE, MAHSR)
  or generic word (computer, document, entry, software, technical, high) is
  never evidence that the work is the company's. Decide from the kind of work.
- Never decide eligibility, money, deadlines, or whether the company should
  bid. You are only saying whether this is the same kind of work.

Examples (tender title -> company evidence -> match_type -> answer):
1. "Digitisation of court records" -> core "document scanning" -> semantic_core -> yes
2. "Document scanning of examination records" -> core "document scanning" -> exact_core -> yes
3. "Supply of desktop computers" -> core "document scanning" -> no_evidence -> no
4. "Annual technical support for the portal" -> support "technical support" -> support_only -> yes
5. "Housekeeping of a district hospital" -> core "document scanning" -> no_evidence -> no
6. "Supply of medicines" -> core "document scanning" -> no_evidence -> not_sure
7. "Web portal maintenance" -> generic "web pages" -> generic_only -> not_sure
"""


class JudgmentAnswer(BaseModel):
    tender_id: str = ""
    title: str = Field(default="", max_length=500)
    answer: str = "not_sure"
    tender_evidence: str = Field(default="", max_length=1200)
    company_evidence: str = Field(default="", max_length=1200)
    match_type: str = Field(default="", max_length=40)
    reason: str = Field(default="", max_length=1200)
    #: Legacy fields accepted so an older cached prompt or a small test double
    #: still produces a usable answer. ``anchor`` is the old tender quote and
    #: ``capability`` is the old company term name.
    anchor: str = Field(default="", max_length=600)
    capability: str = Field(default="", max_length=200)


class JudgmentAnswerList(BaseModel):
    judgments: list[JudgmentAnswer] = Field(default_factory=list)


def _tender_work_text(tender: Tender) -> str:
    """The notice's own work words, never the authority or the location."""

    return " ".join(
        part for part in (tender.work_title, tender.work_type) if part
    )


def _clean_answer(value: str) -> str:
    answer = (value or "").strip().lower().replace("-", "_")
    if answer == "not_sure":
        return "unsure"
    return answer if answer in {"yes", "no", "unsure"} else "unsure"


def _match_rank(match_type: str | None) -> int | None:
    if not match_type:
        return None
    return MATCH_RANK.get(str(match_type))


def judgment_payload(judgment, capability_names: dict[uuid.UUID, str]) -> dict[str, Any]:
    return {
        "answer": judgment.answer,
        "title": getattr(judgment, "judged_title", None),
        "anchor_text": judgment.anchor_text,
        "anchor_verified": judgment.anchor_verified,
        "tender_evidence": judgment.tender_evidence,
        "company_evidence": judgment.company_evidence,
        "match_type": judgment.match_type,
        "evidence_rank": judgment.evidence_rank,
        "needs_confirmation": judgment.needs_confirmation,
        "reason": judgment.reason,
        "capabilities": [
            capability_names[item]
            for item in (judgment.matched_capability_ids or [])
            if item in capability_names
        ],
        "judged_at": judgment.judged_at,
        "model": judgment.model,
        "rules_version": judgment.rules_version,
    }


def adjust_verdict(
    verdict: dict[str, Any],
    judgment,
    capability_names: dict[uuid.UUID, str],
) -> dict[str, Any]:
    """Apply a cached judgment on top of the rules verdict, never into it.

    The verdict cache stays rules-only; a judgment is a second, later signal
    and a human can revoke a capability. The evidence hierarchy is deterministic:

    * exact core -> tier 1;
    * semantic core -> tier 2;
    * adjacent -> tier 3 with the region, otherwise tier 4;
    * support only -> tier 5 with the region, otherwise tier 6;
    * generic only -> tier 6;
    * a ``no`` -> tier 6 at the bottom of the visible list.

    A ``yes`` without a verified tender-side line is stored as ``unsure`` and
    never reaches this function. Neither answer changes eligibility or hides
    the row.
    """

    if judgment is None or judgment.answer == "unsure":
        return verdict

    adjusted = {
        **verdict,
        "reasons": list(verdict.get("reasons") or []),
        "score": float(verdict.get("score") or 0.0),
        "tier": int(verdict.get("tier") or 7),
    }
    matched = list(verdict.get("matched_terms") or [])
    region_match = bool(verdict.get("region_match"))

    if judgment.answer == "no":
        tier = 6
        adjusted["tier"] = tier
        adjusted["bucket"] = "possible"
        adjusted["score"] = band_score(tier, max(1, len(matched)))
        adjusted["reasons"].append(
            "AI read this as a different line of work, so it is at the bottom "
            "of the list; it is still shown and can be saved."
        )
        return adjusted

    if judgment.answer != "yes":
        return adjusted

    match_type = str(getattr(judgment, "match_type", "") or "")
    if match_type == MATCH_EXACT_CORE:
        tier = 1
    elif match_type == MATCH_SEMANTIC_CORE:
        tier = 2
    elif match_type == MATCH_ADJACENT:
        tier = 3 if region_match else 4
    elif match_type == MATCH_SUPPORT:
        tier = 5 if region_match else 6
    elif match_type == MATCH_GENERIC:
        tier = 6
    else:
        # Legacy rows written before the evidence hierarchy had no match_type.
        # Keep their old behaviour: a confirmed match is strong.
        tier = 1 if matched else 2

    adjusted["tier"] = tier
    adjusted["bucket"] = "strong" if tier <= 3 else "possible"
    adjusted["score"] = band_score(
        tier, max(1, len(matched) + (1 if getattr(judgment, "company_evidence", None) else 0))
    )

    names = [
        capability_names[item]
        for item in (judgment.matched_capability_ids or [])
        if item in capability_names
    ]
    evidence = getattr(judgment, "tender_evidence", None) or judgment.anchor_text
    company_evidence = getattr(judgment, "company_evidence", None)
    if match_type == MATCH_EXACT_CORE:
        adjusted["reasons"].append(
            "AI matched an exact core line from your brief"
            + (f' ("{company_evidence}")' if company_evidence else "")
            + (f' to "{evidence}".' if evidence else ".")
        )
    elif match_type == MATCH_SEMANTIC_CORE:
        adjusted["reasons"].append(
            "AI matched the same family as your core work"
            + (f' ("{company_evidence}")' if company_evidence else "")
            + (f' to "{evidence}".' if evidence else ".")
        )
    elif match_type == MATCH_ADJACENT:
        adjusted["reasons"].append(
            "AI matched an adjacent declared work term; it is not a confirmed "
            "core capability, so it ranks below one."
        )
    elif match_type == MATCH_SUPPORT:
        adjusted["reasons"].append(
            "AI matched a support-only term (delivery, not core work), so it "
            "stays in the possible band."
        )
    elif match_type == MATCH_GENERIC:
        adjusted["reasons"].append(
            "AI found only a generic recall term; it does not rank as a match."
        )
    elif names:
        adjusted["reasons"].append(
            f'Matches your confirmed capability "{names[0]}"; the notice says '
            f'"{evidence}".'
        )
    else:
        adjusted["reasons"].append(
            f'The notice says "{evidence}", which is your kind of work.'
        )
    return adjusted


def _lines_text(lines: list[dict[str, Any]], *, empty: str) -> str:
    if not lines:
        return empty
    out: list[str] = []
    for line in lines:
        role = line.get("role") or "tender"
        source = line.get("source") or "source"
        page = (
            f" page {line.get('page_index')}"
            if line.get("page_index") is not None
            else ""
        )
        out.append(f"- [{role}] ({source}{page}) {line.get('text')}")
    return "\n".join(out)


def _brief_text(bundle: dict[str, Any]) -> str:
    terms = bundle.get("company_terms") or {}
    groups = []
    for role in ("core", "adjacent", "support", "generic"):
        values = [str(value) for value in (terms.get(role) or [])]
        groups.append(f"{role}: {', '.join(values) if values else '(none)'}")
    return (
        "\n".join(groups)
        + "\n\nExact company evidence lines (quote one of these verbatim):\n"
        + _lines_text(
            bundle.get("company_lines") or [],
            empty="(no company evidence line was retrieved)",
        )
    )


def _tender_text(tender: Tender, bundle: dict[str, Any]) -> str:
    return (
        f"tender_id: {tender.id}\n"
        f"title: {tender.work_title or '(no title)'}\n"
        f"work type: {tender.work_type or '(not stated)'}\n"
        "Company evidence lines:\n"
        + _lines_text(
            bundle.get("company_lines") or [],
            empty="(no company evidence line was retrieved)",
        )
        + "\nTender evidence lines (quote one of these verbatim):\n"
        + _lines_text(
            bundle.get("tender_lines") or [],
            empty="(no tender evidence line was retrieved)",
        )
    )


def _line_by_term(term: str, lines: list[dict[str, Any]]) -> EvidenceLine | None:
    target = normalize_for_match(term)
    if not target:
        return None
    for line in lines:
        line_term = normalize_for_match(str(line.get("term") or ""))
        if line_term and line_term == target:
            return EvidenceRetriever.find_company_line(
                str(line.get("text") or ""), lines
            )
    return None


class TenderSimilarityService:
    def __init__(
        self,
        session: Session,
        *,
        ai: AIProvider | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self.ai = ai
        self.repository = SimilarityRepository(session)
        self.understanding = CompanyUnderstandingRepository(session)
        self.briefs = CompanyBriefService(session)

    # ------------------------------------------------------------------ read
    def capability_names(self, company_id: uuid.UUID) -> dict[uuid.UUID, str]:
        return {
            row.id: row.name
            for row in self.understanding.confirmed_capabilities(company_id)
        }

    def existing(
        self, *, company_id: uuid.UUID, tender_id: uuid.UUID
    ) -> dict[str, Any] | None:
        judgment = self.repository.get(company_id=company_id, tender_id=tender_id)
        if judgment is None:
            return None
        return judgment_payload(judgment, self.capability_names(company_id))

    # ----------------------------------------------------------------- write
    def judge_one(
        self, *, company: Company, tender: Tender, force: bool = False
    ) -> dict[str, Any]:
        judgments = self.judge_many(company=company, tenders=[tender], force=force)
        return judgments[tender.id]

    def judge_many(
        self,
        *,
        company: Company,
        tenders: list[Tender],
        force: bool = False,
    ) -> dict[uuid.UUID, dict[str, Any]]:
        capabilities = self.understanding.confirmed_capabilities(company.id)
        brief_payload = self.briefs.current_payload(company.id)
        if brief_payload is None:
            brief_payload = self.briefs.derive_payload(company)
        terms = payload_terms(brief_payload)
        if not capabilities and not any(terms.values()):
            raise BadRequest(
                "Confirm at least one capability from your company profile "
                "or generate a company brief before asking the AI to compare "
                "tenders."
            )
        if not tenders:
            return {}

        capability_names = {row.id: row.name for row in capabilities}
        content = {
            tender.id: self._input_hash(
                company.id, tender, capabilities, brief_payload
            )
            for tender in tenders
        }
        cached = self.repository.get_many(company.id, [t.id for t in tenders])

        pending = [
            tender
            for tender in tenders
            if force
            or cached.get(tender.id) is None
            or cached[tender.id].input_hash != content[tender.id]
        ]
        if pending:
            if self.ai is None or not self.ai.configured:
                raise ServiceUnavailable(
                    "Add an AI key to compare tenders with your brief."
                )
            size = max(self.settings.similarity_batch_size, 1)
            for start in range(0, len(pending), size):
                batch = pending[start : start + size]
                self._judge_batch(
                    company=company,
                    tender_batch=batch,
                    capabilities=capabilities,
                    brief_payload=brief_payload,
                    hashes=content,
                )

        self.session.commit()
        result: dict[uuid.UUID, dict[str, Any]] = {}
        for tender in tenders:
            judgment = self.repository.get(
                company_id=company.id, tender_id=tender.id
            )
            if judgment is not None:
                result[tender.id] = judgment_payload(judgment, capability_names)
        return result

    def _input_hash(
        self,
        company_id: uuid.UUID,
        tender: Tender,
        capabilities: list,
        brief_payload: dict[str, Any] | None,
    ) -> str:
        signature = [
            {
                "id": str(row.id),
                "name": row.normalized,
                "aliases": sorted(
                    normalize_for_match(alias) for alias in (row.aliases or [])
                ),
                "description": normalize_for_match(row.description or ""),
            }
            for row in sorted(capabilities, key=lambda item: str(item.id))
        ]
        return stable_hash(
            {
                "rules_version": SIMILARITY_VERSION,
                "capabilities": signature,
                "brief": payload_signature(brief_payload),
                "tender": tender_content_hash(tender),
            }
        )

    def _judge_batch(
        self,
        *,
        company: Company,
        tender_batch: list[Tender],
        capabilities: list,
        brief_payload: dict[str, Any],
        hashes: dict[uuid.UUID, str],
    ) -> None:
        retriever = EvidenceRetriever(self.session)
        bundles: dict[uuid.UUID, dict[str, Any]] = {}
        prompts: list[str] = []
        for tender in tender_batch:
            bundle = retriever.retrieve(
                company, tender, payload=brief_payload
            )
            bundles[tender.id] = bundle
            prompts.append(_tender_text(tender, bundle))

        user = (
            "Company brief (roles, not a ranking):\n"
            + _brief_text(bundles[tender_batch[0].id] if tender_batch else {})
            + "\n\nTenders to compare. The tier, score and bucket are not shown, "
            "and you must not infer them from the order:\n\n"
            + "\n\n---\n\n".join(prompts)
        )
        try:
            raw = self.ai.complete_json(system=SYSTEM_PROMPT, user=user)
            answer = JudgmentAnswerList.model_validate(raw)
        except (AIError, ValueError, TypeError) as exc:
            raise ServiceUnavailable(
                "The AI comparison could not be reached. Try again later."
            ) from exc

        by_key = {str(tender.id): tender for tender in tender_batch}
        by_capability = {normalize_for_match(row.name): row for row in capabilities}
        alias_to_capability = {
            normalize_for_match(alias): row
            for row in capabilities
            for alias in (row.aliases or [])
        }
        model = getattr(self.ai, "model", None)
        seen: set[str] = set()

        for item in answer.judgments:
            key = (item.tender_id or "").strip()
            tender = by_key.get(key)
            if tender is None or key in seen:
                continue
            seen.add(key)
            self._store(
                company_id=company.id,
                tender=tender,
                item=item,
                by_capability=by_capability,
                alias_to_capability=alias_to_capability,
                company_lines=bundles[tender.id].get("company_lines") or [],
                tender_lines=bundles[tender.id].get("tender_lines") or [],
                input_hash=hashes[tender.id],
                model=model,
            )

        # A notice the reader silently skipped is "unsure", not a yes: an
        # unanswered tender must not change the fit.
        for key, tender in by_key.items():
            if key in seen:
                continue
            self._store(
                company_id=company.id,
                tender=tender,
                item=JudgmentAnswer(tender_id=key, answer="not_sure"),
                by_capability=by_capability,
                alias_to_capability=alias_to_capability,
                company_lines=bundles[tender.id].get("company_lines") or [],
                tender_lines=bundles[tender.id].get("tender_lines") or [],
                input_hash=hashes[tender.id],
                model=model,
                unread=True,
            )

    def _store(
        self,
        *,
        company_id: uuid.UUID,
        tender: Tender,
        item: JudgmentAnswer,
        by_capability: dict[str, Any],
        alias_to_capability: dict[str, Any],
        company_lines: list[dict[str, Any]],
        tender_lines: list[dict[str, Any]],
        input_hash: str,
        model: str | None,
        unread: bool = False,
    ) -> None:
        answer = _clean_answer(item.answer)
        tender_quote = trim(
            item.tender_evidence or item.anchor, MAX_ANCHOR_CHARS
        )
        tender_line = (
            EvidenceRetriever.find_tender_line(tender_quote, tender_lines)
            if tender_quote
            else None
        )
        if tender_line is None and tender_quote:
            work_text = normalize_for_match(_tender_work_text(tender))
            if normalize_for_match(tender_quote) in work_text:
                tender_line = EvidenceLine(
                    text=tender_quote,
                    role=None,
                    source="notice_title",
                    reference=str(tender.id),
                )
        tender_evidence = tender_line.text if tender_line is not None else None

        company_quote = trim(item.company_evidence, MAX_ANCHOR_CHARS)
        company_line = (
            EvidenceRetriever.find_company_line(company_quote, company_lines)
            if company_quote
            else None
        )
        if company_line is None and item.capability:
            company_line = _line_by_term(item.capability, company_lines)

        capability = by_capability.get(normalize_for_match(item.capability)) or (
            alias_to_capability.get(normalize_for_match(item.capability))
        )
        if company_line is None and capability is not None:
            # A legacy answer names a human-confirmed capability but quotes no
            # line. The capability row itself is the company evidence.
            company_line = EvidenceLine(
                text=capability.name,
                role=classify_term(capability.name, confirmed=True),
                source="capability",
                term=capability.name,
                reference=str(capability.id),
            )

        if company_line is not None:
            company_evidence = company_line.text
        else:
            company_evidence = None

        source_text = company_quote or (
            company_line.text if company_line is not None else ""
        )
        derived_match, derived_rank = EvidenceRetriever.match_type(
            company_line, source_text
        )

        needs_confirmation = False
        result = answer
        if answer == "yes" and (tender_line is None or company_line is None):
            result = "unsure"
            needs_confirmation = True
        elif answer == "yes" and derived_match == MATCH_GENERIC:
            result = "unsure"
            needs_confirmation = True
        elif answer == "yes" and derived_match == MATCH_NONE:
            result = "unsure"
            needs_confirmation = True

        matched: list[uuid.UUID] = []
        if result == "yes":
            if capability is not None:
                matched = [capability.id]
            elif company_line is not None and company_line.reference:
                try:
                    matched = [uuid.UUID(str(company_line.reference))]
                except (ValueError, TypeError):
                    matched = []

        if result == "yes":
            if item.reason.strip():
                reason = trim(item.reason, MAX_REASON_CHARS)
            elif capability is not None:
                reason = f'Matches your confirmed capability "{capability.name}".'
            else:
                reason = "The notice and the company brief describe the same kind of work."
        elif result == "no":
            if item.reason.strip():
                reason = trim(item.reason, MAX_REASON_CHARS)
            else:
                reason = (
                    "The AI read this as a different line of work. It ranks at "
                    "the bottom of the list; it stays visible and a human can "
                    "override."
                )
        elif unread:
            reason = "The AI did not answer for this notice."
        elif answer == "yes":
            reason = (
                "AI suggested a match, but one of the quoted lines was not found "
                "in the stored evidence. Confirm it yourself before relying on it."
            )
        else:
            if item.reason.strip():
                reason = trim(item.reason, MAX_REASON_CHARS)
            else:
                reason = "The AI could not tell whether this is your line of work."

        self.repository.upsert(
            company_id=company_id,
            tender_id=tender.id,
            values={
                "input_hash": input_hash,
                "answer": result,
                "judged_title": trim(item.title or tender.work_title, 500),
                "anchor_text": tender_evidence,
                "anchor_verified": bool(tender_evidence) and result == "yes",
                "tender_evidence": tender_evidence,
                "company_evidence": company_evidence,
                "match_type": derived_match,
                "evidence_rank": derived_rank,
                "needs_confirmation": needs_confirmation,
                "reason": reason,
                "matched_capability_ids": matched,
                "model": model,
                "prompt_version": SIMILARITY_VERSION,
                "rules_version": SIMILARITY_VERSION,
                "judged_at": utcnow(),
            },
        )
