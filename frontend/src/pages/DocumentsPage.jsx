import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  CalendarClock,
  BookOpen,
  Download,
  FileText,
  Search,
  Sparkles,
  Trash2,
  TriangleAlert,
  Upload,
} from "lucide-react";

import { api } from "../lib/api";
import { currentLocale } from "../i18n";
import { useToast } from "../contexts/ToastContext";
import { PageHeader } from "../components/ui/PageHeader";
import { Button } from "../components/ui/Button";
import { Card, CardContent } from "../components/ui/Card";
import { EmptyState } from "../components/ui/EmptyState";
import { Input, Select } from "../components/ui/Field";
import { Modal } from "../components/ui/Modal";
import { Skeleton } from "../components/ui/Spinner";
import { StatusBadge } from "../components/ui/Badge";
import { DocumentUploadModal } from "../components/documents/DocumentUploadModal";
import { CompanyDocumentTermsModal } from "../components/documents/CompanyDocumentTermsModal";
import { CompanyUnderstandingModal } from "../components/documents/CompanyUnderstandingModal";
import { CompanyBriefModal } from "../components/documents/CompanyBriefModal";
import { cn, formatBytes, formatDate, isExpired } from "../lib/utils";

const TYPES = ["about_company", "registration", "iso", "turnover", "completion_certificate", "pan", "gst", "other"];

// What the profile reader took from one document, stated per document so the
// library and the understanding panel cannot disagree.
function understandingLabel(t, document) {
  if (document.understanding_state === "done") {
    return t("documents.understanding.readMany", {
      capabilities: document.capability_count ?? 0,
      experiences: document.experience_count ?? 0,
    });
  }
  return t(
    `documents.understanding.docStates.${document.understanding_state}`,
    { defaultValue: document.understanding_state },
  );
}

export function DocumentsPage() {
  const { t } = useTranslation();
  const toast = useToast();
  const [documents, setDocuments] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [search, setSearch] = useState("");
  const [type, setType] = useState("");
  const [uploadOpen, setUploadOpen] = useState(false);
  const [uploadType, setUploadType] = useState("registration");
  const [pendingDelete, setPendingDelete] = useState(null);
  const [termsDocument, setTermsDocument] = useState(null);
  const [understandingOpen, setUnderstandingOpen] = useState(false);
  const [briefOpen, setBriefOpen] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const firstLoad = useRef(true);

  const load = useCallback(async () => {
    setError("");
    setLoading(true);
    try {
      const data = await api.get("/company/documents");
      setDocuments(data.items || []);
    } catch (err) {
      setError(err.message === "NETWORK_ERROR" ? t("errors.network") : err.message || t("errors.generic"));
    } finally {
      setLoading(false);
      firstLoad.current = false;
    }
  }, [t]);

  useEffect(() => {
    const timer = window.setTimeout(load, firstLoad.current ? 0 : 220);
    return () => window.clearTimeout(timer);
  }, [load]);

  const filteredDocuments = useMemo(() => {
    const needle = search.trim().toLowerCase();
    return documents.filter((document) => {
      if (type && document.document_type !== type) return false;
      if (!needle) return true;
      return [document.title, document.issuer, document.signatory, document.notes]
        .filter(Boolean)
        .some((value) => String(value).toLowerCase().includes(needle));
    });
  }, [documents, search, type]);

  const total = filteredDocuments.length;
  const hasFilters = Boolean(search.trim() || type);
  const hasProfile = documents.some(
    (document) => document.document_type === "about_company",
  );

  const openUpload = (documentType = "registration") => {
    setUploadType(documentType);
    setUploadOpen(true);
  };

  const download = async (document) => {
    try {
      await api.download(`/company/documents/${document.id}/download`, document.title);
    } catch (err) {
      toast.error(err.message === "NETWORK_ERROR" ? t("errors.network") : err.message);
    }
  };

  const confirmDelete = async () => {
    if (!pendingDelete) return;
    setDeleting(true);
    try {
      await api.delete(`/company/documents/${pendingDelete.id}`);
      toast.success(t("documents.deleteSuccess"));
      setPendingDelete(null);
      await load();
    } catch (err) {
      toast.error(err.message === "NETWORK_ERROR" ? t("errors.network") : err.message);
    } finally {
      setDeleting(false);
    }
  };

  const empty = useMemo(() => {
    if (loading) return null;
    if (filteredDocuments.length) return null;
    return hasFilters ? "filtered" : "empty";
  }, [loading, filteredDocuments.length, hasFilters]);

  return (
    <div className="space-y-6">
      <PageHeader
        title={t("documents.title")}
        description={t("documents.subtitle")}
        actions={
          <div className="flex flex-wrap gap-2">
            <Button variant="outline" onClick={() => setBriefOpen(true)}>
              <BookOpen className="h-4 w-4" />
              {t("documents.brief.open")}
            </Button>
            {hasProfile ? (
              <Button variant="outline" onClick={() => setUnderstandingOpen(true)}>
                <BookOpen className="h-4 w-4" />
                {t("documents.understanding.open")}
              </Button>
            ) : null}
            <Button onClick={() => openUpload()}>
              <Upload className="h-4 w-4" />
              {t("documents.upload")}
            </Button>
          </div>
        }
      />

      {!loading && !hasProfile ? (
        <div className="flex flex-col gap-3 rounded-xl border border-teal-200 bg-teal-50/60 p-4 dark:border-teal-800 dark:bg-teal-950/40 sm:flex-row sm:items-center sm:justify-between">
          <div className="flex items-start gap-3">
            <Sparkles className="mt-0.5 h-5 w-5 shrink-0 text-teal-700 dark:text-teal-300" />
            <div>
              <p className="text-sm font-semibold text-navy-900 dark:text-white">
                {t("documents.intel.promptTitle")}
              </p>
              <p className="mt-1 text-xs text-navy-600 dark:text-navy-300">
                {t("documents.intel.promptBody")}
              </p>
            </div>
          </div>
          <Button
            variant="outline"
            size="sm"
            className="shrink-0"
            onClick={() => openUpload("about_company")}
          >
            <Upload className="h-3.5 w-3.5" />
            {t("documents.intel.promptAction")}
          </Button>
        </div>
      ) : null}

      <Card>
        <CardContent className="pb-0">
          <div className="flex flex-col gap-2.5 sm:flex-row sm:items-center">
            <div className="relative flex-1">
              <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-navy-400" />
              <Input
                value={search}
                onChange={(event) => setSearch(event.target.value)}
                placeholder={t("documents.searchPlaceholder")}
                aria-label={t("documents.searchPlaceholder")}
                className="pl-9"
              />
            </div>
            <Select
              value={type}
              onChange={(event) => setType(event.target.value)}
              aria-label={t("documents.typeLabel")}
              className="sm:w-56"
            >
              <option value="">{t("documents.allTypes")}</option>
              {TYPES.map((item) => (
                <option key={item} value={item}>
                  {t(`documents.types.${item}`)}
                </option>
              ))}
            </Select>
            <p className="shrink-0 text-xs muted sm:ml-2">{t("documents.count", { count: total })}</p>
          </div>
        </CardContent>

        <CardContent className="pt-4">
          {error ? (
            <div className="flex flex-col items-start gap-3 rounded-xl border border-red-200 bg-red-50 p-4 dark:border-red-900 dark:bg-red-950/40 sm:flex-row sm:items-center sm:justify-between">
              <div className="flex items-start gap-2">
                <TriangleAlert className="mt-0.5 h-4 w-4 text-red-600" />
                <p className="text-sm text-red-700 dark:text-red-200">{error}</p>
              </div>
              <Button variant="outline" size="sm" onClick={load}>
                {t("common.retry")}
              </Button>
            </div>
          ) : null}

          {loading && !documents.length ? (
            <div className="space-y-2">
              <Skeleton className="h-14 w-full" />
              <Skeleton className="h-14 w-full" />
              <Skeleton className="h-14 w-full" />
            </div>
          ) : empty === "empty" ? (
            <EmptyState
              icon={FileText}
              title={t("documents.emptyTitle")}
              description={t("documents.emptyBody")}
              action={
                <Button variant="outline" size="sm" onClick={() => openUpload()}>
                  <Upload className="h-3.5 w-3.5" />
                  {t("documents.upload")}
                </Button>
              }
            />
          ) : empty === "filtered" ? (
            <EmptyState icon={Search} title={t("documents.noResults")} />
          ) : (
            <>
              <div className="hidden overflow-x-auto md:block">
                <table className="w-full min-w-[680px] border-collapse text-left">
                  <thead>
                    <tr className="border-b border-navy-100 text-[11px] uppercase tracking-wide text-navy-400 dark:border-navy-800">
                      <th className="pb-2.5 font-semibold">{t("documents.columns.document")}</th>
                      <th className="pb-2.5 font-semibold">{t("documents.columns.type")}</th>
                      <th className="pb-2.5 font-semibold">{t("documents.columns.issuer")}</th>
                      <th className="pb-2.5 font-semibold">{t("documents.columns.validity")}</th>
                      <th className="pb-2.5 font-semibold">{t("documents.columns.status")}</th>
                      <th className="pb-2.5 text-right font-semibold">{t("common.actions")}</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-navy-100 dark:divide-navy-800">
                    {filteredDocuments.map((document) => {
                      const expired = isExpired(document.expires_on);
                      return (
                        <tr key={document.id} className="group">
                          <td className="py-3 pr-4">
                            <div className="flex items-center gap-3">
                              <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-teal-50 text-teal-700 dark:bg-teal-950 dark:text-teal-300">
                                <FileText className="h-4 w-4" />
                              </span>
                              <div className="min-w-0">
                                <p className="truncate text-sm font-semibold text-navy-900 dark:text-white">
                                  {document.title}
                                </p>
                                <p className="truncate text-xs muted">
                                  {formatBytes(document.bytes)}
                                </p>
                                {document.document_type === "about_company" ? (
                                  <>
                                    <p className="truncate text-[11px] font-medium text-teal-700 dark:text-teal-300">
                                      {document.extraction_state === "done"
                                        ? t("documents.intel.readTerms", {
                                            count: document.term_count,
                                          })
                                        : t(
                                            `documents.intel.states.${document.extraction_state}`,
                                            {
                                              defaultValue:
                                                document.extraction_state,
                                            },
                                          )}
                                    </p>
                                    <p className="truncate text-[11px] font-medium text-navy-500 dark:text-navy-300">
                                      {understandingLabel(t, document)}
                                    </p>
                                  </>
                                ) : null}
                              </div>
                            </div>
                          </td>
                          <td className="py-3 pr-4 text-sm text-navy-700 dark:text-navy-200">
                            {t(`documents.types.${document.document_type}`, { defaultValue: document.document_type })}
                          </td>
                          <td className="py-3 pr-4 text-sm text-navy-700 dark:text-navy-200">
                            {document.issuer || "—"}
                          </td>
                          <td className="py-3 pr-4">
                            {document.expires_on ? (
                              <span
                                className={cn(
                                  "inline-flex items-center gap-1.5 text-xs",
                                  expired ? "font-medium text-red-600 dark:text-red-400" : "text-navy-600 dark:text-navy-300",
                                )}
                              >
                                <CalendarClock className="h-3.5 w-3.5" />
                                {expired
                                  ? t("documents.expired", { date: formatDate(document.expires_on, currentLocale()) })
                                  : t("documents.expires", { date: formatDate(document.expires_on, currentLocale()) })}
                              </span>
                            ) : (
                              <span className="text-xs muted">{t("documents.noExpiry")}</span>
                            )}
                          </td>
                          <td className="py-3 pr-4">
                            <StatusBadge
                              status={expired ? "expired" : document.verification_status}
                              label={
                                expired
                                  ? t("status.expired")
                                  : t(`status.${document.verification_status}`, {
                                      defaultValue: document.verification_status,
                                    })
                              }
                            />
                          </td>
                          <td className="py-3">
                            <div className="flex justify-end gap-1">
                              {document.document_type === "about_company" ? (
                                <Button
                                  variant="ghost"
                                  size="icon"
                                  onClick={() => setTermsDocument(document)}
                                  aria-label={t("documents.intel.open")}
                                  title={t("documents.intel.open")}
                                >
                                  <Sparkles className="h-4 w-4 text-teal-700 dark:text-teal-300" />
                                </Button>
                              ) : null}
                              {document.document_type === "about_company" ? (
                                <Button
                                  variant="ghost"
                                  size="icon"
                                  onClick={() => setUnderstandingOpen(true)}
                                  aria-label={t("documents.understanding.open")}
                                  title={t("documents.understanding.open")}
                                >
                                  <BookOpen className="h-4 w-4 text-teal-700 dark:text-teal-300" />
                                </Button>
                              ) : null}
                              <Button
                                variant="ghost"
                                size="icon"
                                onClick={() => download(document)}
                                aria-label={t("common.download")}
                                title={t("common.download")}
                              >
                                <Download className="h-4 w-4" />
                              </Button>
                              <Button
                                variant="ghost"
                                size="icon"
                                onClick={() => setPendingDelete(document)}
                                aria-label={t("common.delete")}
                                title={t("common.delete")}
                              >
                                <Trash2 className="h-4 w-4 text-red-600" />
                              </Button>
                            </div>
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>

              <ul className="space-y-2 md:hidden">
                {filteredDocuments.map((document) => {
                  const expired = isExpired(document.expires_on);
                  return (
                    <li
                      key={document.id}
                      className="rounded-xl border border-navy-100 p-3 dark:border-navy-800"
                    >
                      <div className="flex items-start gap-3">
                        <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-teal-50 text-teal-700 dark:bg-teal-950 dark:text-teal-300">
                          <FileText className="h-4 w-4" />
                        </span>
                        <div className="min-w-0 flex-1">
                          <p className="truncate text-sm font-semibold text-navy-900 dark:text-white">
                            {document.title}
                          </p>
                          <p className="truncate text-xs muted">
                            {t(`documents.types.${document.document_type}`, {
                              defaultValue: document.document_type,
                            })}{" "}
                            · {formatBytes(document.bytes)}
                          </p>
                          {document.document_type === "about_company" ? (
                            <p className="truncate text-[11px] font-medium text-teal-700 dark:text-teal-300">
                              {document.extraction_state === "done"
                                ? t("documents.intel.readTerms", {
                                    count: document.term_count,
                                  })
                                : t(
                                    `documents.intel.states.${document.extraction_state}`,
                                    { defaultValue: document.extraction_state },
                                  )}
                            </p>
                          ) : null}
                          {document.document_type === "about_company" ? (
                            <p className="truncate text-[11px] font-medium text-navy-500 dark:text-navy-300">
                              {understandingLabel(t, document)}
                            </p>
                          ) : null}
                          <div className="mt-2 flex flex-wrap items-center gap-2">
                            <StatusBadge
                              status={expired ? "expired" : document.verification_status}
                              label={
                                expired
                                  ? t("status.expired")
                                  : t(`status.${document.verification_status}`, {
                                      defaultValue: document.verification_status,
                                    })
                              }
                            />
                            {document.expires_on ? (
                              <span className={cn("text-[11px]", expired ? "text-red-600" : "muted")}>
                                {formatDate(document.expires_on, currentLocale())}
                              </span>
                            ) : null}
                          </div>
                        </div>
                      </div>
                      <div className="mt-2 flex justify-end gap-1 border-t border-navy-100 pt-2 dark:border-navy-800">
                        {document.document_type === "about_company" ? (
                          <Button
                            variant="ghost"
                            size="sm"
                            onClick={() => setTermsDocument(document)}
                          >
                            <Sparkles className="h-3.5 w-3.5 text-teal-700 dark:text-teal-300" />
                            {t("documents.intel.open")}
                          </Button>
                        ) : null}
                        {document.document_type === "about_company" ? (
                          <Button
                            variant="ghost"
                            size="sm"
                            onClick={() => setUnderstandingOpen(true)}
                          >
                            <BookOpen className="h-3.5 w-3.5 text-teal-700 dark:text-teal-300" />
                            {t("documents.understanding.open")}
                          </Button>
                        ) : null}
                        <Button variant="ghost" size="sm" onClick={() => download(document)}>
                          <Download className="h-3.5 w-3.5" />
                          {t("common.download")}
                        </Button>
                        <Button variant="ghost" size="sm" onClick={() => setPendingDelete(document)}>
                          <Trash2 className="h-3.5 w-3.5 text-red-600" />
                          {t("common.delete")}
                        </Button>
                      </div>
                    </li>
                  );
                })}
              </ul>
            </>
          )}
        </CardContent>
      </Card>

      <DocumentUploadModal
        open={uploadOpen}
        initialType={uploadType}
        onClose={() => setUploadOpen(false)}
        onUploaded={(document) => {
          load();
          // A profile is read in the background, so open its terms panel at
          // once: the user sees "queued", not an unexplained wait.
          if (document?.document_type === "about_company") {
            setTermsDocument(document);
          }
        }}
      />

      <CompanyDocumentTermsModal
        document={termsDocument}
        open={Boolean(termsDocument)}
        onClose={() => setTermsDocument(null)}
        onChanged={load}
      />

      <CompanyUnderstandingModal
        open={understandingOpen}
        onClose={() => setUnderstandingOpen(false)}
        onChanged={load}
      />

      <CompanyBriefModal
        open={briefOpen}
        onClose={() => setBriefOpen(false)}
        onChanged={load}
      />

      <Modal
        open={Boolean(pendingDelete)}
        onClose={() => setPendingDelete(null)}
        title={t("documents.deleteTitle")}
        description={t("documents.deleteBody")}
        size="sm"
        footer={
          <>
            <Button variant="outline" onClick={() => setPendingDelete(null)} disabled={deleting}>
              {t("common.cancel")}
            </Button>
            <Button variant="danger" onClick={confirmDelete} loading={deleting}>
              {t("common.delete")}
            </Button>
          </>
        }
      >
        {pendingDelete ? (
          <div className="flex items-center gap-3 rounded-xl bg-navy-50 p-3 dark:bg-navy-800">
            <FileText className="h-5 w-5 text-navy-500" />
            <div>
              <p className="text-sm font-semibold text-navy-900 dark:text-white">{pendingDelete.title}</p>
              <p className="text-xs muted">{formatBytes(pendingDelete.bytes)}</p>
            </div>
          </div>
        ) : null}
      </Modal>
    </div>
  );
}
