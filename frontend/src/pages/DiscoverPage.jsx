import { useCallback, useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useTranslation } from "react-i18next";
import {
  AlertTriangle,
  Ban,
  Compass,
  ExternalLink,
  Eye,
  Info,
  Layers,  RefreshCw,
  Search,
  Sparkles,
  ThumbsDown,
  XCircle,
} from "lucide-react";

import { PageHeader } from "../components/ui/PageHeader";
import { Button } from "../components/ui/Button";
import { Card, CardContent, CardHeader, CardTitle } from "../components/ui/Card";
import { Input, Label, Select } from "../components/ui/Field";
import { Badge } from "../components/ui/Badge";
import { EmptyState } from "../components/ui/EmptyState";
import { Pagination } from "../components/ui/Pagination";
import { Spinner } from "../components/ui/Spinner";
import { useToast } from "../contexts/ToastContext";
import { api, ApiError } from "../lib/api";
import { cn, formatDate, formatDateTime, formatNumber } from "../lib/utils";
import { capturePath, useCaptureUrl } from "../lib/useCaptureUrl";
import {
  bucketLabelKey,
  bucketTone,
  DEFAULT_TENDER_SORT,
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
} from "../lib/discovery";
import { tenderDeadline, tenderReference, tenderStatus, tenderTitle } from "../lib/tender";

const PAGE_SIZE = 10;

function StatTile({ label, value }) {
  return (
    <div className="rounded-xl border border-navy-100 px-4 py-3 dark:border-navy-800">
      <p className="text-lg font-semibold text-navy-900 dark:text-white">{value}</p>
      <p className="mt-0.5 text-xs muted">{label}</p>
    </div>
  );
}

function formatValue(amount, currency) {
  return `${currency || "INR"} ${formatNumber(amount)}`;
}

function sourceTypeLabel(sourceType) {
  return `discover.sourceType.${sourceType || "unknown"}`;
}

const TENDER_FIELDS = [
  ["authority", "discover.field.authority"],
  ["organisation_chain", "discover.field.organisation_chain"],
  ["reference", "discover.field.reference"],
  ["portal_tender_id", "discover.field.tender_id"],
  ["closing_at", "discover.field.deadline"],
  ["published_at", "discover.field.published"],
  ["value_amount", "discover.field.value"],
  ["location", "discover.field.location"],
];

function TenderCard({ item, locale, onSave, onDismiss, onNotRelevant, saving, marking }) {
  const { t } = useTranslation();
  const relevance = item.relevance || {};
  // The list is ordered by the gate's own fit (`match_desc`), so the badge
  // shows that same number instead of a second, differently scaled share that
  // could read "60%" above "50%" and make the order look wrong.
  const fit = relevance.match_score ?? relevance.score;
  const percent = scorePercent(fit);
  const bucket = relevance.bucket;
  const source = tenderSourceLink(item);
  const unverified = item.unverified || [];
  const deadline = tenderDeadline(item);
  const similarity = item.similarity || null;
  const similarityKey = similarityLabelKey(similarity);

  const values = {
    authority: item.authority,
    organisation_chain: item.organisation_chain,
    reference: tenderReference(item),
    portal_tender_id: item.portal_tender_id,
    closing_at: deadline ? formatDate(deadline, locale) : null,
    published_at: item.published_at ? formatDate(item.published_at, locale) : null,
    value_amount:
      item.value_amount === null || item.value_amount === undefined
        ? null
        : formatValue(item.value_amount, item.currency),
    location: item.location,
  };

  if (item.cycle_state === "terminated") {
    // The portal said this bidding cycle is over. Nothing from the cancelled
    // notice is shown as if it were still biddable.
    return (
      <Card className="overflow-hidden border-amber-200 dark:border-amber-900/50">
        <CardContent className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
          <div className="flex items-start gap-3">
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
          </div>
          <Button size="sm" variant="ghost" onClick={() => onDismiss(item)}>
            {t("discover.dismiss")}
          </Button>
        </CardContent>
      </Card>
    );
  }

  return (
    <Card
      className={cn(
        "overflow-hidden",
        // "possible" is shown at a lower weight than "strong": the gate was
        // not confident, so the card should not look confident either.
        bucket === "possible" && "border-amber-200 dark:border-amber-900/50",
        bucket === "hidden" && "opacity-90",
      )}
    >
      <CardContent className="space-y-4">
        <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <Badge tone={sourceKindTone(item.source_type)}>
                {t(sourceTypeLabel(item.source_type), {
                  defaultValue: item.source_type || "unknown",
                })}
              </Badge>
              {bucket ? (
                <Badge tone={bucketTone(bucket)}>
                  {t(bucketLabelKey(bucket), { defaultValue: bucket })}
                </Badge>
              ) : null}
              {relevance.tier != null && Number.isFinite(Number(relevance.tier)) ? (
                <Badge tone={tierTone(relevance.tier)}>
                  {t("discover.fit.tier", {
                    tier: relevance.tier,
                    defaultValue: `Fit tier ${relevance.tier}/7`,
                  })}
                </Badge>
              ) : null}
              <Badge tone={scoreTone(fit)}>
                {t("discover.relevance", { percent })}
              </Badge>
              {similarityKey ? (
                <Badge tone={similarityTone(similarity)}>
                  {t(similarityKey, { defaultValue: similarity?.answer })}
                </Badge>
              ) : null}
              {item.extracted_from === "listing_table" ? (
                <Badge tone="blue">{t("discover.fromListing")}</Badge>
              ) : null}
              {item.extracted_from === "search_result" ? (
                <Badge tone="amber">{t("discover.fromSearchResult")}</Badge>
              ) : null}
              {relevance.region_match ? (
                <Badge tone="teal">{t("discover.inYourRegion")}</Badge>
              ) : null}
              <Badge tone={tenderStatus(item) === "closed" ? "neutral" : "green"}>
                {t(`tenders.statuses.${tenderStatus(item)}`, {
                  defaultValue: tenderStatus(item),
                })}
              </Badge>
            </div>
            <h3 className="mt-2 text-base font-semibold leading-6 text-navy-900 dark:text-white">
              {tenderTitle(item)}
            </h3>
            <p className="mt-1 text-xs muted">
              {tenderSourceLabel(item) || t("discover.sourceNotRecorded")} ·{" "}
              {item.retrieved_at
                ? t("discover.retrievedAt", {
                    time: formatDateTime(item.retrieved_at, locale),
                  })
                : t("discover.notStated")}
            </p>
          </div>
          <div className="flex shrink-0 flex-col items-start gap-2 sm:items-end">
            <div className="flex flex-wrap gap-2 sm:justify-end">
              {source.href ? (
                <a
                  href={source.href}
                  target="_blank"
                  rel="noreferrer noopener"
                  className="inline-flex items-center gap-1.5 rounded-xl border border-navy-200 px-3 py-2 text-xs font-semibold text-navy-700 transition hover:bg-navy-50 dark:border-navy-700 dark:text-navy-200 dark:hover:bg-navy-800"
                >
                  <ExternalLink className="h-3.5 w-3.5" />
                  {source.stable ? t("discover.openHomepage") : t("discover.openSource")}
                </a>
              ) : null}
              <Button size="sm" variant="subtle" onClick={() => onSave(item)} loading={saving}>
                {t("discover.save")}
              </Button>
              <Button
                size="sm"
                variant="ghost"
                onClick={() => onNotRelevant(item)}
                loading={marking}
              >
                <ThumbsDown className="h-3.5 w-3.5" />
                {t("discover.notRelevant")}
              </Button>
              <Button size="sm" variant="ghost" onClick={() => onDismiss(item)}>
                {t("discover.dismiss")}
              </Button>
            </div>
          </div>
        </div>

        <dl className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          {TENDER_FIELDS.map(([key, label]) => {
            const value = values[key];
            return (
              <div key={key} className="rounded-xl border border-navy-100 px-3.5 py-3 dark:border-navy-800">
                <dt className="text-[11px] font-semibold uppercase tracking-wide text-navy-400">
                  {t(label, { defaultValue: key.replace(/_/g, " ") })}
                  {unverified.includes(key) ? (
                    <span className="ml-2 rounded bg-amber-100 px-1.5 py-0.5 text-[10px] font-semibold normal-case tracking-normal text-amber-800 dark:bg-amber-900/40 dark:text-amber-200">
                      {t("discover.unverified")}
                    </span>
                  ) : null}
                </dt>
                <dd
                  className={cn(
                    "mt-1 text-sm",
                    value ? "text-navy-900 dark:text-navy-50" : "italic text-navy-400",
                  )}
                >
                  {value || t("discover.notStated")}
                </dd>
              </div>
            );
          })}
        </dl>

        {bucket === "hidden" ? (
          <p className="flex items-start gap-2 rounded-xl border border-navy-100 px-3.5 py-2.5 text-xs leading-5 muted dark:border-navy-800">
            <Info className="mt-0.5 h-3.5 w-3.5 shrink-0" />
            {t("discover.hiddenHint")}
          </p>
        ) : null}

        {relevance.reasons?.length ? (
          <div>
            <p className="text-[11px] font-semibold uppercase tracking-wide text-navy-400">
              {t("discover.reasons")}
            </p>
            <ul className="mt-2 flex flex-wrap gap-2">
              {relevance.reasons.map((reason) => (
                <li
                  key={reason}
                  className="rounded-full bg-teal-50 px-2.5 py-1 text-[11px] font-medium text-teal-800 dark:bg-teal-950 dark:text-teal-200"
                >
                  {reason}
                </li>
              ))}
            </ul>
          </div>
        ) : null}

        {similarity ? (
          <div className="rounded-xl border border-navy-100 px-3.5 py-2.5 dark:border-navy-800">
            <p className="text-[11px] font-semibold uppercase tracking-wide text-navy-400">
              {t("discover.similarity.title")}
            </p>
            {similarity.reason ? (
              <p className="mt-1 text-xs leading-5 muted">{similarity.reason}</p>
            ) : null}
            {similarity.anchor_text ? (
              <p className="mt-1 border-l-2 border-teal-300 pl-2 text-xs italic text-navy-700 dark:border-teal-700 dark:text-navy-200">
                {t("discover.similarity.anchor", {
                  text: similarity.anchor_text,
                })}
              </p>
            ) : null}
            {similarity.capabilities?.length ? (
              <p className="mt-1 text-[11px] muted">
                {t("discover.similarity.matched", {
                  list: similarity.capabilities.join(", "),
                })}
              </p>
            ) : null}
            {similarity.needs_confirmation ? (
              <p className="mt-1 text-[11px] font-medium text-amber-700 dark:text-amber-300">
                {t("discover.similarity.pendingConfirm")}
              </p>
            ) : null}
          </div>
        ) : null}

        {item.fetch_state &&
        (item.fetch_state.last_error ||
          item.fetch_state.documents_present > 0 ||
          item.fetch_state.expected_covers > 0) ? (
          <p className="text-xs muted">
            {item.fetch_state.last_error
              ? t("discover.fetchError", {
                  error: item.fetch_state.last_error,
                  defaultValue: `Document fetch: ${item.fetch_state.last_error}`,
                })
              : item.fetch_state.expected_covers
                ? t("discover.documentsState", {
                    count: item.fetch_state.documents_present ?? 0,
                    covers: item.fetch_state.expected_covers,
                    defaultValue: `${item.fetch_state.documents_present ?? 0} of ${item.fetch_state.expected_covers} expected document(s) attached`,
                  })
                : t("discover.documentsAttached", {
                    count: item.fetch_state.documents_present ?? 0,
                    defaultValue: `${item.fetch_state.documents_present ?? 0} document(s) attached`,
                  })}
          </p>
        ) : null}
      </CardContent>
    </Card>
  );
}

const RUN_TONE = {
  queued: "teal",
  running: "teal",
  needs_you: "amber",
  done: "green",
  partial: "amber",
  failed: "red",
  cancelled: "neutral",
};

function SweepPanel({ sweep, lastSweep, onStart, onCancel, starting, cancelling }) {
  const { t } = useTranslation();
  const summary = sweepSummary(sweep?.last_summary);
  const active = sweepIsActive(lastSweep);
  const state = lastSweep?.state;

  return (
    <Card>
      <CardHeader
        action={
          <div className="flex shrink-0 items-center gap-2">
            <Badge tone={state ? RUN_TONE[state] || "neutral" : "neutral"}>
              {state
                ? t(`status.${state}`, { defaultValue: state })
                : t("discover.sweep.never")}
            </Badge>
            {active ? (
              <Button size="sm" variant="ghost" onClick={onCancel} loading={cancelling}>
                <XCircle className="h-3.5 w-3.5" />
                {t("discover.sweep.cancel")}
              </Button>
            ) : (
              <Button size="sm" variant="outline" onClick={onStart} loading={starting}>
                <Layers className="h-3.5 w-3.5" />
                {t("discover.sweep.start")}
              </Button>
            )}
          </div>
        }
      >
        <CardTitle>{t("discover.sweep.title")}</CardTitle>
      </CardHeader>
      <CardContent className="space-y-3 pt-3">
        <p className="text-sm leading-6 muted">{t("discover.sweep.body")}</p>
        {active ? (
          <>
            <p className="flex items-center gap-2 text-xs text-teal-700 dark:text-teal-300">
              <Spinner className="h-3.5 w-3.5" />
              {t("discover.sweep.running")}
            </p>
            {/* The cancellation contract, stated where the button is. */}
            <p className="text-xs leading-5 muted">{t("discover.sweep.cancelNote")}</p>
          </>
        ) : null}
        {!active && summary.pages !== null ? (
          <div className="rounded-xl border border-navy-100 px-3.5 py-3 dark:border-navy-800">
            <p className="text-[11px] font-semibold uppercase tracking-wide text-navy-400">
              {t("discover.sweep.last")}
            </p>
            <p className="mt-1 text-sm text-navy-900 dark:text-navy-50">
              {t("discover.sweep.counts", {
                pages: summary.pages,
                organisations: summary.organisations ?? 0,
                rows: summary.rows ?? 0,
                stored: summary.stored ?? 0,
                updated: summary.updated ?? 0,
              })}
            </p>
            {summary.notes ? <p className="mt-1 text-xs muted">{summary.notes}</p> : null}
          </div>
        ) : null}
        {lastSweep?.last_error ? (
          <p className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-xs leading-5 text-red-700 dark:border-red-900 dark:bg-red-950/40 dark:text-red-200">
            {lastSweep.last_error}
          </p>
        ) : null}
      </CardContent>
    </Card>
  );
}

function RunProgress({ run, onCancel, onAnswer, signaling }) {
  const { t } = useTranslation();
  const challenge = run?.challenge || null;
  const capture = useCaptureUrl(capturePath(challenge));
  const screenshot = useCaptureUrl(capturePath(challenge, "screenshot_url"));
  const [answer, setAnswer] = useState("");
  const [answerError, setAnswerError] = useState("");

  useEffect(() => {
    setAnswer("");
    setAnswerError("");
  }, [challenge?.id, challenge?.image_url]);

  if (!run) return null;
  const active = runIsActive(run);

  const submitAnswer = async (event) => {
    event.preventDefault();
    if (!challenge || !answer.trim()) return;
    setAnswerError("");
    const result = await onAnswer(answer.trim());
    setAnswer("");
    if (result && result.accepted === false && !result.error) {
      setAnswerError(
        t("discover.progress.answerRejected", {
          defaultValue:
            "That answer was not accepted. The portal drew a new picture; try the new one.",
        }),
      );
    } else if (result?.error) {
      setAnswerError(result.error);
    }
  };
  return (
    <Card>
      <CardHeader
        action={
          <div className="flex shrink-0 items-center gap-2">
            <Badge tone={RUN_TONE[run.state] || "neutral"}>
              {t(`status.${run.state}`, { defaultValue: run.state })}
            </Badge>
            {active ? (
              <Button size="sm" variant="ghost" onClick={onCancel}>
                <XCircle className="h-3.5 w-3.5" />
                {t("discover.progress.cancel")}
              </Button>
            ) : null}
          </div>
        }
      >
        <CardTitle>{t("discover.progress.title")}</CardTitle>
      </CardHeader>
      <CardContent className="space-y-4 pt-3">
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          <StatTile label={t("discover.progress.started")} value={formatDateTime(run.started_at)} />
          <StatTile label={t("discover.progress.attempts")} value={run.attempts} />
          <StatTile label={t("discover.progress.state")} value={run.state} />
          <StatTile
            label={t("discover.progress.finished")}
            value={run.finished_at ? formatDateTime(run.finished_at) : "—"}
          />
        </div>
        {active && run.state !== "needs_you" ? (
          <p className="flex items-center gap-2 text-xs text-teal-700 dark:text-teal-300">
            <Spinner className="h-3.5 w-3.5" />
            {t("discover.progress.running")}
          </p>
        ) : null}
        {run.state === "needs_you" ? (
          <div className="space-y-3 rounded-lg border border-amber-200 bg-amber-50 px-3 py-3 text-xs leading-5 text-amber-900 dark:border-amber-900 dark:bg-amber-950/40 dark:text-amber-100">
            <p className="font-medium">
              {t("discover.progress.needsYouTitle", {
                defaultValue: "Action needed — the portal is waiting for you",
              })}
            </p>
            <p>
              {challenge?.context?.instructions ||
                t("discover.progress.needsYouInstructions", {
                  defaultValue:
                    "Type the characters in the picture into the box below and press Submit. BidKaro carries your answer into the portal's form once and never stores it.",
                })}
            </p>
            <div className="flex flex-wrap items-start gap-3">
              {capture ? (
                <figure className="m-0">
                  <img
                    src={capture}
                    alt={t("discover.progress.challengeAlt", {
                      defaultValue: "The portal's challenge",
                    })}
                    className="max-h-40 w-auto rounded border border-amber-200 bg-white dark:border-amber-900"
                  />
                  <figcaption className="mt-1 text-[10px] opacity-75">
                    {t("discover.progress.challengeCaption", {
                      defaultValue: "The captcha itself",
                    })}
                  </figcaption>
                </figure>
              ) : null}
              {screenshot ? (
                <figure className="m-0 min-w-0">
                  <img
                    src={screenshot}
                    alt={t("discover.progress.screenshotAlt", {
                      defaultValue: "The portal page the captcha was on",
                    })}
                    className="max-h-40 w-auto rounded border border-amber-200 dark:border-amber-900"
                  />
                  <figcaption className="mt-1 text-[10px] opacity-75">
                    {t("discover.progress.screenshotCaption", {
                      defaultValue: "The full portal page",
                    })}
                  </figcaption>
                </figure>
              ) : null}
            </div>
            {challenge ? (
              <form onSubmit={submitAnswer} className="flex flex-wrap items-end gap-2">
                <label className="flex flex-col gap-1 text-[11px] font-semibold">
                  {t("discover.progress.challengeInputLabel", {
                    defaultValue: "Characters in the picture",
                  })}
                  <Input
                    value={answer}
                    onChange={(event) => setAnswer(event.target.value)}
                    autoComplete="off"
                    spellCheck={false}
                    maxLength={16}
                    className="w-44 bg-white text-navy-900 dark:bg-navy-900 dark:text-white"
                    disabled={signaling}
                  />
                </label>
                <Button
                  type="submit"
                  size="sm"
                  disabled={signaling || !answer.trim()}
                >
                  {signaling ? <Spinner className="h-3.5 w-3.5" /> : null}
                  {t("discover.progress.submitAnswer", {
                    defaultValue: "Submit answer",
                  })}
                </Button>
              </form>
            ) : null}
            {answerError ? (
              <p className="text-[11px] font-medium text-red-700 dark:text-red-300">
                {answerError}
              </p>
            ) : null}
            <p className="text-[11px] opacity-80">
              {t("discover.progress.answerHint", {
                defaultValue:
                  "Your answer is carried into the portal's form once and is never saved. The BidKaro browser window stays minimized; everything you need is pictured here.",
              })}
            </p>
          </div>
        ) : null}
        {run.last_error ? (
          <p className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-xs leading-5 text-red-700 dark:border-red-900 dark:bg-red-950/40 dark:text-red-200">
            {run.last_error}
          </p>
        ) : null}
      </CardContent>
    </Card>
  );
}

export function DiscoverPage() {
  const { t, i18n } = useTranslation();
  const toast = useToast();
  const [searchParams] = useSearchParams();
  const [queryInput, setQueryInput] = useState(searchParams.get("q") || "");
  const [query, setQuery] = useState(searchParams.get("q") || "");
  const [sort, setSort] = useState(DEFAULT_TENDER_SORT);
  const [page, setPage] = useState(1);
  const [total, setTotal] = useState(0);
  const [pages, setPages] = useState(1);
  const [profileComplete, setProfileComplete] = useState(true);
  const [status, setStatus] = useState(null);
  const [items, setItems] = useState([]);
  const [sources, setSources] = useState([]);
  const [runDoc, setRunDoc] = useState(null);
  const [loading, setLoading] = useState(true);
  const [running, setRunning] = useState(false);
  const [needsProfile, setNeedsProfile] = useState(false);
  const [savingId, setSavingId] = useState(null);
  const [signaling, setSignaling] = useState(false);
  const [markingId, setMarkingId] = useState(null);
  const [includeHidden, setIncludeHidden] = useState(false);
  const [sweepJob, setSweepJob] = useState(null);
  const [startingSweep, setStartingSweep] = useState(false);
  const [cancellingSweep, setCancellingSweep] = useState(false);
  const [similarityJob, setSimilarityJob] = useState(null);
  const [startingSimilarity, setStartingSimilarity] = useState(false);
  const [similarityError, setSimilarityError] = useState("");

  const locale = i18n.language === "hi" ? "hi-IN" : "en-IN";

  // The list is paged and searched by the server, so the page is always cut
  // from the whole visible corpus rather than from a batch loaded once.
  const load = useCallback(async () => {
    setLoading(true);
    try {
      const params = new URLSearchParams({
        limit: String(PAGE_SIZE),
        offset: String((page - 1) * PAGE_SIZE),
        sort,
      });
      if (query) params.set("q", query);
      // "Show everything" is a server request: the gate's hidden rows are not
      // fetched and then filtered out in the browser, they come back labelled.
      if (includeHidden) params.set("include_hidden", "true");
      const [statusResponse, tenderResponse, runResponse, sourceResponse] = await Promise.all([
        api.get("/discovery/status"),
        api.get(`/tenders?${params.toString()}`),
        api.get("/discovery/runs?limit=1"),
        api.get("/discovery/sources?limit=20"),
      ]);
      setStatus(statusResponse);
      setItems(tenderResponse.items || []);
      setTotal(tenderResponse.total ?? tenderResponse.items?.length ?? 0);
      setPages(tenderResponse.pages ?? 1);
      setProfileComplete(tenderResponse.profile_complete !== false);
      setSources(sourceResponse.items || []);
      const latestRun = runResponse.items?.[0] || null;
      setRunDoc(latestRun);
      setRunning(runIsActive(latestRun));
    } catch (error) {
      if (!(error instanceof ApiError)) throw error;
      toast.error(t("errors.generic"));
    } finally {
      setLoading(false);
    }
  }, [page, query, sort, includeHidden, t, toast]);

  useEffect(() => {
    load();
  }, [load]);

  // Typing filters after a short pause instead of on every keystroke, and any
  // change of words or order starts again at page one.
  useEffect(() => {
    const timer = window.setTimeout(() => {
      const next = queryInput.trim();
      setQuery((current) => (current === next ? current : next));
      setPage((current) => (current === 1 ? current : 1));
    }, 350);
    return () => window.clearTimeout(timer);
  }, [queryInput]);

  const activeRunId = runIsActive(runDoc) ? runDoc?.id : null;

  useEffect(() => {
    if (!activeRunId) return undefined;
    let cancelled = false;
    const timer = window.setInterval(async () => {
      try {
        const job = await api.get(`/discovery/runs/${activeRunId}`);
        if (cancelled) return;
        setRunDoc(job);
        if (!runIsActive(job)) {
          setRunning(false);
          if (job.state === "done") toast.success(t("discover.runSuccess"));
          await load();
        }
      } catch (error) {
        if (cancelled) return;
        if (error instanceof ApiError && error.status === 404) {
          setRunning(false);
          return;
        }
        toast.error(error instanceof ApiError ? error.message : t("errors.generic"));
      }
    }, 2000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [activeRunId, load, toast, t]);

  const sweepStatus = status?.sweep || null;
  const lastSweep = sweepJob || sweepStatus?.last_sweep || null;
  const sweepActive = sweepIsActive(lastSweep);

  // A sweep is a long job, so the page polls it the way it polls a run, but
  // there is no challenge to answer: a sweep never parks for a human.
  useEffect(() => {
    if (!sweepActive || !lastSweep?.id) return undefined;
    let cancelled = false;
    const timer = window.setInterval(async () => {
      try {
        const job = await api.get(`/discovery/sweeps/${lastSweep.id}`);
        if (cancelled) return;
        setSweepJob(job);
        if (!sweepIsActive(job)) {
          if (job.state === "done") toast.success(t("discover.runSuccess"));
          await load();
        }
      } catch (error) {
        if (cancelled) return;
        if (error instanceof ApiError && error.status === 404) return;
        toast.error(error instanceof ApiError ? error.message : t("errors.generic"));
      }
    }, 2000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [sweepActive, lastSweep?.id, load, toast, t]);

  // The comparison is a background job like a sweep: the list waits for no
  // model. It is polled, and the page is re-read when it finishes so the cards
  // show the stored answer instead of a stale guess.
  const similarityActive = runIsActive(similarityJob);
  useEffect(() => {
    if (!similarityActive || !similarityJob?.id) return undefined;
    let cancelled = false;
    const timer = window.setInterval(async () => {
      try {
        const job = await api.get(`/jobs/${similarityJob.id}`);
        if (cancelled) return;
        setSimilarityJob(job);
        if (!runIsActive(job)) {
          if (job.state === "done") {
            toast.success(t("discover.similarity.done"));
          } else if (job.last_error) {
            toast.error(job.last_error);
          }
          await load();
        }
      } catch (error) {
        if (cancelled) return;
        if (error instanceof ApiError && error.status === 404) return;
        toast.error(error instanceof ApiError ? error.message : t("errors.generic"));
      }
    }, 2000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [similarityActive, similarityJob?.id, load, toast, t]);

  const startSimilarity = async () => {
    if (!items.length) return;
    setStartingSimilarity(true);
    setSimilarityError("");
    try {
      const payload = await api.post("/discovery/similarity", {
        tender_ids: items.map((item) => item.id),
      });
      setSimilarityJob(payload.job);
      toast.info(t("discover.similarity.started"));
    } catch (error) {
      const message =
        error instanceof ApiError
          ? error.message
          : t("discover.similarity.failed");
      // A 422 here means there is nothing confirmed to compare with, so the
      // answer is a route to the profile, not a transient error toast.
      if (error instanceof ApiError && error.status === 422) {
        setSimilarityError(message);
      } else {
        toast.error(message);
      }
    } finally {
      setStartingSimilarity(false);
    }
  };

  const startSweep = async () => {
    setStartingSweep(true);
    try {
      const payload = await api.post("/discovery/sweeps", {});
      setSweepJob(payload.job);
      toast.success(t("discover.sweep.started"));
      await load();
    } catch (error) {
      toast.error(error instanceof ApiError ? error.message : t("discover.sweep.failed"));
    } finally {
      setStartingSweep(false);
    }
  };

  const cancelSweep = async () => {
    if (!lastSweep?.id) return;
    setCancellingSweep(true);
    try {
      const job = await api.post(`/discovery/sweeps/${lastSweep.id}/cancel`, {});
      setSweepJob(job);
      toast.info(t("discover.sweep.cancelled"));
      // Re-read the status so the summary shown is the sweep that just stopped,
      // not the one before it.
      await load();
    } catch (error) {
      toast.error(error instanceof ApiError ? error.message : t("errors.generic"));
    } finally {
      setCancellingSweep(false);
    }
  };

  const markNotRelevant = async (item) => {
    setMarkingId(item.id);
    try {
      await api.post(`/tenders/${item.id}/not-relevant`, {});
      toast.info(t("discover.notRelevantSaved"));
      // The verdict changed for this company, so the page is re-read rather
      // than patched: the row may move bucket, not just disappear.
      await load();
    } catch (error) {
      toast.error(
        error instanceof ApiError ? error.message : t("discover.notRelevantFailed"),
      );
    } finally {
      setMarkingId(null);
    }
  };

  const run = async (event) => {
    event.preventDefault();
    setRunning(true);
    setNeedsProfile(false);
    try {
      const payload = await api.post("/discovery/runs", {});
      setRunDoc(payload.job);
      setRunning(runIsActive(payload.job));
      if (runIsActive(payload.job)) {
        toast.success(t("discover.runStarted"));
      } else {
        toast.success(t("discover.runSuccess"));
      }
    } catch (error) {
      setRunning(false);
      if (error instanceof ApiError && error.status === 422) {
        setNeedsProfile(true);
        toast.error(error.message);
      } else if (error instanceof ApiError && error.status === 503) {
        setStatus((current) => ({ ...(current || {}), enabled: false }));
        toast.error(error.message);
      } else {
        toast.error(error instanceof ApiError ? error.message : t("discover.runFailed"));
      }
    }
  };

  const saveTender = async (item) => {
    setSavingId(item.id);
    try {
      await api.post(`/tenders/${item.id}/save`, {});
      toast.success(t("discover.saved"));
    } catch (error) {
      toast.error(error instanceof ApiError ? error.message : t("discover.saveFailed"));
    } finally {
      setSavingId(null);
    }
  };

  const dismissTender = async (item) => {
    try {
      await api.post(`/tenders/${item.id}/dismiss`, {});
      setItems((current) => current.filter((row) => row.id !== item.id));
      setTotal((current) => Math.max(0, current - 1));
      toast.info(t("discover.dismissed"));
      // Refill the page from the server so the last slot is not left empty.
      await load();
    } catch (error) {
      toast.error(error instanceof ApiError ? error.message : t("errors.generic"));
    }
  };

  const cancelRun = async () => {
    if (!runDoc?.id) return;
    try {
      const job = await api.post(`/discovery/runs/${runDoc.id}/cancel`, {});
      setRunDoc(job);
      setRunning(false);
      toast.info(t("discover.cancelled"));
    } catch (error) {
      toast.error(error instanceof ApiError ? error.message : t("errors.generic"));
    }
  };

  const answerRun = async (answer) => {
    if (!runDoc?.id) return { accepted: false };
    setSignaling(true);
    try {
      const result = await api.post(`/jobs/${runDoc.id}/challenge/answer`, {
        answer,
      });
      if (result.accepted) {
        toast.success(
          t("discover.progress.answerAccepted", {
            defaultValue: "Answer sent — the run is continuing in the same browser.",
          }),
        );
      }
      setRunDoc(result.job);
      setRunning(runIsActive(result.job));
      return result;
    } catch (error) {
      const message =
        error instanceof ApiError ? error.message : t("errors.generic");
      toast.error(message);
      return { accepted: false, error: message };
    } finally {
      setSignaling(false);
    }
  };

  const configured = status?.enabled !== false;
  const caps = status?.caps || {};

  return (
    <div className="space-y-6">
      <PageHeader
        title={t("discover.title")}
        description={t("discover.subtitle")}
        actions={
          <Button variant="outline" onClick={load} loading={loading} disabled={running}>
            <RefreshCw className="h-4 w-4" />
            {t("common.refresh")}
          </Button>
        }
      />

      <Card>
        <CardContent className="space-y-4">
          <div className="flex items-start gap-3">
            <span
              className={cn(
                "mt-0.5 flex h-9 w-9 shrink-0 items-center justify-center rounded-xl",
                configured
                  ? "bg-teal-50 text-teal-700 dark:bg-teal-950 dark:text-teal-300"
                  : "bg-amber-50 text-amber-700 dark:bg-amber-950 dark:text-amber-300",
              )}
            >
              {configured ? <Compass className="h-5 w-5" /> : <AlertTriangle className="h-5 w-5" />}
            </span>
            <div className="min-w-0">
              <p className="text-sm font-semibold text-navy-900 dark:text-white">
                {configured ? t("discover.statusConfigured") : t("discover.statusUnconfigured")}
              </p>
              <p className="mt-1 text-sm leading-6 muted">
                {configured ? t("discover.statusBody") : t("discover.unconfiguredBody")}
              </p>
            </div>
          </div>

          {needsProfile ? (
            <div className="flex flex-col gap-2 rounded-xl border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-900 dark:border-amber-900 dark:bg-amber-950 dark:text-amber-100 sm:flex-row sm:items-center sm:justify-between">
              <span>{t("discover.needProfile")}</span>
              <Link
                to="/app/company"
                className="inline-flex shrink-0 items-center gap-1.5 font-semibold underline underline-offset-2"
              >
                {t("discover.openProfile")}
              </Link>
            </div>
          ) : null}

          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <StatTile label={t("discover.stats.listed")} value={status?.tender_count ?? 0} />
            <StatTile label={t("discover.sources")} value={status?.source_count ?? 0} />
            <StatTile
              label={t("discover.lastRun")}
              value={
                status?.last_completed_run_at
                  ? formatDateTime(status.last_completed_run_at, locale)
                  : t("discover.never")
              }
            />
            <StatTile label={t("discover.due")} value={status?.due ? t("common.yes") : t("common.no")} />
          </div>

          <div className="grid gap-4 sm:grid-cols-[1fr_auto] sm:items-end">
            <div>
              <Label htmlFor="discover-search">{t("discover.searchLabel")}</Label>
              <Input
                id="discover-search"
                value={queryInput}
                onChange={(event) => setQueryInput(event.target.value)}
                placeholder={t("discover.searchPlaceholder", {
                  defaultValue: "Title, authority, reference or location",
                })}
              />
            </div>
            <Button
              onClick={run}
              loading={running}
              disabled={!configured || running || sweepActive}
            >
              {!running ? <Search className="h-4 w-4" /> : null}
              {running ? t("discover.running") : t("discover.run")}
            </Button>
          </div>

          <p className="flex items-start gap-2 text-xs leading-5 muted">
            <Info className="mt-0.5 h-3.5 w-3.5 shrink-0" />
            {t("discover.runNote")}
          </p>
          {caps.max_notices ? (
            <p className="text-xs muted">
              {t("discover.caps", {
                notices: caps.max_notices,
                hours: caps.interval_hours ?? "—",
                defaultValue: `Each run reads up to ${caps.max_notices} notices, every ${caps.interval_hours ?? "—"} hours.`,
              })}
            </p>
          ) : null}
        </CardContent>
      </Card>

      <SweepPanel
        sweep={sweepStatus}
        lastSweep={lastSweep}
        onStart={startSweep}
        onCancel={cancelSweep}
        starting={startingSweep}
        cancelling={cancellingSweep}
      />

      {runDoc ? (
        <RunProgress
          run={runDoc}
          onCancel={cancelRun}
          onAnswer={answerRun}
          signaling={signaling}
        />
      ) : null}

      {!profileComplete ? (
        <Card className="border-amber-200 dark:border-amber-900/50">
          <CardContent>
            <EmptyState
              icon={Compass}
              title={t("discover.profileMissingTitle", {
                defaultValue: "Your company profile is incomplete",
              })}
              description={t("discover.profileMissingBody", {
                defaultValue:
                  "Add the work you do - services, sectors or keywords - and the list below will show only the tenders that match it.",
              })}
              action={
                <Link
                  to="/app/company"
                  className="inline-flex items-center gap-2 rounded-xl bg-teal-700 px-4 py-2.5 text-sm font-semibold text-white hover:bg-teal-800"
                >
                  {t("discover.openProfile")}
                </Link>
              }
            />
          </CardContent>
        </Card>
      ) : null}

      <section className="space-y-4">
        <div className="flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
          <div>
            <h2 className="text-lg font-semibold tracking-tight text-navy-900 dark:text-white">
              {profileComplete ? t("discover.results") : t("discover.allTenders")}
            </h2>
            <p className="text-sm muted">
              {profileComplete
                ? t("discover.resultsHint", { count: total })
                : t("discover.allTendersNote", {
                    defaultValue:
                      "The whole corpus, not filtered to your profile because no work is declared yet.",
                  })}
            </p>
            {includeHidden ? (
              <p className="mt-1 text-xs muted">{t("discover.showAllOn")}</p>
            ) : null}
          </div>
          <div className="flex flex-col gap-2 sm:flex-row sm:items-end">
            <div className="sm:w-56">
              <Label htmlFor="discover-sort">{t("tenders.sort.label")}</Label>
              <Select
                id="discover-sort"
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
            <Button
              size="sm"
              variant={includeHidden ? "subtle" : "outline"}
              aria-pressed={includeHidden}
              onClick={() => {
                setIncludeHidden((current) => !current);
                setPage(1);
              }}
            >
              <Eye className="h-3.5 w-3.5" />
              {t("discover.showAll")}
            </Button>
            <Button
              size="sm"
              variant="outline"
              onClick={startSimilarity}
              loading={startingSimilarity}
              disabled={!items.length || similarityActive}
            >
              <Sparkles className="h-3.5 w-3.5" />
              {similarityActive
                ? t("discover.similarity.comparing")
                : t("discover.similarity.compare")}
            </Button>
          </div>
        </div>

        {similarityError ? (
          <div className="flex flex-col gap-2 rounded-xl border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-900 dark:border-amber-900 dark:bg-amber-950 dark:text-amber-100 sm:flex-row sm:items-center sm:justify-between">
            <span>{similarityError}</span>
            <Link
              to="/app/documents"
              className="inline-flex shrink-0 items-center gap-1.5 font-semibold underline underline-offset-2"
            >
              {t("discover.similarity.openUnderstanding")}
            </Link>
          </div>
        ) : null}

        {similarityActive ? (
          <p className="flex items-center gap-2 text-xs text-teal-700 dark:text-teal-300">
            <Spinner className="h-3.5 w-3.5" />
            {t("discover.similarity.started")}
          </p>
        ) : null}

        {loading ? (
          <div className="flex items-center gap-3 rounded-xl border border-navy-100 px-5 py-8 dark:border-navy-800">
            <Spinner />
            <span className="text-sm muted">{t("discover.loading")}</span>
          </div>
        ) : items.length ? (
          <div className="space-y-4">
            {items.map((item) => (
              <TenderCard
                key={item.id}
                item={item}
                locale={locale}
                onSave={saveTender}
                onDismiss={dismissTender}
                onNotRelevant={markNotRelevant}
                saving={savingId === item.id}
                marking={markingId === item.id}
              />
            ))}
          </div>
        ) : (
          <EmptyState
            icon={query ? Search : Sparkles}
            title={query ? t("discover.noResults", { defaultValue: "No tenders match that filter." }) : t("discover.emptyTitle")}
            description={query ? null : t("discover.emptyStored")}
          />
        )}

        <Pagination
          page={page}
          pages={pages}
          total={total}
          pageSize={PAGE_SIZE}
          onPage={setPage}
          disabled={loading || running}
        />
      </section>

      {sources.length ? (
        <section className="space-y-3">
          <div>
            <h2 className="text-lg font-semibold tracking-tight text-navy-900 dark:text-white">
              {t("discover.sources")}
            </h2>
            <p className="text-sm muted">{t("discover.sourcesHint")}</p>
          </div>
          <div className="grid gap-3 sm:grid-cols-2">
            {sources.map((source) => (
              <div
                key={source.url}
                className="flex items-start justify-between gap-3 rounded-xl border border-navy-100 bg-white p-4 dark:border-navy-800 dark:bg-navy-900"
              >
                <div className="min-w-0">
                  <div className="flex flex-wrap items-center gap-2">
                    <Badge tone="neutral">{t("discover.indexPage")}</Badge>
                    <span className="text-[11px] muted">
                      {t("discover.seenCount", {
                        count: source.seen_count,
                        defaultValue: `Seen ${source.seen_count} time(s)`,
                      })}
                    </span>
                  </div>
                  <p className="mt-1.5 break-all text-sm font-semibold text-navy-900 dark:text-white">
                    {source.url}
                  </p>
                  {source.reason ? <p className="mt-0.5 text-xs muted">{source.reason}</p> : null}
                  {source.last_seen_at ? (
                    <p className="mt-0.5 text-[11px] muted">
                      {formatDateTime(source.last_seen_at, locale)}
                    </p>
                  ) : null}
                </div>
                <a
                  href={source.url}
                  target="_blank"
                  rel="noreferrer noopener"
                  className="inline-flex shrink-0 items-center gap-1.5 rounded-xl border border-navy-200 px-3 py-2 text-xs font-semibold text-navy-700 transition hover:bg-navy-50 dark:border-navy-700 dark:text-navy-200 dark:hover:bg-navy-800"
                >
                  <ExternalLink className="h-3.5 w-3.5" />
                  {t("discover.openSource")}
                </a>
              </div>
            ))}
          </div>
        </section>
      ) : null}
    </div>
  );
}
