import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  Check,
  ExternalLink,
  Eye,
  EyeOff,
  FileText,
  Flag,
  Link2,
  ShieldCheck,
  ShieldQuestion,
  Sparkles,
  X,
} from "lucide-react";

import { api } from "../../lib/api";
import {
  REQUIREMENT_CATEGORIES,
  REQUIREMENT_STATES,
  categoryTone,
  requirementQuery,
} from "../../lib/requirements";
import { cn, formatDate, formatDateTime } from "../../lib/utils";
import { currentLocale } from "../../i18n";
import { useToast } from "../../contexts/ToastContext";
import { Badge, StatusBadge } from "../ui/Badge";
import { Button } from "../ui/Button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "../ui/Card";
import { EmptyState } from "../ui/EmptyState";
import { Select } from "../ui/Field";
import { Modal } from "../ui/Modal";
import { Skeleton } from "../ui/Spinner";

function SummaryTile({ label, value, tone = "text-navy-900 dark:text-white" }) {
  return (
    <div className="rounded-xl bg-navy-50 p-3 dark:bg-navy-800">
      <p className="text-[10px] font-bold uppercase tracking-wide text-navy-400">{label}</p>
      <p className={cn("mt-1 text-lg font-semibold", tone)}>{value}</p>
    </div>
  );
}

function stateLabel(t, state) {
  return t(`tenders.requirements.states.${state}`, {
    defaultValue: t(`tenders.requirements.verification.${state}`, {
      defaultValue: state,
    }),
  });
}

const COVERAGE_TONE = {
  covered: "green",
  partial: "amber",
  new: "blue",
  unknown: "neutral",
};

function coverageLabel(t, coverage) {
  return t(`tenders.requirements.coverage.${coverage}`, {
    defaultValue: coverage,
  });
}

export function TenderRequirementsPanel({ tenderId, refreshKey = 0 }) {
  const { t } = useTranslation();
  const toast = useToast();

  const [items, setItems] = useState([]);
  const [total, setTotal] = useState(0);
  const [summary, setSummary] = useState({
    total: 0,
    verified: 0,
    accepted_ocr: 0,
    needs_review: 0,
    unverified: 0,
    rejected: 0,
  });
  const [filters, setFilters] = useState({ category: "", state: "" });
  const [showIgnored, setShowIgnored] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [busyId, setBusyId] = useState(null);
  const [coverage, setCoverage] = useState(null);
  const [coverageLoading, setCoverageLoading] = useState(false);
  const [comparing, setComparing] = useState(false);

  const [pageModal, setPageModal] = useState(null);
  const [pageText, setPageText] = useState(null);
  const [pageLoading, setPageLoading] = useState(false);

  const [evidenceModal, setEvidenceModal] = useState(null);
  const [evidenceData, setEvidenceData] = useState(null);
  const [evidenceLoading, setEvidenceLoading] = useState(false);
  const [evidenceNote, setEvidenceNote] = useState("");
  const [evidenceBusy, setEvidenceBusy] = useState(null);

  const load = async () => {
    setLoading(true);
    setError("");
    try {
      const [list, counts] = await Promise.all([
        api.get(
          `/tenders/${tenderId}/requirements${requirementQuery({
            category: filters.category,
            state: filters.state ? [filters.state] : [],
            include_ignored: showIgnored,
          })}`,
        ),
        api.get(`/tenders/${tenderId}/requirements/summary`),
      ]);
      setItems(list.items || []);
      setTotal(list.total || 0);
      setSummary({
        total: counts.total || 0,
        verified: counts.verified || 0,
        accepted_ocr: counts.accepted_ocr || 0,
        needs_review: counts.needs_review || 0,
        unverified: counts.unverified || 0,
        rejected: counts.rejected || 0,
      });
    } catch (err) {
      setError(
        err.message === "NETWORK_ERROR" ? t("errors.network") : err.message || t("errors.generic"),
      );
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    let active = true;
    setLoading(true);
    setError("");
    Promise.all([
      api.get(
        `/tenders/${tenderId}/requirements${requirementQuery({
          category: filters.category,
          state: filters.state ? [filters.state] : [],
          include_ignored: showIgnored,
        })}`,
      ),
      api.get(`/tenders/${tenderId}/requirements/summary`),
    ])
      .then(([list, counts]) => {
        if (!active) return;
        setItems(list.items || []);
        setTotal(list.total || 0);
        setSummary({
          total: counts.total || 0,
          verified: counts.verified || 0,
          accepted_ocr: counts.accepted_ocr || 0,
          needs_review: counts.needs_review || 0,
          unverified: counts.unverified || 0,
          rejected: counts.rejected || 0,
        });
      })
      .catch((err) => {
        if (active) {
          setError(
            err.message === "NETWORK_ERROR"
              ? t("errors.network")
              : err.message || t("errors.generic"),
          );
        }
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tenderId, refreshKey, filters.category, filters.state, showIgnored]);

  // The experience comparison is a separate, user-triggered read. It is shown
  // beside the requirements but never changes their verification state or an
  // eligibility answer.
  useEffect(() => {
    let active = true;
    setCoverageLoading(true);
    api.get(`/tenders/${tenderId}/requirement-experience`)
      .then((data) => {
        if (active) setCoverage(data);
      })
      .catch(() => {
        // A missing comparison is shown as "not compared yet", not as an
        // error that hides the requirements themselves.
        if (active) setCoverage(null);
      })
      .finally(() => {
        if (active) setCoverageLoading(false);
      });
    return () => {
      active = false;
    };
  }, [tenderId, refreshKey]);

  const updateFilter = (field) => (event) =>
    setFilters((current) => ({ ...current, [field]: event.target.value }));

  const review = async (requirement, state, note = null) => {
    setBusyId(requirement.id);
    try {
      const updated = await api.patch(`/requirements/${requirement.id}`, {
        state,
        note,
      });
      setItems((current) => {
        if (filters.state && updated.state !== filters.state) {
          return current.filter((item) => item.id !== updated.id);
        }
        return current.map((item) => (item.id === updated.id ? updated : item));
      });
      if (filters.state && updated.state !== filters.state) {
        setTotal((current) => Math.max(current - 1, 0));
      }
      await load();
      if (state === "accepted_ocr") {
        toast.success(
          t("tenders.requirements.acceptedOcrToast", {
            defaultValue: "OCR citation accepted after human review.",
          }),
        );
      } else if (state === "rejected") {
        toast.info(t("tenders.requirements.rejectedToast"));
      } else {
        toast.info(t("tenders.requirements.flaggedToast"));
      }
    } catch (err) {
      toast.error(
        err.message === "NETWORK_ERROR" ? t("errors.network") : err.message || t("errors.generic"),
      );
    } finally {
      setBusyId(null);
    }
  };

  const toggleIgnored = async (requirement) => {
    setBusyId(requirement.id);
    try {
      await api.post(`/requirements/${requirement.id}/flag`, {
        ignored: !requirement.flagged,
      });
      await load();
      toast.info(
        requirement.flagged
          ? t("tenders.requirements.unignoredToast", {
              defaultValue: "Requirement shown for your company again.",
            })
          : t("tenders.requirements.ignoredToast", {
              defaultValue: "Requirement hidden for your company.",
            }),
      );
    } catch (err) {
      toast.error(
        err.message === "NETWORK_ERROR" ? t("errors.network") : err.message || t("errors.generic"),
      );
    } finally {
      setBusyId(null);
    }
  };

  const compareExperience = async () => {
    setComparing(true);
    try {
      const data = await api.post(
        `/tenders/${tenderId}/requirement-experience`,
        {},
      );
      setCoverage(data);
      toast.success(t("tenders.requirements.coverage.done"));
    } catch (err) {
      toast.error(
        err.message === "NETWORK_ERROR"
          ? t("errors.network")
          : err.message || t("tenders.requirements.coverage.error"),
      );
    } finally {
      setComparing(false);
    }
  };

  const viewPage = async (requirement) => {
    setPageModal(requirement);
    setPageText(null);
    setPageLoading(true);
    try {
      const page = await api.get(
        `/tender-documents/${requirement.document_id}/pages/${requirement.page_index}`,
      );
      setPageText(page);
    } catch (err) {
      toast.error(
        err.message === "NETWORK_ERROR" ? t("errors.network") : err.message || t("errors.generic"),
      );
    } finally {
      setPageLoading(false);
    }
  };

  const openEvidence = async (requirement) => {
    setEvidenceModal(requirement);
    setEvidenceData(null);
    setEvidenceNote("");
    setEvidenceLoading(true);
    try {
      const data = await api.get(
        `/tenders/${tenderId}/requirements/${requirement.id}/evidence`,
      );
      setEvidenceData(data);
      setEvidenceNote(data.note || "");
    } catch (err) {
      toast.error(
        err.message === "NETWORK_ERROR" ? t("errors.network") : err.message || t("errors.generic"),
      );
    } finally {
      setEvidenceLoading(false);
    }
  };

  const decideEvidence = async (candidate, status) => {
    if (!evidenceModal) return;
    setEvidenceBusy(candidate?.document_id || "missing");
    try {
      const data = await api.post(
        `/tenders/${tenderId}/requirements/${evidenceModal.id}/evidence`,
        {
          document_id: candidate?.document_id || null,
          document_version_id: candidate?.document_version_id || null,
          status,
          note: evidenceNote || null,
        },
      );
      setEvidenceData(data);
      if (status === "covered") {
        toast.success(
          t("tenders.requirements.evidence.coveredToast", {
            defaultValue: "Evidence confirmed.",
          }),
        );
      } else if (status === "contradicted") {
        toast.info(t("tenders.requirements.evidence.contradictedToast"));
      } else {
        toast.info(
          t("tenders.requirements.evidence.missingToast", {
            defaultValue: "Marked as missing evidence.",
          }),
        );
      }
    } catch (err) {
      toast.error(
        err.message === "NETWORK_ERROR" ? t("errors.network") : err.message || t("errors.generic"),
      );
    } finally {
      setEvidenceBusy(null);
    }
  };

  const evidenceStatusTone = (status) => {
    if (status === "covered") return "green";
    if (status === "contradicted") return "red";
    if (status === "missing" || status === "expired") return "amber";
    return "neutral";
  };

  const coverageByRequirement = {};
  for (const item of coverage?.items || []) {
    coverageByRequirement[item.requirement_id] = item;
  }
  const coverageCounts = coverage?.counts || null;
  const coverageTotal = coverageCounts
    ? Object.values(coverageCounts).reduce((sum, value) => sum + value, 0)
    : 0;

  return (
    <Card>
      <CardHeader>
        <CardTitle>{t("tenders.requirements.title")}</CardTitle>
        <CardDescription>{t("tenders.requirements.subtitle")}</CardDescription>
      </CardHeader>
      <CardContent className="space-y-4 pt-2">
        <div className="grid gap-2 sm:grid-cols-3 lg:grid-cols-6">
          <SummaryTile label={t("tenders.requirements.summary.total")} value={summary.total} />
          <SummaryTile
            label={t("tenders.requirements.summary.verified")}
            value={summary.verified}
            tone="text-emerald-700 dark:text-emerald-300"
          />
          <SummaryTile
            label={t("tenders.requirements.summary.acceptedOcr", {
              defaultValue: "Accepted OCR",
            })}
            value={summary.accepted_ocr}
            tone="text-teal-700 dark:text-teal-300"
          />
          <SummaryTile
            label={t("tenders.requirements.summary.unverified")}
            value={summary.unverified}
            tone="text-amber-700 dark:text-amber-300"
          />
          <SummaryTile
            label={t("tenders.requirements.summary.needsReview")}
            value={summary.needs_review}
            tone="text-amber-700 dark:text-amber-300"
          />
          <SummaryTile
            label={t("tenders.requirements.summary.rejected", {
              defaultValue: "Rejected",
            })}
            value={summary.rejected}
            tone="text-red-700 dark:text-red-300"
          />
        </div>

        <div className="grid gap-2 sm:grid-cols-2">
          <Select
            value={filters.category}
            onChange={updateFilter("category")}
            aria-label={t("tenders.requirements.filters.category")}
          >
            <option value="">{t("tenders.requirements.filters.allCategories")}</option>
            {REQUIREMENT_CATEGORIES.map((category) => (
              <option key={category} value={category}>
                {t(`tenders.requirements.categories.${category}`, {
                  defaultValue: category,
                })}
              </option>
            ))}
          </Select>
          <Select
            value={filters.state}
            onChange={updateFilter("state")}
            aria-label={t("tenders.requirements.filters.verification")}
          >
            <option value="">{t("tenders.requirements.filters.allVerification")}</option>
            {REQUIREMENT_STATES.map((state) => (
              <option key={state} value={state}>
                {stateLabel(t, state)}
              </option>
            ))}
          </Select>
        </div>

        <label className="flex items-center gap-2 text-xs text-navy-600 dark:text-navy-300">
          <input
            type="checkbox"
            checked={showIgnored}
            onChange={(event) => setShowIgnored(event.target.checked)}
            className="h-3.5 w-3.5 rounded border-navy-300"
          />
          {t("tenders.requirements.showIgnored", {
            defaultValue: "Show requirements hidden for my company",
          })}
        </label>

        <div className="rounded-xl border border-navy-100 px-3.5 py-3 dark:border-navy-800">
          <div className="flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
            <div className="min-w-0">
              <p className="text-xs font-semibold text-navy-900 dark:text-white">
                {t("tenders.requirements.coverage.title")}
              </p>
              <p className="mt-0.5 text-[11px] leading-5 muted">
                {t("tenders.requirements.coverage.hint")}
              </p>
            </div>
            <Button
              size="sm"
              variant="outline"
              className="shrink-0"
              onClick={compareExperience}
              loading={comparing}
              disabled={!summary.total}
            >
              <Sparkles className="h-3.5 w-3.5" />
              {comparing
                ? t("tenders.requirements.coverage.comparing")
                : t("tenders.requirements.coverage.compare")}
            </Button>
          </div>
          {coverageLoading ? (
            <p className="mt-2 text-[11px] muted">
              {t("common.loading", { defaultValue: "Loading…" })}
            </p>
          ) : coverageTotal ? (
            <p className="mt-2 text-[11px] muted">
              {t("tenders.requirements.coverage.summary", {
                covered: coverageCounts?.covered ?? 0,
                partial: coverageCounts?.partial ?? 0,
                new: coverageCounts?.new ?? 0,
                unknown: coverageCounts?.unknown ?? 0,
              })}
            </p>
          ) : (
            <p className="mt-2 text-[11px] muted">
              {t("tenders.requirements.coverage.noMapping")}
            </p>
          )}
        </div>

        {error ? (
          <div className="flex items-center justify-between gap-3 rounded-xl border border-red-200 bg-red-50 p-3 text-sm text-red-700 dark:border-red-900 dark:bg-red-950/40 dark:text-red-200">
            <span>{error}</span>
            <Button size="sm" variant="outline" onClick={load}>
              {t("common.retry")}
            </Button>
          </div>
        ) : null}

        {loading ? (
          <div className="space-y-2">
            <Skeleton className="h-40 w-full" />
            <Skeleton className="h-40 w-full" />
          </div>
        ) : items.length ? (
          <div className="space-y-3">
            {items.map((requirement) => {
              const page = requirement.page_index;
              const coverageItem = coverageByRequirement[requirement.id];
              const coverageMatches = [
                ...(coverageItem?.capabilities || []),
                ...(coverageItem?.experiences || []),
              ];
              const printed =
                requirement.printed_label &&
                requirement.printed_label !== String(requirement.page_index)
                  ? requirement.printed_label
                  : null;
              const document = requirement.document;
              const canAcceptOcr =
                requirement.state === "needs_review" &&
                document &&
                document.has_text_layer === false;
              return (
                <article
                  key={requirement.id}
                  className="rounded-xl border border-navy-100 p-4 dark:border-navy-800"
                >
                  <div className="flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
                    <div className="min-w-0">
                      <h3 className="text-sm font-semibold text-navy-900 dark:text-white">
                        {requirement.title || requirement.obligation || t("tenders.requirements.untitled", { defaultValue: "Requirement" })}
                      </h3>
                      <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
                        <Badge tone={categoryTone(requirement.category)}>
                          {t(`tenders.requirements.categories.${requirement.category}`, {
                            defaultValue: requirement.category,
                          })}
                        </Badge>
                        <StatusBadge
                          status={requirement.state}
                          label={stateLabel(t, requirement.state)}
                        />
                        <Badge
                          tone={
                            requirement.mandatory_state === "mandatory"
                              ? "amber"
                              : "neutral"
                          }
                        >
                          {t(`tenders.requirements.mandatoryStates.${requirement.mandatory_state}`, {
                            defaultValue: t(
                              `tenders.requirements.mandatoryValues.${requirement.mandatory_state}`,
                              { defaultValue: requirement.mandatory_state },
                            ),
                          })}
                        </Badge>
                        <Badge tone="neutral">
                          {t(`tenders.requirements.phases.${requirement.phase}`, {
                            defaultValue: requirement.phase,
                          })}
                        </Badge>
                        {coverageItem ? (
                          <Badge
                            tone={COVERAGE_TONE[coverageItem.coverage] || "neutral"}
                          >
                            {coverageLabel(t, coverageItem.coverage)}
                          </Badge>
                        ) : null}
                        {document && !document.has_text_layer ? (
                          <Badge tone="amber">{t("tenders.requirements.ocrBadge")}</Badge>
                        ) : null}
                        {requirement.is_navigation ? (
                          <Badge tone="neutral">
                            {t("tenders.requirements.navigation", {
                              defaultValue: "Navigation text",
                            })}
                          </Badge>
                        ) : null}
                        {requirement.flagged ? (
                          <Badge tone="amber">
                            {t("tenders.requirements.ignored", {
                              defaultValue: "Hidden for your company",
                            })}
                          </Badge>
                        ) : null}
                      </div>
                    </div>
                    <div className="flex shrink-0 flex-wrap gap-1.5">
                      <Button size="sm" variant="outline" onClick={() => openEvidence(requirement)}>
                        <Link2 className="h-3.5 w-3.5" />
                        {evidenceData?.status && evidenceModal?.id === requirement.id
                          ? t("tenders.requirements.evidence.review")
                          : t("tenders.requirements.evidence.link")}
                      </Button>
                      <Button size="sm" variant="outline" onClick={() => viewPage(requirement)}>
                        <Eye className="h-3.5 w-3.5" />
                        {t("tenders.requirements.viewPage")}
                      </Button>
                      <Button
                        size="sm"
                        variant="ghost"
                        onClick={() => toggleIgnored(requirement)}
                        loading={busyId === requirement.id}
                        title={
                          requirement.flagged
                            ? t("tenders.requirements.unignore", {
                                defaultValue: "Show for my company",
                              })
                            : t("tenders.requirements.ignore", {
                                defaultValue: "Hide for my company",
                              })
                        }
                      >
                        <EyeOff className="h-3.5 w-3.5" />
                        {requirement.flagged
                          ? t("tenders.requirements.unignore", {
                              defaultValue: "Show for my company",
                            })
                          : t("tenders.requirements.ignore", {
                              defaultValue: "Hide for my company",
                            })}
                      </Button>
                      {canAcceptOcr ? (
                        <Button
                          size="sm"
                          variant="subtle"
                          onClick={() => review(requirement, "accepted_ocr")}
                          loading={busyId === requirement.id}
                        >
                          <Check className="h-3.5 w-3.5" />
                          {t("tenders.requirements.acceptOcr", {
                            defaultValue: "Accept OCR",
                          })}
                        </Button>
                      ) : null}
                      {requirement.state !== "needs_review" ? (
                        <Button
                          size="sm"
                          variant="ghost"
                          onClick={() => review(requirement, "needs_review")}
                          loading={busyId === requirement.id}
                        >
                          <Flag className="h-3.5 w-3.5" />
                          {t("tenders.requirements.flag")}
                        </Button>
                      ) : null}
                      {requirement.state !== "rejected" ? (
                        <Button
                          size="sm"
                          variant="ghost"
                          onClick={() => review(requirement, "rejected")}
                          loading={busyId === requirement.id}
                          aria-label={t("tenders.requirements.reject")}
                          title={t("tenders.requirements.reject")}
                        >
                          <X className="h-3.5 w-3.5" />
                        </Button>
                      ) : (
                        <Button
                          size="sm"
                          variant="ghost"
                          onClick={() => review(requirement, "needs_review")}
                          loading={busyId === requirement.id}
                        >
                          <Flag className="h-3.5 w-3.5" />
                          {t("tenders.requirements.reopen", {
                            defaultValue: "Reopen review",
                          })}
                        </Button>
                      )}
                    </div>
                  </div>

                  <blockquote className="mt-3 border-l-2 border-teal-300 bg-teal-50/50 px-3 py-2 text-sm leading-6 text-navy-800 dark:border-teal-700 dark:bg-teal-950/30 dark:text-navy-100">
                    <p className="mb-1 text-[10px] font-bold uppercase tracking-wide text-teal-700 dark:text-teal-300">
                      {t("tenders.requirements.excerpt")}
                    </p>
                    <p className="whitespace-pre-wrap">{requirement.excerpt}</p>
                  </blockquote>

                  {requirement.obligation && requirement.obligation !== requirement.excerpt ? (
                    <div className="mt-2">
                      <p className="text-[10px] font-bold uppercase tracking-wide text-navy-400">
                        {t("tenders.requirements.interpretation")}
                      </p>
                      <p className="mt-1 text-sm leading-6 muted">{requirement.obligation}</p>
                    </div>
                  ) : null}

                  <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs muted">
                    {document?.filename ? (
                      <span className="inline-flex max-w-full items-center gap-1">
                        <FileText className="h-3 w-3 shrink-0" />
                        <span className="truncate">{document.filename}</span>
                      </span>
                    ) : null}
                    {document?.fetched_at ? (
                      <span>
                        {t("tenders.requirements.retrieved", {
                          date: formatDateTime(document.fetched_at, currentLocale()),
                        })}
                      </span>
                    ) : null}
                    <span>{t("tenders.requirements.page", { page })}</span>
                    {printed ? (
                      <span>{t("tenders.requirements.printed", { label: printed })}</span>
                    ) : null}
                    {requirement.clause_ref ? (
                      <span>{t("tenders.requirements.clause", { ref: requirement.clause_ref })}</span>
                    ) : null}
                    {requirement.form_ref ? (
                      <span>{t("tenders.requirements.form", { ref: requirement.form_ref })}</span>
                    ) : null}
                    {document?.source_url ? (
                      <a
                        href={document.source_url}
                        target="_blank"
                        rel="noreferrer noopener"
                        className="inline-flex items-center gap-1 font-medium text-teal-700 hover:underline dark:text-teal-300"
                      >
                        {t("tenders.requirements.source")}
                        <ExternalLink className="h-3 w-3" />
                      </a>
                    ) : null}
                  </div>

                  <dl className="mt-2 grid gap-2 text-xs sm:grid-cols-2">
                    {requirement.acceptance_criteria ? (
                      <div>
                        <dt className="font-semibold text-navy-700 dark:text-navy-200">
                          {t("tenders.requirements.acceptance", {
                            defaultValue: "Acceptance criteria",
                          })}
                        </dt>
                        <dd className="mt-0.5 muted">{requirement.acceptance_criteria}</dd>
                      </div>
                    ) : null}
                    {requirement.issuer || requirement.signatory ? (
                      <div>
                        <dt className="font-semibold text-navy-700 dark:text-navy-200">
                          {t("tenders.requirements.issuerSignatory", {
                            defaultValue: "Issuer / signatory",
                          })}
                        </dt>
                        <dd className="mt-0.5 muted">
                          {[requirement.issuer, requirement.signatory].filter(Boolean).join(" · ")}
                        </dd>
                      </div>
                    ) : null}
                    {requirement.evidence_check ? (
                      <div>
                        <dt className="font-semibold text-navy-700 dark:text-navy-200">
                          {t("tenders.requirements.evidenceCheck", {
                            defaultValue: "Evidence check",
                          })}
                        </dt>
                        <dd className="mt-0.5 muted">{requirement.evidence_check}</dd>
                      </div>
                    ) : null}
                  </dl>

                  {coverageItem ? (
                    <div className="mt-2 rounded-lg border border-navy-100 px-3 py-2 dark:border-navy-800">
                      <p className="text-[10px] font-bold uppercase tracking-wide text-navy-400">
                        {t("tenders.requirements.coverage.title")}
                      </p>
                      {coverageItem.company_citation ? (
                        <>
                          <p className="mt-1 text-xs muted">
                            {t("tenders.requirements.coverage.companyPage", {
                              page: coverageItem.company_citation.page_index,
                            })}
                          </p>
                          <p className="mt-1 border-l-2 border-teal-300 pl-2 text-xs italic text-navy-700 dark:border-teal-700 dark:text-navy-200">
                            “{coverageItem.company_citation.excerpt}”
                          </p>
                        </>
                      ) : null}
                      {coverageMatches.length ? (
                        <p className="mt-1 text-[11px] muted">
                          {t("tenders.requirements.coverage.matchedWith", {
                            list: coverageMatches.join(", "),
                          })}
                        </p>
                      ) : null}
                      {coverageItem.coverage === "new" ? (
                        <p className="mt-1 text-[11px] text-amber-700 dark:text-amber-300">
                          {t("tenders.requirements.coverage.newHint")}
                        </p>
                      ) : null}
                      {coverageItem.coverage === "unknown" ? (
                        <p className="mt-1 text-[11px] muted">
                          {t("tenders.requirements.coverage.unknownHint")}
                        </p>
                      ) : null}
                    </div>
                  ) : null}

                  {requirement.deadline_text || requirement.deadline_at ? (
                    <p className="mt-2 text-xs text-navy-700 dark:text-navy-200">
                      <span className="font-semibold">{t("tenders.requirements.deadline")}:</span>{" "}
                      {requirement.deadline_text ||
                        formatDate(requirement.deadline_at, currentLocale())}
                    </p>
                  ) : null}
                  {requirement.consequence_text ? (
                    <p className="mt-1 text-xs text-navy-700 dark:text-navy-200">
                      <span className="font-semibold">
                        {t("tenders.requirements.consequence")}:
                      </span>{" "}
                      {requirement.consequence_text}
                    </p>
                  ) : null}

                  {["unverified", "needs_review"].includes(requirement.state) &&
                  requirement.state_reason ? (
                    <p className="mt-2 rounded-lg bg-amber-50 px-2.5 py-1.5 text-xs text-amber-800 dark:bg-amber-950/40 dark:text-amber-200">
                      {t("tenders.requirements.unverifiedNote", {
                        reason: requirement.state_reason,
                      })}
                    </p>
                  ) : null}
                </article>
              );
            })}
          </div>
        ) : (
          <EmptyState
            icon={Sparkles}
            title={t("tenders.requirements.emptyTitle")}
            description={
              summary.total
                ? t("tenders.requirements.emptyFiltered")
                : t("tenders.requirements.emptyBody")
            }
          />
        )}

        {!loading && !filters.category && !filters.state && total > items.length ? (
          <p className="text-center text-xs muted">
            {t("tenders.requirements.showing", { shown: items.length, total })}
          </p>
        ) : null}
      </CardContent>

      <Modal
        open={Boolean(pageModal)}
        onClose={() => setPageModal(null)}
        title={t("tenders.requirements.pageModalTitle")}
        description={
          pageModal
            ? `${pageModal.title || t("tenders.requirements.untitled", { defaultValue: "Requirement" })} · ${t(
                "tenders.requirements.page",
                { page: pageModal.page_index },
              )}`
            : ""
        }
        size="lg"
        footer={
          <Button variant="outline" onClick={() => setPageModal(null)}>
            {t("common.close")}
          </Button>
        }
      >
        {pageLoading ? (
          <Skeleton className="h-40 w-full" />
        ) : pageText ? (
          <div>
            {pageText.is_ocr ? (
              <div className="mb-2 rounded-lg border border-amber-200 bg-amber-50 px-2.5 py-2 text-xs text-amber-800 dark:border-amber-900 dark:bg-amber-950/40 dark:text-amber-200">
                {t("tenders.requirements.ocrPageNote")}
              </div>
            ) : null}
            <div className="mb-2 flex items-center gap-2 text-xs muted">
              {pageText.char_count > 0 ? (
                <ShieldCheck className="h-3.5 w-3.5 text-emerald-600" />
              ) : (
                <ShieldQuestion className="h-3.5 w-3.5 text-amber-600" />
              )}
              <span>{t("tenders.requirements.page", { page: pageText.page_index })}</span>
              {pageText.printed_label && pageText.printed_label !== String(pageText.page_index) ? (
                <span>
                  {t("tenders.requirements.printed", { label: pageText.printed_label })}
                </span>
              ) : null}
            </div>
            <pre className="max-h-[30rem] overflow-auto whitespace-pre-wrap rounded-xl bg-navy-50 p-3 text-xs leading-5 text-navy-800 dark:bg-navy-950 dark:text-navy-100">
              {pageText.text || t("tenders.documentsPanel.pageNoText")}
            </pre>
          </div>
        ) : null}
      </Modal>

      <Modal
        open={Boolean(evidenceModal)}
        onClose={() => setEvidenceModal(null)}
        title={t("tenders.requirements.evidence.title")}
        description={evidenceModal?.title || ""}
        size="lg"
        footer={
          <Button variant="outline" onClick={() => setEvidenceModal(null)}>
            {t("common.close")}
          </Button>
        }
      >
        {evidenceLoading ? (
          <Skeleton className="h-40 w-full" />
        ) : evidenceData ? (
          <div className="space-y-4">
            <p className="text-sm leading-6 muted">{t("tenders.requirements.evidence.help")}</p>

            {evidenceData.status && evidenceData.status !== "none" ? (
              <div className="rounded-xl border border-teal-200 bg-teal-50 p-3 text-sm dark:border-teal-900 dark:bg-teal-950/40">
                <p className="font-semibold text-teal-900 dark:text-teal-100">
                  {t("tenders.requirements.evidence.current")}:{" "}
                  {evidenceData.document_title || "—"}
                </p>
                <p className="mt-1 text-xs text-teal-800 dark:text-teal-200">
                  <Badge tone={evidenceStatusTone(evidenceData.status)}>
                    {t(`tenders.requirements.evidence.statuses.${evidenceData.status}`, {
                      defaultValue: evidenceData.status,
                    })}
                  </Badge>
                  {evidenceData.note ? ` · ${evidenceData.note}` : ""}
                  {evidenceData.validity_note ? ` · ${evidenceData.validity_note}` : ""}
                </p>
              </div>
            ) : null}

            <div>
              <label
                htmlFor="evidence-note"
                className="text-xs font-semibold text-navy-700 dark:text-navy-200"
              >
                {t("tenders.requirements.evidence.noteLabel")}
              </label>
              <textarea
                id="evidence-note"
                value={evidenceNote}
                onChange={(event) => setEvidenceNote(event.target.value)}
                rows={2}
                maxLength={1000}
                className="mt-1.5 w-full rounded-xl border border-navy-200 bg-white px-3 py-2 text-sm text-navy-900 outline-none transition focus:border-teal-400 focus:ring-2 focus:ring-teal-100 dark:border-navy-700 dark:bg-navy-900 dark:text-white dark:focus:ring-teal-900"
                placeholder={t("tenders.requirements.evidence.notePlaceholder")}
              />
            </div>

            {evidenceData.candidates.length ? (
              <div className="space-y-2">
                {evidenceData.candidates.map((candidate) => (
                  <article
                    key={candidate.document_version_id || candidate.document_id}
                    className="rounded-xl border border-navy-100 p-3 dark:border-navy-800"
                  >
                    <div className="flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
                      <div className="min-w-0">
                        <p className="text-sm font-semibold text-navy-900 dark:text-white">
                          {candidate.title}
                        </p>
                        <p className="mt-0.5 text-xs muted">
                          {candidate.document_type}
                          {candidate.issuer ? ` · ${candidate.issuer}` : ""}
                          {candidate.expiry_date
                            ? ` · ${t("tenders.requirements.evidence.expires", {
                                date: formatDate(candidate.expiry_date, currentLocale()),
                              })}`
                            : ""}
                        </p>
                      </div>
                      <Badge tone={candidate.valid ? "teal" : "red"}>
                        {Math.round(candidate.score * 100)}%
                      </Badge>
                    </div>
                    {candidate.reasons?.length ? (
                      <ul className="mt-2 space-y-0.5">
                        {candidate.reasons.map((reason) => (
                          <li key={reason} className="text-xs muted">
                            {reason}
                          </li>
                        ))}
                      </ul>
                    ) : null}
                    {candidate.validity_note ? (
                      <p className="mt-1 text-xs text-amber-700 dark:text-amber-300">
                        {candidate.validity_note}
                      </p>
                    ) : null}
                    <div className="mt-3 flex flex-wrap gap-1.5">
                      <Button
                        size="sm"
                        variant="subtle"
                        onClick={() => decideEvidence(candidate, "covered")}
                        loading={evidenceBusy === candidate.document_id}
                        disabled={!candidate.valid}
                      >
                        <Check className="h-3.5 w-3.5" />
                        {t("tenders.requirements.evidence.covered", {
                          defaultValue: "Covered by this document",
                        })}
                      </Button>
                      <Button
                        size="sm"
                        variant="ghost"
                        onClick={() => decideEvidence(candidate, "contradicted")}
                        loading={evidenceBusy === candidate.document_id}
                      >
                        <ShieldQuestion className="h-3.5 w-3.5" />
                        {t("tenders.requirements.evidence.contradict")}
                      </Button>
                    </div>
                  </article>
                ))}
              </div>
            ) : (
              <EmptyState
                icon={FileText}
                title={t("tenders.requirements.evidence.noCandidatesTitle")}
                description={t("tenders.requirements.evidence.noCandidatesBody")}
              />
            )}

            <div className="rounded-xl border border-navy-100 p-3 dark:border-navy-800">
              <p className="text-xs font-semibold text-navy-700 dark:text-navy-200">
                {t("tenders.requirements.evidence.noEvidence", {
                  defaultValue: "No document supports this requirement",
                })}
              </p>
              <p className="mt-1 text-xs muted">
                {t("tenders.requirements.evidence.noEvidenceHelp", {
                  defaultValue:
                    "Marking it missing is a decision about your own evidence; it never changes the tender text.",
                })}
              </p>
              <Button
                size="sm"
                variant="outline"
                className="mt-2"
                onClick={() => decideEvidence(null, "missing")}
                loading={evidenceBusy === "missing"}
              >
                <X className="h-3.5 w-3.5" />
                {t("tenders.requirements.evidence.markMissing", {
                  defaultValue: "Mark evidence missing",
                })}
              </Button>
            </div>
          </div>
        ) : null}
      </Modal>
    </Card>
  );
}
