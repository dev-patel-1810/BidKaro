import { describe, expect, it } from "vitest";

import {
  categoryTone,
  pageReference,
  requirementQuery,
  summarizeRequirements,
  verificationTone,
} from "./requirements";

describe("requirementQuery", () => {
  it("builds a query from the v2 filters", () => {
    expect(requirementQuery({ category: "eligibility", state: ["unverified"] })).toBe(
      "?category=eligibility&state=unverified",
    );
  });

  it("repeats the state filter and omits empty filters", () => {
    expect(
      requirementQuery({ state: ["verified", "accepted_ocr"], include_inactive: true }),
    ).toBe("?state=verified&state=accepted_ocr&include_inactive=true");
    expect(requirementQuery()).toBe("");
  });
});

describe("pageReference", () => {
  it("reports a printed label only when it differs from the viewer page", () => {
    expect(pageReference({ page_index: 3, printed_label: "3" })).toEqual({
      viewer: 3,
      printed: "",
    });
    expect(pageReference({ page_index: 3, printed_label: "A-2" })).toEqual({
      viewer: 3,
      printed: "A-2",
    });
  });
});

describe("tones and summaries", () => {
  it("maps verification and category states to readable tones", () => {
    expect(verificationTone("verified")).toBe("green");
    expect(verificationTone("accepted_ocr")).toBe("teal");
    expect(verificationTone("unverified")).toBe("amber");
    expect(verificationTone("rejected")).toBe("red");
    expect(categoryTone("eligibility")).toBe("teal");
    expect(categoryTone("unknown")).toBe("neutral");
  });

  it("counts every v2 requirement state", () => {
    expect(
      summarizeRequirements([
        { state: "verified" },
        { state: "accepted_ocr" },
        { state: "unverified" },
        { state: "needs_review" },
        { state: "rejected" },
      ]),
    ).toEqual({
      total: 5,
      verified: 1,
      accepted_ocr: 1,
      needs_review: 1,
      unverified: 1,
      rejected: 1,
    });
  });
});
