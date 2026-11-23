// The v2 backend keeps the company's structured profile in one JSONB column
// (`profile`) and the discovery terms in typed columns (`sectors`, `services`,
// `keywords`, `region`).  The UI keeps the older, richer form shape, so this
// module is the single translation point between the two.

export const EMPTY_COMPANY_FORM = {
  name: "",
  legal_name: "",
  business_type: "",
  description: "",
  products_services: [],
  sectors: [],
  regions: [],
  certifications: [],
  annual_turnover: "",
  turnover_currency: "INR",
  employee_count: "",
  established_year: "",
  preferences: {
    sectors: [],
    regions: [],
    keywords: [],
    min_value: "",
    max_value: "",
  },
};

function valueOrEmpty(value) {
  return value === null || value === undefined ? "" : value;
}

function list(value) {
  return Array.isArray(value) ? value.filter(Boolean) : [];
}

function unique(values) {
  return [...new Set(values.map((value) => String(value).trim()).filter(Boolean))];
}

export function companyToForm(company) {
  if (!company) return { ...EMPTY_COMPANY_FORM, preferences: { ...EMPTY_COMPANY_FORM.preferences } };
  const profile = company.profile || {};
  const preferences = profile.preferences || {};
  const regions = list(profile.regions);
  if (company.region && !regions.includes(company.region)) regions.unshift(company.region);

  return {
    name: valueOrEmpty(company.name),
    legal_name: valueOrEmpty(profile.legal_name),
    business_type: valueOrEmpty(profile.business_type),
    description: valueOrEmpty(profile.description),
    products_services: list(company.services).length ? list(company.services) : list(profile.products_services),
    sectors: list(company.sectors).length ? list(company.sectors) : list(preferences.sectors),
    regions,
    certifications: list(profile.certifications),
    annual_turnover: valueOrEmpty(profile.annual_turnover),
    turnover_currency: profile.turnover_currency || "INR",
    employee_count: valueOrEmpty(profile.employee_count),
    established_year: valueOrEmpty(profile.established_year),
    preferences: {
      sectors: list(preferences.sectors).length ? list(preferences.sectors) : list(company.sectors),
      regions: list(preferences.regions).length ? list(preferences.regions) : regions,
      keywords: list(company.keywords).length ? list(company.keywords) : list(preferences.keywords),
      min_value: valueOrEmpty(preferences.min_value),
      max_value: valueOrEmpty(preferences.max_value),
    },
  };
}

function numberOrNull(value) {
  if (value === "" || value === null || value === undefined) return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

export function formToPayload(form, profileExtra = {}) {
  const preferences = form.preferences || {};
  // The onboarding form collects sectors and regions twice (capability and
  // preference steps); the backend has one typed column for each, so merge both
  // inputs instead of letting the last step silently win.
  const sectors = unique([...(form.sectors || []), ...(preferences.sectors || [])]);
  const regions = unique([...(form.regions || []), ...(preferences.regions || [])]);
  const services = unique(form.products_services || []);
  const keywords = unique(preferences.keywords || []);
  return {
    name: form.name?.trim(),
    region: regions[0] || null,
    sectors,
    services,
    keywords,
    profile: {
      legal_name: form.legal_name?.trim() || null,
      business_type: form.business_type || null,
      description: form.description?.trim() || null,
      regions,
      certifications: form.certifications || [],
      annual_turnover: numberOrNull(form.annual_turnover),
      turnover_currency: (form.turnover_currency || "INR").toUpperCase(),
      employee_count: numberOrNull(form.employee_count),
      established_year: numberOrNull(form.established_year),
      products_services: services,
      preferences: {
        sectors,
        regions,
        keywords,
        min_value: numberOrNull(preferences.min_value),
        max_value: numberOrNull(preferences.max_value),
      },
      ...profileExtra,
    },
  };
}

// The backend has no `onboarding_completed` column.  It lives in the profile
// JSON so the flag survives without a schema change.  A company that already
// has discovery terms is treated as onboarded even if it predates the flag.
export function isOnboarded(company) {
  if (!company) return false;
  const flag = company.profile?.onboarding_completed;
  if (flag === true) return true;
  if (flag === false) return false;
  return Boolean(
    list(company.sectors).length ||
      list(company.services).length ||
      list(company.keywords).length,
  );
}

export function companyCompleteness(companyOrForm) {
  if (!companyOrForm) return 0;
  const form = companyOrForm.preferences
    ? companyOrForm
    : companyToForm(companyOrForm);
  const checks = [
    Boolean(form.name?.trim()),
    Boolean(form.sectors?.length),
    Boolean(form.products_services?.length),
    Boolean(form.regions?.length || form.preferences?.regions?.length),
    Boolean(form.preferences?.keywords?.length),
  ];
  return Math.round((checks.filter(Boolean).length / checks.length) * 100);
}
