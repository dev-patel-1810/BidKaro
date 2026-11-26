// Pure helpers for the discovery UI. Kept free of React so they can be unit tested.

export function scorePercent(score) {
  const value = Number(score);
  if (!Number.isFinite(value)) return 0;
  // A tender match is never a guarantee, so the UI never reads 100%. The
  // backend caps its score bands at 0.95 too; this is the display guarantee.
  return Math.min(95, Math.round(Math.min(Math.max(value, 0), 1) * 100));
}

// The fit ladder's rung, toned like the bucket it belongs to: the top three
// rungs are strong evidence, the next three are possible, and the last is the
// "could not tell" row shown only when the reader asks for low confidence.
export function tierTone(tier) {
  const value = Number(tier);
  if (!Number.isFinite(value)) return "neutral";
  if (value >= 1 && value <= 3) return "green";
  if (value >= 4 && value <= 6) return "amber";
  return "neutral";
}

export function scoreTone(score) {
  const percent = scorePercent(score);
  if (percent >= 70) return "green";
  if (percent >= 45) return "teal";
  if (percent >= 30) return "amber";
  return "neutral";
}

// The kind of page a source is: a national e-procurement portal, a government
// body's own site, a commercial aggregator, or a manual upload.
export function sourceKindTone(sourceKind) {
  if (sourceKind === "official_portal") return "teal";
  if (sourceKind === "authority") return "green";
  if (sourceKind === "aggregator") return "blue";
  return "neutral";
}

// The list order comes from the server (arrival order by default, or the
// order the reader picked). The browser never re-sorts it: a client-side
// relevance sort would bury a tender fetched today under an older one.
//
// The one sort vocabulary both lists use. "match_desc" is the company's fit,
// best first, with the newest arrival breaking a tie: a 55% fit read last week
// leads a 50% fit read today. It is the default because the company came for
// the tenders that look like its work.
export const TENDER_SORT_OPTIONS = [
  "match_desc",
  "created_desc",
  "created_asc",
  "closing_asc",
  "closing_desc",
];
export const DEFAULT_TENDER_SORT = "match_desc";

// The stable eProcurement address. A notice opened from the portal carries a
// session token in its address and stops working when that session ends, so the
// card links here and asks the user to search by the tender reference instead.
export const EPROCUREMENT_HOMEPAGE = "https://etenders.gov.in/eprocure/app";

const EPROCUREMENT_HOSTS = new Set([
  "etenders.gov.in",
  "www.etenders.gov.in",
  "eprocure.gov.in",
  "www.eprocure.gov.in",
]);

export function isEprocurementSource(url) {
  if (typeof url !== "string" || !url) return false;
  try {
    return EPROCUREMENT_HOSTS.has(new URL(url).hostname.toLowerCase());
  } catch {
    return false;
  }
}

// Where a card should send the user. The session-bound notice address is never
// returned here: it is kept on the record as provenance, not shown as a link.
export function tenderSourceLink(item) {
  const href = typeof item?.source_url === "string" ? item.source_url : "";
  if (isEprocurementSource(href)) {
    return { href: EPROCUREMENT_HOMEPAGE, stable: true };
  }
  return { href, stable: false };
}

// A label that can never carry the session token: a known site name, then a
// bare hostname, and only then the address itself for other sources.
export function tenderSourceLabel(item) {
  if (item?.site_name) return item.site_name;
  const link = tenderSourceLink(item);
  if (!link.href) return "";
  if (link.stable) return "etenders.gov.in";
  try {
    return new URL(link.href).hostname;
  } catch {
    return link.href;
  }
}

// A v2 job is still working while it is queued, running or waiting for a human.
export function runIsActive(run) {
  const state = run?.state ?? run?.status;
  return state === "queued" || state === "running" || state === "needs_you";
}

// The AI work comparison is a second signal on top of the rules gate. It may
// only say "same family of work", and an answer whose quote is not found in
// the notice is a suggestion the human still has to confirm.
export function similarityTone(similarity) {
  if (!similarity) return "neutral";
  // A suggestion still waiting for the human is amber even when the model
  // said yes; only an anchored, unqualified yes is a green finding.
  if (similarity.needs_confirmation) return "amber";
  if (similarity.answer === "yes" && similarity.anchor_verified) return "green";
  if (similarity.answer === "yes") return "amber";
  return "neutral";
}

export function similarityLabelKey(similarity) {
  if (!similarity) return null;
  if (similarity.needs_confirmation) return "discover.similarity.suggested";
  if (similarity.answer === "yes" && similarity.anchor_verified) {
    return "discover.similarity.sameWork";
  }
  if (similarity.answer === "yes") return "discover.similarity.suggested";
  if (similarity.answer === "no") return "discover.similarity.differentWork";
  return "discover.similarity.unsure";
}

// The evidence hierarchy is deterministic (it comes from the matched company
// line's role, not from the model's confidence), so the UI can tone it.
const MATCH_TYPE_TONES = {
  exact_core: "green",
  semantic_core: "green",
  adjacent: "teal",
  support_only: "amber",
  generic_only: "neutral",
  no_evidence: "neutral",
};

export function matchTypeTone(matchType) {
  return MATCH_TYPE_TONES[matchType] || "neutral";
}

export function matchTypeLabelKey(matchType) {
  if (!matchType || !MATCH_TYPE_TONES[matchType]) return null;
  return `discover.similarity.matchTypes.${matchType}`;
}

// The gate's three-way answer, shown as it is so a "possible match" card never
// looks like the gate was certain. "hidden" only appears when the reader asked
// to see everything, and then it says why the row was out of the list.
const BUCKET_TONES = {
  strong: "green",
  possible: "amber",
  hidden: "neutral",
};

export function bucketTone(bucket) {
  return BUCKET_TONES[bucket] || "neutral";
}

export function bucketLabelKey(bucket) {
  if (bucket === "strong") return "discover.bucket.strong";
  if (bucket === "hidden") return "discover.bucket.hidden";
  return "discover.bucket.possible";
}

// A sweep is a background job with no human handoff: it is never "needs_you".
export function sweepIsActive(sweep) {
  const state = sweep?.state;
  return state === "queued" || state === "running";
}

// The counts the worker reported, in the order a reader wants them. Missing
// numbers are left out rather than shown as zero, because "0 pages" reads like
// a failed sweep when the job simply has not written its result yet.
export function sweepSummary(summary) {
  const metrics = summary?.metrics || {};
  const count = (value) => (Number.isFinite(Number(value)) ? Number(value) : null);
  return {
    pages: count(metrics.pages),
    organisations: count(metrics.organisations_walked),
    rows: count(metrics.rows_seen),
    stored: count(metrics.stored),
    updated: count(metrics.updated),
    rejected: count(metrics.rejected),
    notes: typeof summary?.notes === "string" ? summary.notes : "",
    state: summary?.state || null,
  };
}

