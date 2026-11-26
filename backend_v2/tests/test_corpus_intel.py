"""Corpus intelligence: one label per tender, one verdict per company.

The read path must stay deterministic and fast, so the expensive steps happen
here, in the background, and are stored with the hash of their inputs. These
tests cover the three pieces: the label, the verdict cache and the bounded AI
adjudication that is only ever allowed to answer "is this the company's work".
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from sqlalchemy import select

from app.core.db import SessionLocal
from app.discovery.adjudication import adjudicate
from app.discovery.classification import (
    CLASSIFIER_VERSION,
    classify_title,
    significant_words,
)
from app.discovery.intel import CorpusIntelligenceService
from app.discovery.relevance import RULES_VERSION
from app.models.corpus import Tender
from app.models.intelligence import (
    CompanyTenderVerdict,
    TenderClassification,
)
from app.models.understanding import TenderSimilarityJudgment
from app.repositories.corpus import TenderRepository
from app.repositories.identity import CompanyRepository
from app.repositories.intelligence import FeedbackRepository
from app.services.tender_service import TenderService

NOW = datetime(2026, 11, 16, 9, 0, tzinfo=timezone.utc)


# -- the label ------------------------------------------------------------


def test_the_label_is_read_from_the_title_not_a_trade_list():
    label = classify_title(
        "Construction of four lane road on NH-48 [NHAI/2027/1] [2027_NHAI_1_1]"
    )

    # The identifiers are stripped, the boilerplate words are dropped, and what
    # remains is the work the title actually names.
    assert label["primary_work_type"] == "construction"
    assert "n hai" not in " ".join(label["work_types"])
    assert "2027" not in " ".join(label["work_types"])
    assert "nhai" not in " ".join(label["work_types"])
    assert label["confidence"] > 0


def test_function_words_are_grammar_and_carry_no_work_meaning():
    words = significant_words("Supply of and installation of computers for the office")
    assert words == ["supply", "installation", "computers", "office"]


def test_an_empty_title_is_labelled_empty_not_guessed():
    label = classify_title("")
    assert label["work_types"] == []
    assert label["primary_work_type"] is None
    assert label["confidence"] == 0.0


# -- the stored label -----------------------------------------------------


def _store(title: str) -> uuid.UUID:
    with SessionLocal() as session:
        tender = TenderRepository(session).create(
            {
                "dedupe_key": f"test:{uuid.uuid4()}",
                "portal_tender_id": f"2027_T_{uuid.uuid4().hex[:8]}_1",
                "work_title": title,
                "location": "Maharashtra",
                "region": "Maharashtra",
                "closing_at": NOW + timedelta(days=30),
                "source_type": "official_portal",
                "extracted_from": "listing_table",
                "created_at": NOW,
            }
        )
        session.commit()
        return tender.id


def test_classification_is_written_once_and_reused():
    _store("Construction of four lane road on NH-48")
    _store("Housekeeping of a district hospital")

    with SessionLocal() as session:
        service = CorpusIntelligenceService(session)
        first = service.classify_pending(limit=50)
        second = service.classify_pending(limit=50)
        rows = session.scalars(select(TenderClassification)).all()

    assert first == 2
    # Stamped with the content hash and the rules version, so the second pass
    # has nothing to do.
    assert second == 0
    assert len(rows) == 2
    assert {row.rules_version for row in rows} == {CLASSIFIER_VERSION}
    assert all(row.input_hash for row in rows)


def test_a_changed_title_is_reclassified():
    tender_id = _store("Construction of four lane road")
    with SessionLocal() as session:
        CorpusIntelligenceService(session).classify_pending(limit=10)

    with SessionLocal() as session:
        tender = session.get(Tender, tender_id)
        tender.work_title = "Housekeeping of a district hospital"
        session.commit()

    with SessionLocal() as session:
        written = CorpusIntelligenceService(session).classify_pending(limit=10)

    assert written == 1


# -- the per-company verdict ----------------------------------------------


def _company(session, *, services=("road",), keywords=()):
    return CompanyRepository(session).create(
        name=f"Co {uuid.uuid4().hex[:6]}",
        services=list(services),
        sectors=[],
        keywords=list(keywords),
        region="Maharashtra",
    )


def test_verdicts_are_stored_with_the_hash_of_their_inputs():
    _store("Road resurfacing of NH-48")
    with SessionLocal() as session:
        company = _company(session)
        service = CorpusIntelligenceService(session)
        written = service.refresh_verdicts(limit=50)
        again = service.refresh_verdicts(limit=50)
        rows = session.scalars(select(CompanyTenderVerdict)).all()

    assert written >= 1
    assert again == 0
    assert len(rows) == 1
    assert rows[0].bucket in {"strong", "possible", "hidden"}
    assert rows[0].rules_version == RULES_VERSION
    assert rows[0].source == "rules"


def test_feedback_invalidates_the_verdict():
    tender_id = _store("Road resurfacing of NH-48")
    with SessionLocal() as session:
        company = _company(session)
        CorpusIntelligenceService(session).refresh_verdicts(limit=50)
        FeedbackRepository(session).upsert(
            company.id, tender_id, verdict="not_relevant", matched_terms=["road"]
        )
        session.commit()

    with SessionLocal() as session:
        written = CorpusIntelligenceService(session).refresh_verdicts(limit=50)

    assert written >= 1


def test_the_list_reads_the_stored_verdict_instead_of_recomputing():
    _store("Road resurfacing of NH-48")
    with SessionLocal() as session:
        company = _company(session, services=["road"])
        CorpusIntelligenceService(session).refresh_verdicts(limit=50)
        session.commit()
        company_id = company.id

    # A stored model-sourced verdict is what the list reads. A fresh rules pass
    # would stamp source="rules"; seeing "model" proves the cache was used.
    with SessionLocal() as session:
        verdict = session.scalars(select(CompanyTenderVerdict)).first()
        verdict.source = "model"
        verdict.reasons = ["The model judged this work relevant."]
        session.commit()

    with SessionLocal() as session:
        company = CompanyRepository(session).get(company_id)
        service = TenderService(session)
        rows, total = service.list(company=company)

    assert total == 1
    assert service.last_verdicts[rows[0].id]["source"] == "model"
    assert service.last_verdicts[rows[0].id]["reasons"] == [
        "The model judged this work relevant."
    ]


# -- the bounded adjudication ---------------------------------------------


class FakeProvider:
    configured = True

    def __init__(self, answers: dict[str, str]) -> None:
        self.answers = answers
        self.calls: list[str] = []

    def complete_json(self, *, system: str, user: str) -> dict:
        self.calls.append(user)
        decisions = []
        for identifier, answer in self.answers.items():
            decisions.append({"id": identifier, "relevant": answer, "reason": "test"})
        return {"decisions": decisions}


class AnchorProvider:
    """A provider that returns the anchor too, so promotion can be checked."""

    configured = True

    def __init__(self, answers: dict[str, tuple[str, str]]) -> None:
        self.answers = answers

    def complete_json(self, *, system: str, user: str) -> dict:
        return {
            "decisions": [
                {
                    "id": identifier,
                    "relevant": answer,
                    "anchor": anchor,
                    "reason": "test",
                }
                for identifier, (answer, anchor) in self.answers.items()
            ]
        }


class BrokenProvider:
    configured = True

    def complete_json(self, *, system: str, user: str) -> dict:
        from app.integrations.ai import AIError

        raise AIError("upstream is down")


def test_an_unconfigured_provider_leaves_everything_unknown():
    items = [{"id": "one", "title": "Road work", "matched": ["road"]}]
    answers = adjudicate(None, declared=["road"], items=items)
    assert answers["one"]["relevant"] == "unknown"


def test_a_provider_error_shows_the_tender_rather_than_hiding_it():
    items = [{"id": "one", "title": "Road work", "matched": ["road"]}]
    answers = adjudicate(BrokenProvider(), declared=["road"], items=items)
    assert answers["one"]["relevant"] == "unknown"


def test_unknown_ids_and_odd_answers_are_ignored():
    class SneakyProvider:
        configured = True

        def complete_json(self, *, system: str, user: str) -> dict:
            return {
                "decisions": [
                    {"id": "one", "relevant": "no", "reason": "hospital"},
                    {"id": "not-asked", "relevant": "no", "reason": "injected"},
                    {"id": "two", "relevant": "maybe", "reason": "unclear"},
                ]
            }

    items = [
        {"id": "one", "title": "Housekeeping", "matched": ["road"]},
        {"id": "two", "title": "Road work", "matched": ["road"]},
    ]
    answers = adjudicate(SneakyProvider(), declared=["road"], items=items)

    assert answers["one"]["relevant"] == "no"
    # Not asked about, so it does not exist in the answer at all.
    assert "not-asked" not in answers
    # Anything that is not yes/no/unknown is unknown, which is shown.
    assert answers["two"]["relevant"] == "unknown"


def test_adjudication_demotes_a_rejected_row_and_stores_its_source():
    tender_id = _store("Housekeeping of a district hospital")
    with SessionLocal() as session:
        company = _company(session, services=["road"])
        CorpusIntelligenceService(session).classify_pending(limit=50)
        CorpusIntelligenceService(session).refresh_verdicts(limit=50)
        # The rules call this hidden already; give it a possible verdict so the
        # model has something to judge.
        verdict = session.scalars(select(CompanyTenderVerdict)).first()
        verdict.bucket = "possible"
        verdict.tier = 6
        verdict.source = "rules"
        session.commit()

    provider = FakeProvider({str(tender_id): "no"})
    with SessionLocal() as session:
        metrics = CorpusIntelligenceService(session).adjudicate_possible(
            provider, limit=10, batch_size=10, max_batches=1
        )

    assert metrics["batches"] == 1
    assert metrics["judged"] == 1
    # A model "no" is a demotion, never a silent hide: the human can still
    # disagree with the model.
    assert metrics["hidden"] == 0
    assert metrics["demoted"] == 1
    with SessionLocal() as session:
        verdict = session.scalars(
            select(CompanyTenderVerdict).where(
                CompanyTenderVerdict.tender_id == tender_id
            )
        ).one()
    assert verdict.bucket == "possible"
    assert verdict.tier == 6
    assert 0.40 <= verdict.score <= 0.48
    assert verdict.source == "model"
    assert any("not relevant" in reason for reason in verdict.reasons)


def test_adjudication_promotes_a_yes_only_when_the_anchor_is_in_the_notice():
    tender_id = _store("Road resurfacing of NH-48")
    with SessionLocal() as session:
        _company(session, services=["road"])
        CorpusIntelligenceService(session).classify_pending(limit=50)
        CorpusIntelligenceService(session).refresh_verdicts(limit=50)

    provider = AnchorProvider({str(tender_id): ("yes", "Road resurfacing")})
    with SessionLocal() as session:
        CorpusIntelligenceService(session).adjudicate_possible(
            provider, limit=10, batch_size=10, max_batches=1
        )

    with SessionLocal() as session:
        verdict = session.scalars(
            select(CompanyTenderVerdict).where(
                CompanyTenderVerdict.tender_id == tender_id
            )
        ).one()
    assert verdict.tier in (1, 2)
    assert verdict.bucket == "strong"
    assert verdict.source == "model"


def test_an_unanchored_ai_yes_does_not_promote_the_row():
    tender_id = _store("Road resurfacing of NH-48")
    with SessionLocal() as session:
        _company(session, services=["road"])
        CorpusIntelligenceService(session).classify_pending(limit=50)
        CorpusIntelligenceService(session).refresh_verdicts(limit=50)
        before = session.scalars(
            select(CompanyTenderVerdict).where(
                CompanyTenderVerdict.tender_id == tender_id
            )
        ).one()
        before_tier = before.tier

    provider = AnchorProvider({str(tender_id): ("yes", "bridge construction")})
    with SessionLocal() as session:
        CorpusIntelligenceService(session).adjudicate_possible(
            provider, limit=10, batch_size=10, max_batches=1
        )

    with SessionLocal() as session:
        verdict = session.scalars(
            select(CompanyTenderVerdict).where(
                CompanyTenderVerdict.tender_id == tender_id
            )
        ).one()
    assert verdict.tier == before_tier
    assert verdict.bucket == "strong"
    assert any("not promoted" in reason for reason in verdict.reasons)


class CandidateProvider:
    """Answers the similarity prompt, not the legacy adjudication prompt."""

    configured = True

    def complete_json(self, *, system: str, user: str) -> dict:
        ids = re.findall(r"tender_id: ([0-9a-f-]{36})", user)
        return {
            "judgments": [
                {
                    "tender_id": tender_id,
                    "title": "Road resurfacing of NH-48",
                    "answer": "not_sure",
                    "tender_evidence": "",
                    "company_evidence": "",
                    "match_type": "no_evidence",
                    "reason": "test",
                }
                for tender_id in ids
            ]
        }


def test_all_tier_adjudication_judges_a_strong_rules_row():
    tender_id = _store("Road resurfacing of NH-48")
    with SessionLocal() as session:
        company = _company(session, services=["road"])
        CorpusIntelligenceService(session).classify_pending(limit=50)
        CorpusIntelligenceService(session).refresh_verdicts(limit=50)
        verdict = session.scalars(
            select(CompanyTenderVerdict).where(
                CompanyTenderVerdict.company_id == company.id
            )
        ).one()
        assert verdict.tier == 3
        assert verdict.bucket == "strong"

    with SessionLocal() as session:
        metrics = CorpusIntelligenceService(session).adjudicate_candidates(
            CandidateProvider(), limit=10, batch_size=10, max_batches=1
        )

    # The new pass is not limited to the ambiguous residue: a tier-3 row is
    # shown to the model too, without the tier in the prompt.
    assert metrics["judged"] == 1
    with SessionLocal() as session:
        judgment = session.scalars(
            select(TenderSimilarityJudgment).where(
                TenderSimilarityJudgment.tender_id == tender_id
            )
        ).one()
    assert judgment.answer == "unsure"
    assert judgment.match_type == "no_evidence"
    assert judgment.evidence_rank == 6
