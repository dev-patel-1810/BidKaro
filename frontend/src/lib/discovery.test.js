import { describe, expect, it } from "vitest";

import en from "../i18n/locales/en.json";
import hi from "../i18n/locales/hi.json";

import {
  bucketLabelKey,
  bucketTone,
  DEFAULT_TENDER_SORT,
  isEprocurementSource,
  matchTypeLabelKey,
  matchTypeTone,
  runIsActive,
  scorePercent,
  scoreTone,
  similarityLabelKey,
  similarityTone,
  sourceKindTone,
  sweepIsActive,
  sweepSummary,
  TENDER_SORT_OPTIONS,
  tenderSourceLabel,
  tenderSourceLink,
  tierTone,
} from "./discovery";

describe("score helpers", () => {
  it("turns a 0..1 score into a percentage and tone", () => {
    expect(scorePercent(0.72)).toBe(72);
    expect(scorePercent(undefined)).toBe(0);
    // A perfect score never reads 100%: fit is not a guarantee.
    expect(scorePercent(1)).toBe(95);
    expect(scorePercent(1.5)).toBe(95);
    expect(scoreTone(0.72)).toBe("green");
    expect(scoreTone(0.5)).toBe("teal");
    expect(scoreTone(0.35)).toBe("amber");
    expect(scoreTone(0.1)).toBe("neutral");
  });

  it("tones each rung of the fit ladder", () => {
    expect(tierTone(1)).toBe("green");
    expect(tierTone(3)).toBe("green");
    expect(tierTone(4)).toBe("amber");
    expect(tierTone(6)).toBe("amber");
    expect(tierTone(7)).toBe("neutral");
    expect(tierTone(undefined)).toBe("neutral");
  });

  it("maps v2 source kinds to tones", () => {
    expect(sourceKindTone("official_portal")).toBe("teal");
    expect(sourceKindTone("authority")).toBe("green");
    expect(sourceKindTone("aggregator")).toBe("blue");
    expect(sourceKindTone("manual_upload")).toBe("neutral");
  });
});

describe("source links", () => {
  it("never returns a session-bound eProcurement notice address", () => {
    const link = tenderSourceLink({
      source_url: "https://etenders.gov.in/eprocure/app?session=secret",
    });
    expect(link).toEqual({
      href: "https://etenders.gov.in/eprocure/app",
      stable: true,
    });
    expect(isEprocurementSource("https://etenders.gov.in/eprocure/app")).toBe(true);
    expect(tenderSourceLabel({ source_url: "https://example.gov.in/notice" })).toBe(
      "example.gov.in",
    );
  });
});

describe("runIsActive", () => {
  it("treats queued, running and needs_you as active", () => {
    expect(runIsActive({ state: "queued" })).toBe(true);
    expect(runIsActive({ state: "running" })).toBe(true);
    expect(runIsActive({ state: "needs_you" })).toBe(true);
    expect(runIsActive({ state: "done" })).toBe(false);
    expect(runIsActive(null)).toBe(false);
  });
});

describe("relevance buckets", () => {
  it("tones a confident match differently from a possible one", () => {
    expect(bucketTone("strong")).toBe("green");
    expect(bucketTone("possible")).toBe("amber");
    expect(bucketTone("hidden")).toBe("neutral");
    expect(bucketTone(undefined)).toBe("neutral");
  });

  it("labels the bucket the gate actually used", () => {
    expect(bucketLabelKey("strong")).toBe("discover.bucket.strong");
    expect(bucketLabelKey("possible")).toBe("discover.bucket.possible");
    expect(bucketLabelKey("hidden")).toBe("discover.bucket.hidden");
    // Anything unexpected is shown as a possible match rather than an
    // unlabelled card: the row is visible, so it must say why.
    expect(bucketLabelKey("other")).toBe("discover.bucket.possible");
  });

  it("has a fit-tier label and a low-confidence toggle in both locales", () => {
    for (const locale of [en, hi]) {
      expect(locale.discover.fit.tier).toBeTruthy();
      expect(locale.discover.showAll).toBeTruthy();
      expect(locale.discover.hiddenHint).toBeTruthy();
    }
  });
});

describe("sweep helpers", () => {
  it("never treats a sweep as waiting for a human", () => {
    expect(sweepIsActive({ state: "queued" })).toBe(true);
    expect(sweepIsActive({ state: "running" })).toBe(true);
    // A sweep reports a challenge and stops; it never parks.
    expect(sweepIsActive({ state: "needs_you" })).toBe(false);
    expect(sweepIsActive({ state: "partial" })).toBe(false);
    expect(sweepIsActive(null)).toBe(false);
  });

  it("reads the counts it was given and leaves missing ones null", () => {
    const summary = sweepSummary({
      state: "done",
      notes: "Read 3 list pages.",
      metrics: {
        pages: 3,
        organisations_walked: 2,
        rows_seen: 41,
        stored: 12,
        updated: 29,
      },
    });

    expect(summary).toMatchObject({
      pages: 3,
      organisations: 2,
      rows: 41,
      stored: 12,
      updated: 29,
      notes: "Read 3 list pages.",
      state: "done",
    });
    expect(summary.rejected).toBeNull();
    expect(sweepSummary(undefined)).toMatchObject({ pages: null, notes: "" });
  });
});

describe("work comparison labels", () => {
  it("only a quoted yes reads as the same work", () => {
    expect(
      similarityTone({ answer: "yes", anchor_verified: true }),
    ).toBe("green");
    // A yes the notice does not quote is a suggestion, not a finding.
    expect(
      similarityTone({ answer: "yes", anchor_verified: false }),
    ).toBe("amber");
    // A no demotes; it is never shown as a confident rejection.
    expect(similarityTone({ answer: "no" })).toBe("neutral");
    expect(similarityTone(null)).toBe("neutral");
    // An unanchored yes is stored unsure + needs_confirmation; it reads as a
    // suggestion, never as a green finding.
    expect(
      similarityTone({ answer: "unsure", needs_confirmation: true }),
    ).toBe("amber");
    expect(
      similarityTone({
        answer: "yes",
        anchor_verified: true,
        needs_confirmation: true,
      }),
    ).toBe("amber");
  });

  it("uses the four closed answers and nothing else", () => {
    expect(similarityLabelKey({ answer: "yes", anchor_verified: true })).toBe(
      "discover.similarity.sameWork",
    );
    expect(similarityLabelKey({ answer: "yes", anchor_verified: false })).toBe(
      "discover.similarity.suggested",
    );
    expect(similarityLabelKey({ answer: "no" })).toBe(
      "discover.similarity.differentWork",
    );
    expect(similarityLabelKey({ answer: "unsure" })).toBe(
      "discover.similarity.unsure",
    );
    expect(
      similarityLabelKey({ answer: "unsure", needs_confirmation: true }),
    ).toBe("discover.similarity.suggested");
    expect(
      similarityLabelKey({
        answer: "yes",
        anchor_verified: true,
        needs_confirmation: true,
      }),
    ).toBe("discover.similarity.suggested");
    expect(similarityLabelKey(null)).toBeNull();
  });

  it("tones and labels the deterministic evidence hierarchy", () => {
    expect(matchTypeTone("exact_core")).toBe("green");
    expect(matchTypeTone("semantic_core")).toBe("green");
    expect(matchTypeTone("adjacent")).toBe("teal");
    expect(matchTypeTone("support_only")).toBe("amber");
    expect(matchTypeTone("generic_only")).toBe("neutral");
    expect(matchTypeTone("no_evidence")).toBe("neutral");
    expect(matchTypeTone(null)).toBe("neutral");

    expect(matchTypeLabelKey("exact_core")).toBe(
      "discover.similarity.matchTypes.exact_core",
    );
    expect(matchTypeLabelKey("no_evidence")).toBe(
      "discover.similarity.matchTypes.no_evidence",
    );
    expect(matchTypeLabelKey("made_up")).toBeNull();
    expect(matchTypeLabelKey(null)).toBeNull();
  });

  it("has a label in both locales for every answer", () => {
    for (const key of [
      "sameWork",
      "suggested",
      "differentWork",
      "unsure",
      "compare",
      "needCapabilities",
      "judgedAs",
      "tenderEvidence",
      "companyEvidence",
      "noTenderEvidence",
      "noCompanyEvidence",
      "evidenceRank",
    ]) {
      for (const locale of [en, hi]) {
        expect(locale.discover.similarity[key]).toBeTruthy();
      }
    }
    for (const key of [
      "exact_core",
      "semantic_core",
      "adjacent",
      "support_only",
      "generic_only",
      "no_evidence",
    ]) {
      for (const locale of [en, hi]) {
        expect(locale.discover.similarity.matchTypes[key]).toBeTruthy();
      }
    }
  });
});

describe("tender sort vocabulary", () => {
  it("defaults to best fit and names every order the server accepts", () => {
    expect(DEFAULT_TENDER_SORT).toBe("match_desc");
    expect(TENDER_SORT_OPTIONS[0]).toBe(DEFAULT_TENDER_SORT);
    // The gate's own fit, then the four columns the repository can order by.
    expect(TENDER_SORT_OPTIONS).toEqual([
      "match_desc",
      "created_desc",
      "created_asc",
      "closing_asc",
      "closing_desc",
    ]);
  });

  it("has a label in both locales for every order", () => {
    for (const option of TENDER_SORT_OPTIONS) {
      for (const locale of [en, hi]) {
        expect(locale.tenders.sort[option]).toBeTruthy();
      }
    }
  });
});
