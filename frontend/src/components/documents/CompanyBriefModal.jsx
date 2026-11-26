import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { BookOpen, FileText, RefreshCw, Save, Sparkles } from "lucide-react";

import { api } from "../../lib/api";
import { useToast } from "../../contexts/ToastContext";
import { Button } from "../ui/Button";
import { Modal } from "../ui/Modal";
import { Textarea } from "../ui/Field";
import { Badge } from "../ui/Badge";
import { Spinner } from "../ui/Spinner";
import { formatDate } from "../../lib/utils";
import { currentLocale } from "../../i18n";
import {
  BRIEF_ROLES,
  BRIEF_ROLE_GUIDE,
  briefCounts,
  briefIsStored,
  briefPayload,
  briefVersion,
  draftFromPayload,
  payloadFromDraft,
} from "../../lib/brief";

function RoleField({ role, value, onChange }) {
  const { t } = useTranslation();
  return (
    <div className="rounded-xl border border-navy-100 p-3 dark:border-navy-800">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-sm font-semibold text-navy-900 dark:text-white">
          {t(`documents.brief.roles.${role}`)}
        </p>
        <span className="text-[11px] muted">
          {t("documents.brief.termCount", {
            count: value.split(/\r?\n/).filter((line) => line.trim()).length,
          })}
        </span>
      </div>
      <p className="mt-0.5 text-[11px] leading-5 muted">
        {t(BRIEF_ROLE_GUIDE[role])}
      </p>
      <Textarea
        className="mt-2"
        rows={4}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        placeholder={t("documents.brief.onePerLine")}
        aria-label={t(`documents.brief.roles.${role}`)}
      />
    </div>
  );
}

function ProjectList({ projects }) {
  const { t } = useTranslation();
  if (!projects?.length) {
    return <p className="text-xs muted">{t("documents.brief.noProjects")}</p>;
  }
  return (
    <ul className="space-y-2">
      {projects.map((project, index) => (
        <li
          key={`${project.title}-${index}`}
          className="rounded-lg border border-navy-100 p-2.5 dark:border-navy-800"
        >
          <p className="text-xs font-semibold text-navy-900 dark:text-white">
            {project.title}
          </p>
          <p className="mt-0.5 text-[11px] muted">
            {[project.work_family, project.client].filter(Boolean).join(" · ")}
          </p>
          {project.evidence ? (
            <p className="mt-1 border-l-2 border-teal-300 pl-2 text-[11px] italic leading-5 text-navy-700 dark:border-teal-700 dark:text-navy-200">
              &ldquo;{project.evidence}&rdquo;
            </p>
          ) : null}
          {project.page_index ? (
            <p className="mt-0.5 text-[10px] muted">
              {t("documents.brief.page", { page: project.page_index })}
            </p>
          ) : null}
        </li>
      ))}
    </ul>
  );
}

function CertificationList({ certifications }) {
  const { t } = useTranslation();
  if (!certifications?.length) {
    return <p className="text-xs muted">{t("documents.brief.noCertifications")}</p>;
  }
  return (
    <ul className="space-y-2">
      {certifications.map((item, index) => (
        <li
          key={`${item.title}-${index}`}
          className="rounded-lg border border-navy-100 p-2.5 dark:border-navy-800"
        >
          <p className="text-xs font-semibold text-navy-900 dark:text-white">
            {item.title}
          </p>
          <p className="mt-0.5 text-[11px] muted">
            {[item.document_type, item.issuer].filter(Boolean).join(" · ")}
          </p>
          <p className="mt-0.5 text-[10px] font-medium text-amber-700 dark:text-amber-300">
            {t("documents.brief.certificationNote")}
          </p>
        </li>
      ))}
    </ul>
  );
}

export function CompanyBriefModal({ open, onClose, onChanged }) {
  const { t } = useTranslation();
  const toast = useToast();
  const [entry, setEntry] = useState(null);
  const [draft, setDraft] = useState(null);
  const [versions, setVersions] = useState([]);
  const [tab, setTab] = useState("edit");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  const [generating, setGenerating] = useState(false);
  const [activatingId, setActivatingId] = useState(null);

  const loadVersions = useCallback(async () => {
    try {
      const data = await api.get("/company/brief/versions");
      setVersions(data.items || []);
    } catch {
      setVersions([]);
    }
  }, []);

  const applyEntry = useCallback((data) => {
    setEntry(data);
    const payload = briefPayload(data);
    setDraft(payload ? draftFromPayload(payload) : null);
  }, []);

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const [brief, versionData] = await Promise.all([
        api.get("/company/brief"),
        api.get("/company/brief/versions").catch(() => ({ items: [] })),
      ]);
      applyEntry(brief);
      setVersions(versionData.items || []);
    } catch (err) {
      setError(err.message === "NETWORK_ERROR" ? t("errors.network") : err.message);
    } finally {
      setLoading(false);
    }
  }, [applyEntry, t]);

  useEffect(() => {
    if (!open) return;
    setTab("edit");
    load();
  }, [open, load]);

  const payload = briefPayload(entry);
  const counts = briefCounts(payload);
  const stored = briefIsStored(entry);
  const version = briefVersion(entry);

  const save = async () => {
    if (!draft) return;
    setSaving(true);
    try {
      const data = await api.post("/company/brief", {
        payload: payloadFromDraft(draft, payload),
      });
      applyEntry(data);
      await loadVersions();
      toast.success(t("documents.brief.saved"));
      onChanged?.();
    } catch (err) {
      toast.error(err.message === "NETWORK_ERROR" ? t("errors.network") : err.message);
    } finally {
      setSaving(false);
    }
  };

  const generate = async () => {
    setGenerating(true);
    try {
      const data = await api.post("/company/brief/generate", {});
      applyEntry(data);
      await loadVersions();
      toast.success(t("documents.brief.generated"));
      onChanged?.();
    } catch (err) {
      toast.error(
        err.message === "NETWORK_ERROR"
          ? t("errors.network")
          : err.message || t("documents.brief.generateFailed"),
      );
    } finally {
      setGenerating(false);
    }
  };

  const activate = async (id) => {
    setActivatingId(id);
    try {
      const data = await api.post(`/company/brief/versions/${id}/activate`, {});
      applyEntry(data);
      await loadVersions();
      toast.success(t("documents.brief.activated"));
      onChanged?.();
    } catch (err) {
      toast.error(
        err.message === "NETWORK_ERROR"
          ? t("errors.network")
          : err.message || t("documents.brief.activateFailed"),
      );
    } finally {
      setActivatingId(null);
    }
  };

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={t("documents.brief.title")}
      description={t("documents.brief.subtitle")}
      size="lg"
      footer={
        <>
          <Button variant="outline" onClick={onClose} disabled={saving}>
            {t("common.cancel")}
          </Button>
          {tab === "edit" ? (
            <Button onClick={save} loading={saving} disabled={!draft || generating}>
              <Save className="h-4 w-4" />
              {t("documents.brief.save")}
            </Button>
          ) : null}
        </>
      }
    >
      {loading ? (
        <div className="flex items-center gap-2 py-8 text-sm muted">
          <Spinner />
          {t("documents.brief.loading")}
        </div>
      ) : error ? (
        <div className="flex flex-col items-start gap-3 rounded-xl border border-red-200 bg-red-50 p-4 dark:border-red-900 dark:bg-red-950/40 sm:flex-row sm:items-center sm:justify-between">
          <p className="text-sm text-red-700 dark:text-red-200">{error}</p>
          <Button variant="outline" size="sm" onClick={load}>
            {t("common.retry")}
          </Button>
        </div>
      ) : !payload || !draft ? (
        <p className="text-sm muted">{t("documents.brief.empty")}</p>
      ) : (
        <div className="space-y-4">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="flex flex-wrap items-center gap-2">
              <Badge tone={stored ? "teal" : "amber"}>
                {stored
                  ? t("documents.brief.stored", { version })
                  : t("documents.brief.preview")}
              </Badge>
              <span className="text-xs muted">
                {t(
                  `documents.brief.sources.${entry?.brief?.source_kind || "derived"}`,
                  { defaultValue: entry?.brief?.source_kind || "derived" },
                )}
              </span>
            </div>
            <Button
              variant="outline"
              size="sm"
              onClick={generate}
              loading={generating}
              disabled={saving}
            >
              <Sparkles className="h-3.5 w-3.5" />
              {t("documents.brief.generate")}
            </Button>
          </div>

          {!stored ? (
            <p className="rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-900 dark:border-amber-900 dark:bg-amber-950 dark:text-amber-100">
              {t("documents.brief.previewNote")}
            </p>
          ) : (
            <p className="text-[11px] leading-5 muted">
              {t("documents.brief.generateNote")}
            </p>
          )}

          <div className="flex gap-1 rounded-xl border border-navy-100 bg-white p-1 dark:border-navy-800 dark:bg-navy-900">
            {["edit", "versions"].map((item) => (
              <button
                key={item}
                type="button"
                onClick={() => setTab(item)}
                className={
                  tab === item
                    ? "flex-1 rounded-lg bg-teal-50 px-3 py-1.5 text-xs font-semibold text-teal-800 dark:bg-teal-950 dark:text-teal-200"
                    : "flex-1 rounded-lg px-3 py-1.5 text-xs font-medium text-navy-500 hover:bg-navy-50 dark:hover:bg-navy-800"
                }
              >
                {t(`documents.brief.tabs.${item}`)}
              </button>
            ))}
          </div>

          {tab === "edit" ? (
            <div className="space-y-3">
              <div>
                <p className="mb-1.5 text-sm font-medium text-navy-800 dark:text-navy-100">
                  {t("documents.brief.summary")}
                </p>
                <Textarea
                  rows={3}
                  value={draft.summary}
                  onChange={(event) =>
                    setDraft((current) => ({ ...current, summary: event.target.value }))
                  }
                  placeholder={t("documents.brief.summaryPlaceholder")}
                />
              </div>

              <div className="grid gap-3 sm:grid-cols-2">
                {BRIEF_ROLES.map((role) => (
                  <RoleField
                    key={role}
                    role={role}
                    value={draft[role]}
                    onChange={(value) =>
                      setDraft((current) => ({ ...current, [role]: value }))
                    }
                  />
                ))}
              </div>

              <div className="grid gap-3 sm:grid-cols-2">
                <div className="rounded-xl border border-navy-100 p-3 dark:border-navy-800">
                  <p className="flex items-center gap-1.5 text-sm font-semibold text-navy-900 dark:text-white">
                    <BookOpen className="h-4 w-4 text-teal-700 dark:text-teal-300" />
                    {t("documents.brief.projects")}
                  </p>
                  <div className="mt-2">
                    <ProjectList projects={payload.projects} />
                  </div>
                </div>
                <div className="rounded-xl border border-navy-100 p-3 dark:border-navy-800">
                  <p className="flex items-center gap-1.5 text-sm font-semibold text-navy-900 dark:text-white">
                    <FileText className="h-4 w-4 text-navy-500" />
                    {t("documents.brief.certifications")}
                  </p>
                  <div className="mt-2">
                    <CertificationList certifications={payload.certifications} />
                  </div>
                </div>
              </div>

              <p className="text-[11px] muted">
                {t("documents.brief.counts", {
                  core: counts.core,
                  adjacent: counts.adjacent,
                  support: counts.support,
                  generic: counts.generic,
                })}
              </p>
            </div>
          ) : (
            <div className="space-y-2">
              {!versions.length ? (
                <p className="text-sm muted">{t("documents.brief.noVersions")}</p>
              ) : (
                versions.map((item) => (
                  <div
                    key={item.id}
                    className="flex flex-col gap-2 rounded-xl border border-navy-100 p-3 dark:border-navy-800 sm:flex-row sm:items-center sm:justify-between"
                  >
                    <div className="min-w-0">
                      <div className="flex flex-wrap items-center gap-2">
                        <p className="text-sm font-semibold text-navy-900 dark:text-white">
                          {t("documents.brief.stored", { version: item.version })}
                        </p>
                        {item.is_current ? (
                          <Badge tone="green">{t("documents.brief.current")}</Badge>
                        ) : null}
                        <span className="text-[11px] muted">
                          {t(`documents.brief.sources.${item.source_kind}`, {
                            defaultValue: item.source_kind,
                          })}
                        </span>
                      </div>
                      {item.summary ? (
                        <p className="mt-1 line-clamp-2 text-xs muted">{item.summary}</p>
                      ) : null}
                      <p className="mt-1 text-[11px] muted">
                        {t("documents.brief.versionMeta", {
                          core: item.counts?.core ?? 0,
                          adjacent: item.counts?.adjacent ?? 0,
                          support: item.counts?.support ?? 0,
                          generic: item.counts?.generic ?? 0,
                        })}
                        {item.created_at
                          ? ` · ${formatDate(item.created_at, currentLocale())}`
                          : ""}
                      </p>
                    </div>
                    {!item.is_current ? (
                      <Button
                        variant="outline"
                        size="sm"
                        className="shrink-0"
                        onClick={() => activate(item.id)}
                        loading={activatingId === item.id}
                        disabled={activatingId !== null && activatingId !== item.id}
                      >
                        <RefreshCw className="h-3.5 w-3.5" />
                        {t("documents.brief.activate")}
                      </Button>
                    ) : null}
                  </div>
                ))
              )}
            </div>
          )}
        </div>
      )}
    </Modal>
  );
}
