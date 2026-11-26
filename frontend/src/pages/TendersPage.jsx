import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { useTranslation } from "react-i18next";
import {
  ArrowRight,
  Ban,
  BookmarkX,
  CalendarClock,
  FileText,
  MapPin,
  Search,
  ThumbsDown,
  TriangleAlert,
} from "lucide-react";

import { api, ApiError } from "../lib/api";
import { currentLocale } from "../i18n";
import { PageHeader } from "../components/ui/PageHeader";
import { Button } from "../components/ui/Button";
import { Card, CardContent } from "../components/ui/Card";
import { EmptyState } from "../components/ui/EmptyState";
import { Input, Label, Select } from "../components/ui/Field";
import { Pagination } from "../components/ui/Pagination";
import { Skeleton } from "../components/ui/Spinner";
import { Badge, StatusBadge } from "../components/ui/Badge";
import { SimilarityEvidence } from "../components/tender/SimilarityEvidence";
import { useToast } from "../contexts/ToastContext";
import {
  DEFAULT_TENDER_SORT,
  scorePercent,
  scoreTone,
  TENDER_SORT_OPTIONS,
  tierTone,
} from "../lib/discovery";
import { formatDate } from "../lib/utils";
import {
  matchedTerms,
  tenderDeadline,
  tenderReference,
  tenderStatus,
  tenderTitle,
} from "../lib/tender";

const PAGE_SIZE = 10;

export function TendersPage() {
  const { t } = useTranslation();
  const toast = useToast();
  const [tenders, setTenders] = useState([]);
  const [queryInput, setQueryInput] = useState("");
  const [query, setQuery] = useState("");
  const [sort, setSort] = useState(DEFAULT_TENDER_SORT);
  const [page, setPage] = useState(1);
  const [total, setTotal] = useState(0);
  const [pages, setPages] = useState(1);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [removingId, setRemovingId] = useState(null);
  const [markingId, setMarkingId] = useState(null);

  // "My tenders" is the saved and bid-preparation set, not the whole
  // discovery corpus. One server query returns it a page at a time, so the
  // order and the page count are the same as Discover's.
  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const params = new URLSearchParams({
        mine: "true",
        limit: String(PAGE_SIZE),
        offset: String((page - 1) * PAGE_SIZE),
        sort,
      });
      if (query) params.set("q", query);
      const data = await api.get(`/tenders?${params.toString()}`);
      setTenders(data.items || []);
      setTotal(data.total ?? data.items?.length ?? 0);
      setPages(data.pages ?? 1);
    } catch (err) {
      setError(err.message === "NETWORK_ERROR" ? t("errors.network") : err.message || t("errors.generic"));
    } finally {
      setLoading(false);
    }
  }, [page, query, sort, t]);

  useEffect(() => {
    load();
  }, [load]);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      const next = queryInput.trim();
      setQuery((current) => (current === next ? current : next));
      setPage((current) => (current === 1 ? current : 1));
    }, 350);
    return () => window.clearTimeout(timer);
  }, [queryInput]);

  // Removing a save is not the same statement as "not relevant": the first
  // only undoes the bookmark and leaves the tender to the profile filter, the
  // second removes it and teaches the matcher that this work is not yours.
  const removeSaved = async (tender) => {
    setRemovingId(tender.id);
    try {
      const payload = await api.delete(`/tenders/${tender.id}/save`);
      if (payload?.bidding) toast.info(t("tenders.biddingKept"));
      else toast.success(t("tenders.removed"));
      await load();
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : t("errors.generic"));
    } finally {
      setRemovingId(null);
    }
  };

  const markNotRelevant = async (tender) => {
    setMarkingId(tender.id);
    try {
      await api.post(`/tenders/${tender.id}/not-relevant`, {});
      toast.info(t("tenders.notRelevantSaved"));
      await load();
    } catch (err) {
      toast.error(
        err instanceof ApiError ? err.message : t("tenders.notRelevantFailed"),
      );
    } finally {
      setMarkingId(null);
    }
  };

  return (
    <div className="space-y-6">
      <PageHeader
        title={t("tenders.title")}
        description={t("tenders.subtitle")}
        actions={
          <Button variant="outline" size="sm" onClick={load} loading={loading && Boolean(tenders.length)}>
            {t("common.refresh")}
          </Button>
        }
      />

      {error ? (
        <div className="flex items-center justify-between gap-4 rounded-xl border border-red-200 bg-red-50 p-4 dark:border-red-900 dark:bg-red-950/40">
          <div className="flex items-start gap-2">
            <TriangleAlert className="mt-0.5 h-4 w-4 text-red-600" />
            <p className="text-sm text-red-700 dark:text-red-200">{error}</p>
          </div>
          <Button variant="outline" size="sm" onClick={load}>
            {t("common.retry")}
          </Button>
        </div>
      ) : null}

      <div className="grid gap-4 sm:grid-cols-[1fr_14rem] sm:items-end">
        <div>
          <Label htmlFor="tenders-search">{t("tenders.searchLabel")}</Label>
          <Input
            id="tenders-search"
            value={queryInput}
            onChange={(event) => setQueryInput(event.target.value)}
            placeholder={t("tenders.searchPlaceholder")}
          />
        </div>
        <div>
          <Label htmlFor="tenders-sort">{t("tenders.sort.label")}</Label>
          <Select
            id="tenders-sort"
            value={sort}
            onChange={(event) => {
              setSort(event.target.value);
              setPage(1);
            }}
          >
            {TENDER_SORT_OPTIONS.map((option) => (
              <option key={option} value={option}>
                {t(`tenders.sort.${option}`)}
              </option>
            ))}
          </Select>
        </div>
      </div>

      {loading ? (
        <div className="space-y-3">
          <Skeleton className="h-28 w-full" />
          <Skeleton className="h-28 w-full" />
        </div>
      ) : tenders.length ? (
        <div className="space-y-3">
          <div className="grid gap-3">
            {tenders.map((tender) => {
              const status = tenderStatus(tender);
              const terms = matchedTerms(tender);
              const relevance = tender.relevance || {};
              const fit = relevance.match_score ?? relevance.score;
              if (status === "terminated") {
                // The portal said this cycle is over. Its old details are not a
                // live tender, so the card states only what happened.
                return (
                  <Card
                    key={tender.id}
                    className="border-amber-200 dark:border-amber-900/50"
                  >
                    <CardContent className="flex items-start gap-3">
                      <span className="flex h-11 w-11 shrink-0 items-center justify-center rounded-xl bg-amber-50 text-amber-700 dark:bg-amber-950 dark:text-amber-300">
                        <Ban className="h-5 w-5" />
                      </span>
                      <div className="min-w-0">
                        <p className="text-sm font-semibold text-navy-900 dark:text-white">
                          {t("tenders.terminated.title")}
                        </p>
                        <p className="mt-1 text-sm muted">
                          {t("tenders.terminated.message")}
                        </p>
                      </div>
                    </CardContent>
                  </Card>
                );
              }
              return (
              <Card key={tender.id} className="transition hover:border-teal-200 hover:shadow-pop">
                <CardContent className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
                  <div className="flex min-w-0 items-start gap-3">
                    <span className="flex h-11 w-11 shrink-0 items-center justify-center rounded-xl bg-navy-100 text-navy-600 dark:bg-navy-800 dark:text-navy-300">
                      <FileText className="h-5 w-5" />
                    </span>
                    <div className="min-w-0">
                      <div className="flex flex-wrap items-center gap-2">
                        <Link
                          to={`/app/tenders/${tender.id}`}
                          className="truncate text-sm font-semibold text-navy-900 hover:text-teal-700 dark:text-white dark:hover:text-teal-300"
                        >
                          {tenderTitle(tender)}
                        </Link>
                        <StatusBadge
                          status={status}
                          label={t(`tenders.statuses.${status}`, { defaultValue: status })}
                        />
                        {terms.length ? (
                          <span className="rounded-full bg-teal-50 px-2 py-0.5 text-[10px] font-semibold text-teal-700 dark:bg-teal-950 dark:text-teal-300">
                            {terms.slice(0, 3).join(" · ")}
                          </span>
                        ) : null}
                        {relevance.tier != null && Number.isFinite(Number(relevance.tier)) ? (
                          <Badge tone={tierTone(relevance.tier)}>
                            {t("discover.fit.tier", { tier: relevance.tier })}
                          </Badge>
                        ) : null}
                        {Number.isFinite(Number(fit)) ? (
                          <Badge tone={scoreTone(fit)}>
                            {t("discover.relevance", {
                              percent: scorePercent(fit),
                            })}
                          </Badge>
                        ) : null}
                      </div>
                      <p className="mt-1 truncate text-xs muted">
                        {[tender.authority, tenderReference(tender)].filter(Boolean).join(" · ") || "—"}
                      </p>
                      <p className="mt-1.5 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-navy-500 dark:text-navy-300">
                        <span className="inline-flex items-center gap-1">
                          <CalendarClock className="h-3.5 w-3.5" />
                          {tenderDeadline(tender) ? formatDate(tenderDeadline(tender), currentLocale()) : "—"}
                        </span>
                        {tender.source_url ? (
                          <span className="inline-flex items-center gap-1">
                            <MapPin className="h-3.5 w-3.5" />
                            {t("tenders.source")}
                          </span>
                        ) : null}
                      </p>
                      {tender.similarity ? (
                        <SimilarityEvidence
                          similarity={tender.similarity}
                          compact
                          className="mt-2"
                        />
                      ) : null}
                    </div>
                  </div>
                  <div className="flex shrink-0 flex-col items-stretch gap-2 sm:items-end">
                    <Link
                      to={`/app/tenders/${tender.id}`}
                      className="inline-flex items-center justify-center gap-1.5 rounded-lg border border-navy-200 px-3 py-2 text-xs font-semibold text-navy-700 transition hover:border-teal-300 hover:bg-teal-50 hover:text-teal-800 dark:border-navy-700 dark:text-navy-200 dark:hover:bg-teal-950"
                    >
                      {t("tenders.open")}
                      <ArrowRight className="h-3.5 w-3.5" />
                    </Link>
                    <div className="flex flex-wrap gap-2">
                      {tender.saved ? (
                        <Button
                          size="sm"
                          variant="ghost"
                          onClick={() => removeSaved(tender)}
                          loading={removingId === tender.id}
                          title={t("tenders.removeSavedHint")}
                        >
                          <BookmarkX className="h-3.5 w-3.5" />
                          {t("tenders.removeSaved")}
                        </Button>
                      ) : null}
                      <Button
                        size="sm"
                        variant="ghost"
                        onClick={() => markNotRelevant(tender)}
                        loading={markingId === tender.id}
                        title={t("tenders.notRelevantHint")}
                      >
                        <ThumbsDown className="h-3.5 w-3.5" />
                        {t("tenders.notRelevant")}
                      </Button>
                    </div>
                  </div>
                </CardContent>
              </Card>
              );
            })}
          </div>
          <Pagination
            page={page}
            pages={pages}
            total={total}
            pageSize={PAGE_SIZE}
            onPage={setPage}
            disabled={loading}
          />
        </div>
      ) : (
        <Card>
          <CardContent>
            <EmptyState
              icon={query ? Search : FileText}
              title={query ? t("tenders.noResults") : t("tenders.emptyTitle")}
              description={query ? null : t("tenders.emptyBody")}
              action={
                query ? null : (
                  <Link
                    to="/app/discover"
                    className="inline-flex items-center gap-2 rounded-xl bg-teal-700 px-4 py-2.5 text-sm font-semibold text-white hover:bg-teal-800"
                  >
                    {t("dashboard.discoverCta")}
                    <ArrowRight className="h-4 w-4" />
                  </Link>
                )
              }
            />
          </CardContent>
        </Card>
      )}
    </div>
  );
}
