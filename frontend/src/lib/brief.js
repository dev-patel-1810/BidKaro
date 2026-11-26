// Pure helpers for the editable company brief. Kept free of React so the role
// grouping and the citation-preserving edit can be unit tested.

export const BRIEF_ROLES = ["core", "adjacent", "support", "generic"];

export const BRIEF_ROLE_KEYS = {
  core: "core_terms",
  adjacent: "adjacent_terms",
  support: "support_terms",
  generic: "generic_terms",
};

// The GET /company/brief response carries either a stored brief or a derived
// preview. Both have a payload, and the UI must not invent one when neither is
// present.
export function briefPayload(entry) {
  if (!entry) return null;
  if (entry.brief?.payload) return entry.brief.payload;
  if (entry.preview?.payload) return entry.preview.payload;
  return null;
}

export function briefIsStored(entry) {
  return Boolean(entry?.brief?.id);
}

export function briefVersion(entry) {
  return entry?.brief?.version ?? null;
}

function cleanLines(value) {
  const seen = new Set();
  const lines = [];
  for (const raw of String(value || "").split(/\r?\n/)) {
    const term = raw.trim();
    if (!term) continue;
    const key = term.toLowerCase().replace(/\s+/g, " ");
    if (seen.has(key)) continue;
    seen.add(key);
    lines.push(term);
  }
  return lines;
}

export function termsToText(items) {
  return (items || [])
    .map((item) => (typeof item === "string" ? item : item?.term))
    .filter(Boolean)
    .join("\n");
}

export function draftFromPayload(payload) {
  const draft = { summary: String(payload?.summary || "") };
  for (const role of BRIEF_ROLES) {
    draft[role] = termsToText(payload?.[BRIEF_ROLE_KEYS[role]]);
  }
  return draft;
}

function indexItems(items) {
  const index = new Map();
  for (const item of items || []) {
    if (!item || typeof item !== "object") continue;
    const term = String(item.term || "").trim();
    if (!term) continue;
    const key = term.toLowerCase().replace(/\s+/g, " ");
    if (!index.has(key)) index.set(key, item);
  }
  return index;
}

// Build the save payload from the four textareas. An untouched term keeps the
// citation it already had; a new or moved term becomes a human-declared item.
// The section the human typed it in is the role they chose, and the backend
// still downgrades a support/generic phrase that was put under core by mistake.
export function payloadFromDraft(draft, basePayload) {
  const payload = {
    summary: String(draft?.summary || "").trim(),
    projects: basePayload?.projects || [],
    certifications: basePayload?.certifications || [],
    rules_version: basePayload?.rules_version || null,
  };
  for (const role of BRIEF_ROLES) {
    const key = BRIEF_ROLE_KEYS[role];
    const existing = indexItems(basePayload?.[key]);
    payload[key] = cleanLines(draft?.[role]).map((term) => {
      const previous = existing.get(term.toLowerCase().replace(/\s+/g, " "));
      if (previous) return { ...previous, term, role };
      return { term, role, source: "human" };
    });
  }
  return payload;
}

export function briefCounts(payload) {
  const counts = {};
  for (const role of BRIEF_ROLES) {
    counts[role] = (payload?.[BRIEF_ROLE_KEYS[role]] || []).length;
  }
  counts.projects = (payload?.projects || []).length;
  counts.certifications = (payload?.certifications || []).length;
  return counts;
}

export const BRIEF_ROLE_GUIDE = {
  core: "documents.brief.roles.coreHint",
  adjacent: "documents.brief.roles.adjacentHint",
  support: "documents.brief.roles.supportHint",
  generic: "documents.brief.roles.genericHint",
};
