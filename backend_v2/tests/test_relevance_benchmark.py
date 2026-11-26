"""The synthetic relevance benchmark is a regression guard, not a demo.

It pins two things a later "improvement" could quietly break:

* the labelled core work ranks above adjacent/support work;
* the listing gate does not hide labelled work while showing unrelated rows.

The fixture is synthetic and clearly labelled. It is never seeded into a
database; the script and this test only call the deterministic classifier.
"""

from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.benchmark_relevance import evaluate_fixture, load_fixture  # noqa: E402


def test_the_labelled_corpus_keeps_core_work_ahead_of_adjacent_work():
    result = evaluate_fixture(load_fixture())

    assert len(result["companies"]) == 2
    for company in result["companies"]:
        assert company["listed"] >= 20
        assert company["precision_at_10"] >= 0.9, company
        assert company["precision_at_20"] >= 0.9, company
        assert company["ndcg_at_20"] >= 0.9, company
        assert company["top20_mean_grade"] >= 1.4, company
        # An unrelated row that is listed is a false positive; the gate's
        # contract is to keep it out, so a regression should show up here.
        assert company["visible_irrelevant"] == 0, company
        # A labelled row the gate hides is a recall failure. One bridge-repair
        # row sits at the support-only edge and is the only allowed miss.
        assert company["hidden_relevant"] <= 1, company

    assert result["macro"]["precision_at_10"] >= 0.9
    assert result["macro"]["precision_at_20"] >= 0.9
    assert result["macro"]["ndcg_at_20"] >= 0.9


def test_the_benchmark_reports_both_sides_of_the_gate():
    result = evaluate_fixture(load_fixture())
    for company in result["companies"]:
        assert "precision_at_10" in company
        assert "precision_at_20" in company
        assert "ndcg_at_20" in company
        assert "visible_irrelevant" in company
        assert "hidden_relevant" in company
        assert len(company["rows"]) == company["listed"]
