// Small, pure presentation helpers for the v2 tender shape.  The backend keeps
// `work_title`/`reference`/`closing_at`; the UI's older labels are derived here
// rather than duplicated in every page.

export function tenderStatus(tender) {
  if (!tender) return "unknown";
  if (tender.cycle_state === "terminated") return "terminated";
  if (tender.status) return tender.status;
  if (!tender.closing_at) return "unknown";
  const closing = new Date(tender.closing_at);
  if (Number.isNaN(closing.getTime())) return "unknown";
  return closing.getTime() < Date.now() ? "closed" : "open";
}

export function tenderTitle(tender) {
  return tender?.work_title || tender?.title || "—";
}

export function tenderReference(tender) {
  return tender?.reference || tender?.reference_number || null;
}

export function tenderDeadline(tender) {
  return tender?.closing_at || tender?.deadline || null;
}

export function tenderSummary(tender) {
  return [tender?.work_type, tender?.location].filter(Boolean).join(" · ");
}

export function matchedTerms(tender) {
  return tender?.relevance?.matched_terms || [];
}
