import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";
import {
  ArrowRight,
  CalendarClock,
  CheckCircle2,
  ClipboardList,
  FileText,
  FolderCheck,
  Gauge,
  RefreshCw,
  Sparkles,
  Target,
  TrendingUp,
  TriangleAlert,
} from "lucide-react";

import { api } from "../lib/api";
import { currentLocale } from "../i18n";
import { useAuth } from "../contexts/AuthContext";
import { PageHeader } from "../components/ui/PageHeader";
import { Card, CardContent, CardHeader, CardTitle } from "../components/ui/Card";
import { Button } from "../components/ui/Button";
import { EmptyState } from "../components/ui/EmptyState";
import { Progress } from "../components/ui/Progress";
import { Skeleton } from "../components/ui/Spinner";
import { StatusBadge } from "../components/ui/Badge";
import { cn, formatDate, formatDateTime, greetingKey } from "../lib/utils";
import { companyCompleteness, companyToForm } from "../lib/companyForm";
import {
  tenderDeadline,
  tenderReference,
  tenderStatus,
  tenderTitle,
} from "../lib/tender";

const METRICS = [
  { key: "activeBids", icon: Target, href: "/app/tenders", tone: "teal" },
  { key: "openTasks", icon: ClipboardList, href: "/app/checklist", tone: "amber" },
  { key: "upcomingDeadlines", icon: CalendarClock, href: "/app/reminders", tone: "red" },
  { key: "evidenceDocuments", icon: FileText, href: "/app/documents", tone: "blue" },
];

const TONES = {
  teal: "bg-teal-50 text-teal-700 dark:bg-teal-950 dark:text-teal-300",
  amber: "bg-amber-50 text-amber-700 dark:bg-amber-950 dark:text-amber-300",
  red: "bg-red-50 text-red-700 dark:bg-red-950 dark:text-red-300",
  blue: "bg-sky-50 text-sky-700 dark:bg-sky-950 dark:text-sky-300",
};

const ACTION_TONES = {
  warning: "border-amber-200 bg-amber-50/60 dark:border-amber-900 dark:bg-amber-950/30",
  danger: "border-red-200 bg-red-50/60 dark:border-red-900 dark:bg-red-950/30",
  success: "border-teal-200 bg-teal-50/60 dark:border-teal-900 dark:bg-teal-950/30",
  info: "border-navy-100 bg-navy-50/60 dark:border-navy-800 dark:bg-navy-800/40",
};

const OPEN_TASK_STATES = new Set(["open", "in_progress", "blocked"]);

function MetricCard({ metric, value, loading }) {
  const { t } = useTranslation();
  const Icon = metric.icon;
  return (
    <Link
      to={metric.href}
      className="surface group p-4 transition hover:-translate-y-0.5 hover:border-teal-200 hover:shadow-pop sm:p-5"
    >
      <div className="flex items-start justify-between">
        <div className={cn("flex h-10 w-10 items-center justify-center rounded-xl", TONES[metric.tone])}>
          <Icon className="h-5 w-5" aria-hidden="true" />
        </div>
        <ArrowRight className="h-4 w-4 text-navy-300 transition group-hover:translate-x-0.5 group-hover:text-teal-600" />
      </div>
      {loading ? (
        <Skeleton className="mt-4 h-8 w-14" />
      ) : (
        <p className="mt-4 text-[26px] font-semibold leading-none tracking-tight text-navy-900 dark:text-white">
          {value}
        </p>
      )}
      <p className="mt-2 text-sm font-medium text-navy-700 dark:text-navy-200">
        {t(`dashboard.metrics.${metric.key}`)}
      </p>
      <p className="mt-0.5 text-xs muted">{t(`dashboard.metricsHelp.${metric.key}`)}</p>
    </Link>
  );
}

function TenderRow({ tender }) {
  const { t } = useTranslation();
  const deadline = tenderDeadline(tender);
  const status = tenderStatus(tender);
  return (
    <li>
      <Link
        to={`/app/tenders/${tender.id}`}
        className="flex items-center gap-3 rounded-xl px-2 py-3 transition hover:bg-navy-50 dark:hover:bg-navy-800"
      >
        <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-navy-100 text-navy-600 dark:bg-navy-800 dark:text-navy-300">
          <FileText className="h-4 w-4" />
        </span>
        <div className="min-w-0 flex-1">
          <p className="truncate text-sm font-semibold text-navy-900 dark:text-white">
            {tenderTitle(tender)}
          </p>
          <p className="truncate text-xs muted">
            {[tender.authority, tenderReference(tender)].filter(Boolean).join(" · ") ||
              t("tenders.source")}
          </p>
        </div>
        <div className="hidden text-right sm:block">
          <p className="text-xs font-medium text-navy-700 dark:text-navy-200">
            {deadline ? formatDate(deadline, currentLocale()) : "—"}
          </p>
          <StatusBadge
            status={status}
            label={t(`tenders.statuses.${status}`, { defaultValue: status })}
          />
        </div>
        <ArrowRight className="h-4 w-4 shrink-0 text-navy-300" />
      </Link>
    </li>
  );
}

export function DashboardPage() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { session, refreshSession } = useAuth();
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    setError("");
    setLoading(true);
    try {
      const [tenders, saved, bidding, documents, reminders, changes, checklist] =
        await Promise.all([
          api.get("/tenders?limit=50"),
          api.get("/tenders?saved=true&limit=200"),
          api.get("/tenders?bidding=true&limit=200"),
          api.get("/company/documents"),
          api.get("/reminders?limit=200"),
          api.get("/changes?limit=50"),
          api.get("/checklist?limit=2000"),
        ]);
      setData({
        tenders: tenders.items || [],
        saved: saved.items || [],
        bidding: bidding.items || [],
        documents: documents.items || [],
        reminders: reminders.items || [],
        changes: changes.items || [],
        checklist: checklist.items || [],
      });
    } catch (err) {
      setError(
        err.message === "NETWORK_ERROR" ? t("errors.network") : err.message || t("errors.generic"),
      );
    } finally {
      setLoading(false);
    }
  }, [t]);

  useEffect(() => {
    load();
  }, [load]);

  const profileCompleteness = useMemo(
    () => companyCompleteness(companyToForm(session?.company)),
    [session?.company],
  );

  const openTasks = useMemo(
    () => (data?.checklist || []).filter((task) => OPEN_TASK_STATES.has(task.status)),
    [data],
  );
  const openReminders = useMemo(
    () => (data?.reminders || []).filter((reminder) => reminder.state === "open"),
    [data],
  );
  const pendingChanges = useMemo(
    () => (data?.changes || []).filter((change) => change.review_state === "pending"),
    [data],
  );

  const activeBidIds = new Set(
    [...(data?.bidding || []), ...(data?.saved || [])].map((tender) => tender.id),
  );

  const metrics = {
    activeBids: activeBidIds.size,
    openTasks: openTasks.length,
    upcomingDeadlines: openReminders.length,
    evidenceDocuments: data?.documents?.length || 0,
  };

  const priorityActions = useMemo(() => {
    const actions = [];
    if (!profileCompleteness) {
      actions.push({
        key: "profile",
        title: t("dashboard.actions.completeProfile", {
          defaultValue: "Complete your company profile",
        }),
        description: t("dashboard.actions.completeProfileBody", {
          defaultValue:
            "Discovery and fit scoring need your sectors, services and keywords.",
        }),
        href: "/app/company",
        tone: "warning",
      });
    }
    for (const reminder of openReminders.slice(0, 3)) {
      actions.push({
        key: `reminder-${reminder.id}`,
        title: reminder.title,
        description: reminder.body,
        href: reminder.href || "/app/reminders",
        tone: "danger",
      });
    }
    for (const task of openTasks.slice(0, 2)) {
      actions.push({
        key: `task-${task.id}`,
        title: task.title,
        description: task.acceptance_criteria || task.trigger || "",
        href: `/app/tenders/${task.tender_id}?tab=${
          task.phase === "post_award" ? "obligations" : "tasks"
        }`,
        tone: "warning",
      });
    }
    if (pendingChanges.length) {
      actions.push({
        key: "changes",
        title: t("dashboard.actions.reviewChanges", {
          defaultValue: "Review source changes",
          count: pendingChanges.length,
        }),
        description: t("dashboard.actions.reviewChangesBody", {
          count: pendingChanges.length,
          defaultValue: `${pendingChanges.length} change(s) await your review.`,
        }),
        href: "/app/changes",
        tone: "warning",
      });
    }
    return actions.slice(0, 5);
  }, [openReminders, openTasks, pendingChanges, profileCompleteness, t]);

  const name = (session?.user?.full_name || "").split(" ")[0];
  const greeting = t(`dashboard.${greetingKey()}`);
  const companyName = session?.company?.name;

  const refresh = async () => {
    await Promise.all([load(), refreshSession().catch(() => null)]);
  };

  return (
    <div className="space-y-6">
      <PageHeader
        eyebrow={companyName}
        title={t("dashboard.greeting", { greeting, name })}
        description={t("dashboard.subtitle")}
        actions={
          <>
            <Button variant="outline" size="sm" onClick={refresh} loading={loading && Boolean(data)}>
              <RefreshCw className="h-3.5 w-3.5" />
              {t("common.refresh")}
            </Button>
            <Button size="sm" onClick={() => navigate("/app/discover")}>
              <Sparkles className="h-3.5 w-3.5" />
              {t("dashboard.discoverCta")}
            </Button>
          </>
        }
      />

      {error ? (
        <Card className="border-red-200 dark:border-red-900">
          <CardContent className="flex flex-col items-start gap-3 sm:flex-row sm:items-center sm:justify-between">
            <div className="flex items-start gap-3">
              <TriangleAlert className="mt-0.5 h-5 w-5 text-red-600" />
              <div>
                <p className="text-sm font-semibold text-navy-900 dark:text-white">
                  {t("common.somethingWentWrong")}
                </p>
                <p className="mt-0.5 text-sm muted">{error}</p>
              </div>
            </div>
            <Button variant="outline" size="sm" onClick={load}>
              {t("common.retry")}
            </Button>
          </CardContent>
        </Card>
      ) : null}

      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        {METRICS.map((metric) => (
          <MetricCard
            key={metric.key}
            metric={metric}
            value={metrics[metric.key] ?? 0}
            loading={loading && !data}
          />
        ))}
      </div>

      <div className="relative overflow-hidden rounded-2xl bg-navy-900 px-5 py-5 text-white shadow-card sm:px-7 sm:py-6">
        <div className="absolute -right-16 -top-20 h-56 w-56 rounded-full bg-teal-500/20 blur-3xl" />
        <div className="relative flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
          <div className="flex items-start gap-3">
            <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-white/10 text-teal-300">
              <Sparkles className="h-5 w-5" />
            </span>
            <div>
              <p className="text-sm font-semibold sm:text-base">{t("dashboard.bannerTitle")}</p>
              <p className="mt-1 max-w-2xl text-xs leading-5 text-navy-200 sm:text-sm">
                {t("dashboard.bannerBody")}
              </p>
            </div>
          </div>
          <Link
            to="/app/discover"
            className="inline-flex shrink-0 items-center justify-center gap-2 rounded-xl bg-white px-4 py-2.5 text-sm font-semibold text-navy-900 transition hover:bg-teal-50"
          >
            {t("dashboard.bannerCta")}
            <ArrowRight className="h-4 w-4" />
          </Link>
        </div>
      </div>

      <div className="grid gap-6 xl:grid-cols-[1.55fr_1fr]">
        <div className="space-y-6">
          <Card>
            <CardHeader
              action={
                <Link
                  to="/app/checklist"
                  className="text-xs font-semibold text-teal-700 hover:text-teal-900 dark:text-teal-300"
                >
                  {t("common.viewAll")}
                </Link>
              }
            >
              <CardTitle>{t("dashboard.priorityActions")}</CardTitle>
              <p className="mt-1 text-sm muted">{t("dashboard.subtitle")}</p>
            </CardHeader>
            <CardContent className="pt-4">
              {loading && !data ? (
                <div className="space-y-2">
                  <Skeleton className="h-16 w-full" />
                  <Skeleton className="h-16 w-full" />
                </div>
              ) : priorityActions.length ? (
                <ul className="space-y-2.5">
                  {priorityActions.map((action) => (
                    <li key={action.key}>
                      <Link
                        to={action.href}
                        className={cn(
                          "group flex items-center gap-3 rounded-xl border p-3.5 transition hover:shadow-sm",
                          ACTION_TONES[action.tone] || ACTION_TONES.info,
                        )}
                      >
                        <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-white/80 text-navy-700 shadow-sm dark:bg-navy-900 dark:text-navy-200">
                          {action.tone === "danger" ? (
                            <TriangleAlert className="h-4 w-4" />
                          ) : action.tone === "warning" ? (
                            <Gauge className="h-4 w-4" />
                          ) : (
                            <CheckCircle2 className="h-4 w-4" />
                          )}
                        </span>
                        <div className="min-w-0 flex-1">
                          <p className="text-sm font-semibold text-navy-900 dark:text-white">
                            {action.title}
                          </p>
                          <p className="mt-0.5 text-xs leading-5 text-navy-600 dark:text-navy-300">
                            {action.description}
                          </p>
                        </div>
                        <ArrowRight className="h-4 w-4 shrink-0 text-navy-300 transition group-hover:translate-x-0.5 group-hover:text-teal-600" />
                      </Link>
                    </li>
                  ))}
                </ul>
              ) : (
                <EmptyState
                  icon={CheckCircle2}
                  title={t("dashboard.noActions")}
                  description={t("dashboard.noTendersHint")}
                  className="py-8"
                />
              )}
            </CardContent>
          </Card>

          <Card>
            <CardHeader
              action={
                <Link
                  to="/app/tenders"
                  className="text-xs font-semibold text-teal-700 hover:text-teal-900 dark:text-teal-300"
                >
                  {t("common.viewAll")}
                </Link>
              }
            >
              <CardTitle>{t("dashboard.recentTenders")}</CardTitle>
            </CardHeader>
            <CardContent className="pt-2">
              {loading && !data ? (
                <div className="space-y-2">
                  <Skeleton className="h-14 w-full" />
                  <Skeleton className="h-14 w-full" />
                </div>
              ) : data?.tenders?.length ? (
                <ul className="divide-y divide-navy-100 dark:divide-navy-800">
                  {data.tenders.slice(0, 5).map((tender) => (
                    <TenderRow key={tender.id} tender={tender} />
                  ))}
                </ul>
              ) : (
                <EmptyState
                  icon={FileText}
                  title={t("dashboard.noTenders")}
                  description={t("dashboard.noTendersHint")}
                  action={
                    <Button variant="outline" size="sm" onClick={() => navigate("/app/discover")}>
                      {t("dashboard.discoverCta")}
                    </Button>
                  }
                  className="py-8"
                />
              )}
            </CardContent>
          </Card>
        </div>

        <div className="space-y-6">
          <Card>
            <CardHeader>
              <CardTitle>{t("dashboard.profileStrength")}</CardTitle>
            </CardHeader>
            <CardContent className="pt-2">
              <div className="flex items-end justify-between">
                <p className="text-3xl font-semibold tracking-tight text-navy-900 dark:text-white">
                  {profileCompleteness}%
                </p>
                <TrendingUp className="h-5 w-5 text-teal-600" />
              </div>
              <Progress
                value={profileCompleteness}
                className="mt-3"
                label={t("dashboard.profileStrength")}
              />
              <p className="mt-2 text-xs leading-5 muted">{t("dashboard.profileHint")}</p>
              <Link
                to="/app/company"
                className="mt-3 inline-flex items-center gap-1 text-xs font-semibold text-teal-700 hover:text-teal-900 dark:text-teal-300"
              >
                {t("dashboard.completeProfile")}
                <ArrowRight className="h-3.5 w-3.5" />
              </Link>
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>{t("dashboard.upcomingDeadlines")}</CardTitle>
              <p className="mt-1 text-xs muted">{t("dashboard.next14Days")}</p>
            </CardHeader>
            <CardContent className="pt-2">
              {openReminders.length ? (
                <ul className="space-y-2">
                  {openReminders.slice(0, 5).map((reminder) => (
                    <li key={reminder.id}>
                      <Link
                        to={reminder.href || "/app/reminders"}
                        className="flex items-center gap-3 rounded-lg px-2 py-2 hover:bg-navy-50 dark:hover:bg-navy-800"
                      >
                        <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-red-50 text-red-600 dark:bg-red-950 dark:text-red-300">
                          <CalendarClock className="h-4 w-4" />
                        </span>
                        <div className="min-w-0 flex-1">
                          <p className="truncate text-sm font-medium text-navy-900 dark:text-white">
                            {reminder.title}
                          </p>
                          <p className="text-xs muted">
                            {formatDate(reminder.due_at, currentLocale())}
                          </p>
                        </div>
                      </Link>
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="rounded-lg bg-navy-50 px-3 py-4 text-center text-xs muted dark:bg-navy-800">
                  {t("dashboard.noDeadlines")}
                </p>
              )}
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>{t("dashboard.recentActivity")}</CardTitle>
            </CardHeader>
            <CardContent className="pt-2">
              {pendingChanges.length ? (
                <ul className="space-y-3">
                  {pendingChanges.slice(0, 5).map((change) => (
                    <li key={change.id} className="flex items-start gap-3">
                      <span className="mt-1 flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-teal-50 text-teal-700 dark:bg-teal-950 dark:text-teal-300">
                        <FolderCheck className="h-3 w-3" />
                      </span>
                      <div className="min-w-0">
                        <p className="text-sm text-navy-800 dark:text-navy-100">
                          {t(`changes.types.${change.change_type}`, {
                            defaultValue: change.change_type,
                          })}{" "}
                          · {change.field_key?.replace(/_/g, " ")}
                        </p>
                        <p className="text-xs muted">
                          {formatDateTime(change.detected_at, currentLocale())}
                        </p>
                      </div>
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="rounded-lg bg-navy-50 px-3 py-4 text-center text-xs muted dark:bg-navy-800">
                  {t("dashboard.noActivity")}
                </p>
              )}
            </CardContent>
          </Card>
        </div>
      </div>
    </div>
  );
}
