"""Batched AI adjudication of the ambiguous residue, and nothing else.

The deterministic matcher decides most of the corpus. What is left is the
genuinely ambiguous part: a tender whose only match is one common word. Asking
a model about those rows is useful, but it has to be *bounded*:

* the model is asked in batches with ids, and answers with ids, so a bad answer
  can only affect the rows it named;
* every answer is validated, and anything missing, malformed or unexpected
  becomes ``unknown``, which is shown, not hidden;
* the model may only answer "is this the kind of work the company says it
  does". It is never asked about eligibility, money, deadlines or whether the
  company can perform, and its answer never passes an eligibility gate;
* a provider that is not configured, or an error, leaves every candidate
  ``unknown`` rather than hiding a tender.

The caller stores each answer as a model-sourced verdict, so the read path
still only reads rows, and nothing on a request path calls a model.
"""

from __future__ import annotations

from typing import Any

from app.integrations.ai import AIError

SYSTEM_PROMPT = (
    "You help match government tenders to a company's declared work.\n"
    "You are given the company's declared services, sectors and keywords, and "
    "a batch of tender titles with the declared words each one matched.\n"
    "Decide only whether each tender's WORK is the kind of work the company "
    "says it does.\n"
    "Rules:\n"
    "- Answer only with JSON: "
    '{"decisions":[{"id":"<id>","relevant":"yes|no|unknown",'
    '"anchor":"<exact words copied from that title>","reason":"<short>"}]}\n'
    "- Use every id you were given, once, and no other id.\n"
    "- The anchor must be copied exactly from the title, never paraphrased. If "
    "you cannot copy a phrase, answer \"unknown\".\n"
    "- Answer \"unknown\" when the title does not make the work clear. Do not "
    "guess.\n"
    "- Never judge eligibility, licences, turnover, deadlines, value or whether "
    "the company can perform the work. Those are not your question.\n"
    "- Do not answer based on the issuing authority: a department's name says "
    "nothing about what the work is.\n"
)

VALID_ANSWERS = ("yes", "no", "unknown")


def build_user_prompt(
    declared: list[str], items: list[dict[str, Any]]
) -> str:
    lines = [
        "Company declares this work:",
        "\n".join(f"- {term}" for term in declared[:60]) or "- (nothing declared)",
        "",
        "Tenders to judge:",
    ]
    for item in items:
        matched = ", ".join(item.get("matched") or []) or "none"
        lines.append(
            f"id={item['id']} | title={item.get('title') or ''} | matched={matched}"
        )
    return "\n".join(lines)


def _clean_answer(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in {"yes", "true", "relevant", "1"}:
        return "yes"
    if text in {"no", "false", "not_relevant", "irrelevant", "0"}:
        return "no"
    return "unknown"


def adjudicate(
    provider,
    *,
    declared: list[str],
    items: list[dict[str, Any]],
) -> dict[str, dict[str, str]]:
    """Ask the provider about one batch; always return one answer per id.

    The default is ``unknown`` for every row. A row only gains a ``yes`` or
    ``no`` when the provider named its id and used one of the three words.
    """

    decisions: dict[str, dict[str, str]] = {
        str(item["id"]): {
            "relevant": "unknown",
            "anchor": "",
            "reason": "Not judged.",
        }
        for item in items
    }
    if not items or provider is None or not getattr(provider, "configured", False):
        return decisions

    try:
        payload = provider.complete_json(
            system=SYSTEM_PROMPT,
            user=build_user_prompt(declared, items),
        )
    except AIError:
        return decisions

    rows = payload.get("decisions") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return decisions
    for row in rows:
        if not isinstance(row, dict):
            continue
        identifier = str(row.get("id") or "")
        if identifier not in decisions:
            # An id that was not asked about is ignored, not trusted.
            continue
        answer = _clean_answer(row.get("relevant"))
        if answer not in VALID_ANSWERS:
            answer = "unknown"
        anchor = str(row.get("anchor") or "")[:300]
        reason = str(row.get("reason") or "")[:300]
        decisions[identifier] = {
            "relevant": answer,
            "anchor": anchor,
            "reason": reason,
        }
    return decisions
