// Deterministic presentation helpers for the v2 requirement shape.

export const REQUIREMENT_CATEGORIES = [
  "eligibility",
  "submission",
  "compliance",
  "form",
  "performance",
];

export const REQUIREMENT_STATES = [
  "verified",
  "unverified",
  "needs_review",
  "accepted_ocr",
  "rejected",
];

// A human may move a requirement to one of these; `verified` is decided only by
// the deterministic citation gate and is never set from the UI.
export const REVIEW_STATES = ["needs_review", "accepted_ocr", "rejected"];

export const MANDATORY_STATES = ["mandatory", "not_mandatory", "unknown"];

export const EVIDENCE_STATES = [
  "none",
  "suggested",
  "covered",
  "missing",
  "expired",
  "contradicted",
];

export function requirementQuery(filters = {}) {
  const params = new URLSearchParams();
  if (filters.category) params.set("category", filters.category);
  for (const state of filters.state || []) {
    if (state) params.append("state", state);
  }
  if (filters.include_navigation) params.set("include_navigation", "true");
  if (filters.include_inactive) params.set("include_inactive", "true");
  if (filters.include_ignored) params.set("include_ignored", "true");
  const query = params.toString();
  return query ? `?${query}` : "";
}

export function pageReference(requirement) {
  if (!requirement) return { viewer: 0, printed: "" };
  const viewer = Number(requirement.page_index) || 0;
  const printed = String(requirement.printed_label ?? "").trim();
  if (printed && printed !== String(viewer)) {
    return { viewer, printed };
  }
  return { viewer, printed: "" };
}

export function verificationTone(state) {
  if (state === "verified") return "green";
  if (state === "accepted_ocr") return "teal";
  if (state === "unverified") return "amber";
  if (state === "needs_review") return "amber";
  if (state === "rejected") return "red";
  return "neutral";
}

export function categoryTone(category) {
  if (category === "eligibility") return "teal";
  if (category === "compliance") return "amber";
  if (category === "submission") return "blue";
  if (category === "form") return "blue";
  if (category === "performance") return "neutral";
  return "neutral";
}

export function summarizeRequirements(items = []) {
  return items.reduce(
    (summary, item) => {
      summary.total += 1;
      if (item.state === "verified") summary.verified += 1;
      if (item.state === "accepted_ocr") summary.accepted_ocr += 1;
      if (item.state === "needs_review") summary.needs_review += 1;
      if (item.state === "unverified") summary.unverified += 1;
      if (item.state === "rejected") summary.rejected += 1;
      return summary;
    },
    {
      total: 0,
      verified: 0,
      accepted_ocr: 0,
      needs_review: 0,
      unverified: 0,
      rejected: 0,
    },
  );
}
