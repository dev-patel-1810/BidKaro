import { describe, expect, it } from "vitest";

import { companyCompleteness, companyToForm, formToPayload, isOnboarded } from "./companyForm";

describe("company form mapping", () => {
  it("normalizes empty company state and preserves arrays", () => {
    const form = companyToForm(null);
    expect(form.name).toBe("");
    expect(form.preferences.sectors).toEqual([]);

    const mapped = companyToForm({
      name: "Asha Infra",
      region: "Odisha",
      sectors: ["Infrastructure"],
      services: ["Roads"],
      keywords: ["highway"],
      profile: {
        legal_name: null,
        regions: ["Odisha", "Bihar"],
        preferences: { sectors: ["Transport"], min_value: 1000 },
      },
    });
    expect(mapped.legal_name).toBe("");
    expect(mapped.sectors).toEqual(["Infrastructure"]);
    expect(mapped.products_services).toEqual(["Roads"]);
    expect(mapped.preferences.keywords).toEqual(["highway"]);
    expect(mapped.preferences.min_value).toBe(1000);
  });

  it("maps the UI form onto the v2 payload", () => {
    const payload = formToPayload(
      {
        name: "  Asha Infra  ",
        legal_name: "  Asha Infrastructure Pvt Ltd ",
        business_type: "",
        description: "",
        products_services: ["Roads"],
        sectors: ["Infrastructure"],
        regions: ["Odisha", "Bihar"],
        certifications: [],
        annual_turnover: "",
        turnover_currency: "inr",
        employee_count: "240",
        established_year: "",
        preferences: {
          sectors: ["Infrastructure"],
          regions: ["Odisha"],
          keywords: ["highway"],
          min_value: "",
          max_value: "500000",
        },
      },
      { onboarding_completed: true },
    );

    expect(payload.name).toBe("Asha Infra");
    expect(payload.region).toBe("Odisha");
    expect(payload.sectors).toEqual(["Infrastructure"]);
    expect(payload.services).toEqual(["Roads"]);
    expect(payload.keywords).toEqual(["highway"]);
    expect(payload.profile.legal_name).toBe("Asha Infrastructure Pvt Ltd");
    expect(payload.profile.annual_turnover).toBeNull();
    expect(payload.profile.employee_count).toBe(240);
    expect(payload.profile.turnover_currency).toBe("INR");
    expect(payload.profile.preferences.min_value).toBeNull();
    expect(payload.profile.preferences.max_value).toBe(500000);
    expect(payload.profile.onboarding_completed).toBe(true);
  });

  it("treats a company with discovery terms as onboarded", () => {
    expect(isOnboarded({ sectors: ["Infrastructure"] })).toBe(true);
    expect(isOnboarded({ services: ["Roads"] })).toBe(true);
    expect(isOnboarded({ keywords: ["highway"] })).toBe(true);
    expect(isOnboarded({ profile: { onboarding_completed: true } })).toBe(true);
    expect(isOnboarded({ profile: { onboarding_completed: false }, sectors: ["Roads"] })).toBe(
      false,
    );
    expect(isOnboarded({})).toBe(false);
  });

  it("computes completeness from the form", () => {
    expect(companyCompleteness(null)).toBe(0);
    expect(
      companyCompleteness({
        name: "Asha",
        sectors: ["Roads"],
        products_services: ["Highways"],
        regions: ["Odisha"],
        preferences: { keywords: ["NH"], regions: [] },
      }),
    ).toBe(100);
  });
});
