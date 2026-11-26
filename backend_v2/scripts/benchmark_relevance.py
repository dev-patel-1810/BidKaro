"""Measure read-time fit ranking on a labelled corpus.

The production list is a rules verdict plus an optional AI adjustment. This
benchmark deliberately measures the deterministic rules layer only: it is the
part that can be rerun without a model key and the part the AI may adjust but
never replace. The fixture is synthetic and clearly labelled; it is not a
sample of real tenders and it is never seeded into a database.

Grades:
  2 = core work (the company's confirmed kind of work);
  1 = adjacent or support work (worth reading, not a core match);
  0 = not the company's work.

Metrics:
  * precision@10 and precision@20 count grade >= 1 in the first k listed rows;
  * nDCG@20 uses gains 2**grade - 1, so a core row ranks above an adjacent one;
  * top-20 mean grade is a plain quality read on the same window;
  * hidden_relevant counts labelled work the gate would not show at all.

Run:
  E:\\BidKaro\\.venv\\Scripts\\python.exe scripts\\benchmark_relevance.py
  ... --json
  ... --include-hidden
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.discovery.relevance import classify  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "relevance_benchmark.json"
BUCKET_ORDER = {"strong": 0, "possible": 1, "hidden": 2}


def load_fixture(path: Path = FIXTURE) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _company(entry: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(
        services=list(entry.get("services") or []),
        sectors=list(entry.get("sectors") or []),
        keywords=list(entry.get("keywords") or []),
        understanding=list(entry.get("understanding") or []),
        region=entry.get("region"),
        profile={},
    )


def _tender(item: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(
        work_title=item["title"],
        work_type=None,
        authority=item.get("authority"),
        organisation_chain=item.get("organisation_chain"),
        location=item.get("location") or "",
        region=item.get("location") or "",
        reference=item.get("reference"),
        source_type="official_portal",
    )


def rank(company_entry: dict[str, Any], *, include_hidden: bool = False):
    company = _company(company_entry)
    rows = []
    for item in company_entry.get("tenders") or []:
        verdict = classify(company, _tender(item))
        if verdict["bucket"] == "hidden" and not include_hidden:
            continue
        rows.append(
            {
                "id": item.get("id") or item["title"],
                "title": item["title"],
                "grade": int(item.get("grade") or 0),
                "bucket": verdict["bucket"],
                "tier": verdict["tier"],
                "score": verdict["score"],
                "matched_terms": verdict["matched_terms"],
            }
        )
    # The default list order is fit descending, with the newest arrival breaking
    # a tie. The fixture has no arrival times, so the title is the stable tie-break.
    rows.sort(
        key=lambda row: (
            -float(row["score"]),
            int(row["tier"]),
            BUCKET_ORDER.get(row["bucket"], 9),
            row["title"].casefold(),
        )
    )
    return rows


def precision_at(rows: list[dict[str, Any]], k: int) -> float:
    window = rows[:k]
    if not window:
        return 0.0
    return sum(1 for row in window if row["grade"] >= 1) / min(k, len(window))


def ndcg_at(rows: list[dict[str, Any]], k: int) -> float:
    window = rows[:k]
    if not window:
        return 0.0

    def dcg(values: list[int]) -> float:
        return sum(
            (2 ** grade - 1) / math.log2(index + 2)
            for index, grade in enumerate(values)
        )

    ideal = sorted((row["grade"] for row in rows), reverse=True)[:k]
    best = dcg(ideal)
    if best <= 0:
        return 0.0
    return dcg([row["grade"] for row in window]) / best


def evaluate(company_entry: dict[str, Any], *, include_hidden: bool = False) -> dict[str, Any]:
    rows = rank(company_entry, include_hidden=include_hidden)
    top20 = rows[:20]
    relevant = [row for row in rows if row["grade"] >= 1]
    hidden_relevant = 0
    if not include_hidden:
        all_rows = rank(company_entry, include_hidden=True)
        hidden_relevant = sum(
            1 for row in all_rows if row["bucket"] == "hidden" and row["grade"] >= 1
        )
    return {
        "id": company_entry.get("id"),
        "label": company_entry.get("label"),
        "tenders": len(company_entry.get("tenders") or []),
        "listed": len(rows),
        "relevant": len(relevant),
        "visible_irrelevant": sum(1 for row in rows if row["grade"] == 0),
        "hidden_relevant": hidden_relevant,
        "precision_at_10": round(precision_at(rows, 10), 4),
        "precision_at_20": round(precision_at(rows, 20), 4),
        "ndcg_at_20": round(ndcg_at(rows, 20), 4),
        "top20_relevant_share": round(
            sum(1 for row in top20 if row["grade"] >= 1) / len(top20), 4
        )
        if top20
        else 0.0,
        "top20_mean_grade": round(
            sum(row["grade"] for row in top20) / len(top20), 4
        )
        if top20
        else 0.0,
        "top20_strong_share": round(
            sum(1 for row in top20 if row["tier"] <= 3) / len(top20), 4
        )
        if top20
        else 0.0,
        "rows": rows,
    }


def evaluate_fixture(
    fixture: dict[str, Any], *, include_hidden: bool = False
) -> dict[str, Any]:
    companies = [
        evaluate(entry, include_hidden=include_hidden)
        for entry in fixture.get("companies") or []
    ]
    keys = (
        "precision_at_10",
        "precision_at_20",
        "ndcg_at_20",
        "top20_relevant_share",
        "top20_mean_grade",
        "top20_strong_share",
    )
    macro = {
        key: round(sum(item[key] for item in companies) / len(companies), 4)
        if companies
        else 0.0
        for key in keys
    }
    return {
        "fixture": fixture.get("name"),
        "labelled": "synthetic; not a sample of real tenders",
        "companies": companies,
        "macro": macro,
    }


def render(result: dict[str, Any]) -> str:
    lines = [
        f"Relevance benchmark: {result['fixture']} ({result['labelled']})",
        "",
        f"{'company':<24} {'listed':>6} {'p@10':>6} {'p@20':>6} "
        f"{'ndcg@20':>8} {'mean20':>7} {'strong20':>8} {'false+':>7} {'hidden+':>8}",
    ]
    for item in result["companies"]:
        lines.append(
            f"{str(item['label'] or item['id'])[:24]:<24} "
            f"{item['listed']:>6} {item['precision_at_10']:>6.2f} "
            f"{item['precision_at_20']:>6.2f} {item['ndcg_at_20']:>8.2f} "
            f"{item['top20_mean_grade']:>7.2f} {item['top20_strong_share']:>8.2f} "
            f"{item['visible_irrelevant']:>7} {item['hidden_relevant']:>8}"
        )
    macro = result["macro"]
    lines.append(
        f"{'macro average':<24} {'':>6} {macro['precision_at_10']:>6.2f} "
        f"{macro['precision_at_20']:>6.2f} {macro['ndcg_at_20']:>8.2f} "
        f"{macro['top20_mean_grade']:>7.2f} {macro['top20_strong_share']:>8.2f}"
    )
    lines.append("")
    lines.append("Grades: 2 core, 1 adjacent/support, 0 not the company's work.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, default=FIXTURE)
    parser.add_argument("--json", action="store_true", help="Print metrics as JSON.")
    parser.add_argument(
        "--include-hidden",
        action="store_true",
        help="Rank every row, including the rows the list would hide.",
    )
    parser.add_argument(
        "--fail-under",
        type=float,
        default=None,
        help="Exit 2 when macro precision@10 is below this value.",
    )
    args = parser.parse_args(argv)
    result = evaluate_fixture(load_fixture(args.fixture), include_hidden=args.include_hidden)
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(render(result))
    if args.fail_under is not None and result["macro"]["precision_at_10"] < args.fail_under:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
