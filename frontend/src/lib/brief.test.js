import { describe, expect, it } from "vitest";

import {
  briefCounts,
  briefIsStored,
  briefPayload,
  briefVersion,
  draftFromPayload,
  payloadFromDraft,
  termsToText,
} from "./brief";

const stored = {
  brief: {
    id: "brief-1",
    version: 3,
    payload: {
      summary: "Scanning and digitisation",
      core_terms: [
        {
          term: "document scanning",
          role: "core",
          source: "capability",
          evidence: "We scan court records.",
          page_index: 2,
        },
      ],
      adjacent_terms: [{ term: "web solutions", role: "adjacent" }],
      support_terms: [{ term: "technical support", role: "support" }],
      generic_terms: [{ term: "technology", role: "generic" }],
      projects: [{ title: "Court record digitisation", work_family: "digitisation" }],
      certifications: [{ title: "ISO 9001", document_type: "iso" }],
      rules_version: "2026-11-22.1",
    },
  },
  terms: {},
  rules_version: "2026-11-22.1",
};

const preview = {
  brief: null,
  preview: { payload: stored.brief.payload, source_kind: "derived", version: null },
  terms: {},
  rules_version: "2026-11-22.1",
};

describe("company brief helpers", () => {
  it("reads a stored brief and falls back to the preview payload", () => {
    expect(briefPayload(stored)).toBe(stored.brief.payload);
    expect(briefPayload(preview)).toBe(stored.brief.payload);
    expect(briefPayload(null)).toBeNull();
    expect(briefIsStored(stored)).toBe(true);
    expect(briefIsStored(preview)).toBe(false);
    expect(briefVersion(stored)).toBe(3);
    expect(briefVersion(preview)).toBeNull();
  });

  it("turns the four role groups into editable lines", () => {
    const draft = draftFromPayload(stored.brief.payload);
    expect(draft.summary).toBe("Scanning and digitisation");
    expect(draft.core).toBe("document scanning");
    expect(draft.adjacent).toBe("web solutions");
    expect(draft.support).toBe("technical support");
    expect(draft.generic).toBe("technology");
    expect(termsToText([{ term: "a" }, "b", { term: "" }])).toBe("a\nb");
  });

  it("keeps an untouched citation and marks a new term as human", () => {
    const draft = draftFromPayload(stored.brief.payload);
    draft.core = "document scanning\nExamination processing";
    draft.support = "technical support";

    const payload = payloadFromDraft(draft, stored.brief.payload);

    const kept = payload.core_terms.find((item) => item.term === "document scanning");
    expect(kept.evidence).toBe("We scan court records.");
    expect(kept.page_index).toBe(2);

    const added = payload.core_terms.find((item) => item.term === "Examination processing");
    expect(added).toMatchObject({ role: "core", source: "human" });

    // The project and certification lists are evidence, not edit fields.
    expect(payload.projects).toEqual(stored.brief.payload.projects);
    expect(payload.certifications).toEqual(stored.brief.payload.certifications);
  });

  it("removes blank and duplicate lines and moves a term to its new section", () => {
    const draft = draftFromPayload(stored.brief.payload);
    draft.support = "";
    draft.core = "web solutions\nweb solutions\n\ndocument scanning";

    const payload = payloadFromDraft(draft, stored.brief.payload);

    expect(payload.support_terms).toEqual([]);
    expect(payload.core_terms.map((item) => item.term)).toEqual([
      "web solutions",
      "document scanning",
    ]);
  });

  it("counts every role and the evidence lists", () => {
    expect(briefCounts(stored.brief.payload)).toEqual({
      core: 1,
      adjacent: 1,
      support: 1,
      generic: 1,
      projects: 1,
      certifications: 1,
    });
  });
});
