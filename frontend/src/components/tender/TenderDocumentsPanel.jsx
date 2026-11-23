import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  Download,
  Eye,
  FileText,
  Link2,
  Plus,
  RefreshCw,
  ScrollText,
  Sparkles,
  UploadCloud,
  X,
} from "lucide-react";

import { api } from "../../lib/api";
import { formatBytes, formatDateTime } from "../../lib/utils";
import { capturePath, useCaptureUrl } from "../../lib/useCaptureUrl";
import { currentLocale } from "../../i18n";
import { useToast } from "../../contexts/ToastContext";
import { Badge, StatusBadge } from "../ui/Badge";
import { Button } from "../ui/Button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "../ui/Card";
import { EmptyState } from "../ui/EmptyState";
import { FieldError, Input, Label } from "../ui/Field";
import { Modal } from "../ui/Modal";
import { Skeleton, Spinner } from "../ui/Spinner";
import { PdfViewerModal } from "./PdfViewerModal";

const MAX_BYTES = 25 * 1024 * 1024;

function documentState(document) {
  if (document.extraction_state) return document.extraction_state;
  return document.has_text_layer ? "extracted" : "no_text_layer";
}

export function TenderDocumentsPanel({ tenderId, onExtracted }) {
  const { t } = useTranslation();
  const toast = useToast();

  const [documents, setDocuments] = useState([]);
  const [covers, setCovers] = useState([]);
  const [fetchState, setFetchState] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const [addOpen, setAddOpen] = useState(false);
  const [file, setFile] = useState(null);
  const [sourceUrl, setSourceUrl] = useState("");
  const [formError, setFormError] = useState("");
  const [uploading, setUploading] = useState(false);

  const [fetching, setFetching] = useState(false);
  const [fetchJob, setFetchJob] = useState(null);
  const [fetchJobId, setFetchJobId] = useState(null);
  const [answer, setAnswer] = useState("");
  const [answering, setAnswering] = useState(false);
  const [answerError, setAnswerError] = useState("");
  const [extractingId, setExtractingId] = useState(null);

  const [pdfDoc, setPdfDoc] = useState(null);
  const [pagesDoc, setPagesDoc] = useState(null);
  const [pages, setPages] = useState([]);
  const [pagesLoading, setPagesLoading] = useState(false);
  const [selectedPage, setSelectedPage] = useState(null);
  const [pageLoading, setPageLoading] = useState(false);

  const load = async () => {
    setLoading(true);
    setError("");
    try {
      const data = await api.get(`/tenders/${tenderId}/documents`);
      setDocuments(data.items || []);
      setCovers(data.covers || []);
      setFetchState(data.fetch_state || null);
    } catch (err) {
      setError(err.message === "NETWORK_ERROR" ? t("errors.network") : err.message || t("errors.generic"));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tenderId]);

  const challenge =
    fetchJob?.state === "needs_you" && fetchJob.challenge?.state === "open"
      ? fetchJob.challenge
      : null;
  const challengePath = capturePath(challenge);
  const screenshotPath = capturePath(challenge, "screenshot_url");
  const capture = useCaptureUrl(challengePath);
  const screenshot = useCaptureUrl(screenshotPath);
  const fetchActive = Boolean(
    fetchJob && ["queued", "running", "needs_you"].includes(fetchJob.state),
  );

  // A page reload must still find the fetch that is waiting for the human.
  useEffect(() => {
    let active = true;
    setFetchJob(null);
    setFetchJobId(null);
    api
      .get("/jobs?kind=fetch_documents&state=queued&state=running&state=needs_you&limit=100")
      .then((data) => {
        if (!active) return;
        const job = (data.items || []).find(
          (item) => String(item.payload?.tender_id || "") === String(tenderId),
        );
        if (job) {
          setFetchJob(job);
          setFetchJobId(job.id);
        }
      })
      .catch(() => {});
    return () => {
      active = false;
    };
  }, [tenderId]);

  // Poll the fetch job so the challenge and the finished documents appear
  // without a manual reload.
  useEffect(() => {
    if (!fetchJobId) return undefined;
    let active = true;
    let timer = null;
    const tick = async () => {
      try {
        const job = await api.get(`/jobs/${fetchJobId}`);
        if (!active) return;
        setFetchJob(job);
        if (["queued", "running", "needs_you"].includes(job.state)) {
          timer = setTimeout(tick, job.state === "needs_you" ? 5000 : 3000);
        } else {
          await load();
          if (!active) return;
          setFetchJobId(null);
        }
      } catch {
        if (active) setFetchJobId(null);
      }
    };
    tick();
    return () => {
      active = false;
      if (timer) clearTimeout(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fetchJobId]);

  useEffect(() => {
    if (!addOpen) {
      setFile(null);
      setSourceUrl("");
      setFormError("");
      setUploading(false);
    }
  }, [addOpen]);

  const chooseFile = (candidate) => {
    if (!candidate) return;
    const name = candidate.name.toLowerCase();
    if (!name.endsWith(".pdf") && !name.endsWith(".zip")) {
      setFormError(t("tenders.documentsPanel.pdfOnly"));
      return;
    }
    if (candidate.size > MAX_BYTES) {
      setFormError(t("tenders.documentsPanel.tooLarge"));
      return;
    }
    setFormError("");
    setFile(candidate);
  };

  const upload = async (event) => {
    event.preventDefault();
    if (!file) {
      setFormError(t("tenders.documentsPanel.chooseFile"));
      return;
    }
    setFormError("");
    setUploading(true);
    try {
      const data = new FormData();
      data.append("file", file);
      if (sourceUrl.trim()) data.append("source_url", sourceUrl.trim());
      if (file.name.toLowerCase().endsWith(".zip")) {
        const result = await api.postForm(
          `/tenders/${tenderId}/documents/archive`,
          data,
        );
        const attached = result.documents?.length || 0;
        toast.success(
          t("tenders.documentsPanel.archiveUploadedToast", {
            count: attached,
            defaultValue: `Attached ${attached} document(s) from the zip.`,
          }),
        );
        if (result.skipped?.length) {
          toast.info(
            t("tenders.documentsPanel.archiveSkippedToast", {
              count: result.skipped.length,
              defaultValue: `${result.skipped.length} file(s) in the zip were skipped.`,
            }),
          );
        }
      } else {
        const document = await api.postForm(`/tenders/${tenderId}/documents`, data);
        toast.success(
          document.has_text_layer || document.ocr_page_count > 0
            ? t("tenders.documentsPanel.uploadedToast")
            : t("tenders.documentsPanel.uploadedNoTextToast"),
        );
      }
      setAddOpen(false);
      await load();
    } catch (err) {
      setFormError(err.message === "NETWORK_ERROR" ? t("errors.network") : err.message || t("errors.generic"));
    } finally {
      setUploading(false);
    }
  };

  const fetchFromSource = async () => {
    setFetching(true);
    try {
      const payload = await api.post(`/tenders/${tenderId}/documents/fetch`, {});
      toast.success(
        payload.created
          ? t("tenders.documentsPanel.fetchQueued", {
              defaultValue: "Document fetch queued. The job will report what it found.",
            })
          : t("tenders.documentsPanel.fetchAlreadyRunning", {
              defaultValue: "A document fetch is already running for this tender.",
            }),
      );
      if (payload.job?.id) {
        setFetchJob(payload.job);
        setFetchJobId(payload.job.id);
      }
      await load();
    } catch (err) {
      toast.error(err.message === "NETWORK_ERROR" ? t("errors.network") : err.message || t("errors.generic"));
    } finally {
      setFetching(false);
    }
  };

  const cancelFetch = async () => {
    if (!fetchJobId) return;
    try {
      const job = await api.post(`/jobs/${fetchJobId}/cancel`, {});
      setFetchJob(job);
      setFetchJobId(null);
      setAnswer("");
      setAnswerError("");
      toast.info(
        t("tenders.documentsPanel.fetchCancelled", {
          defaultValue: "Document fetch cancelled. The browser session was closed.",
        }),
      );
      await load();
    } catch (err) {
      toast.error(
        err.message === "NETWORK_ERROR"
          ? t("errors.network")
          : err.message || t("errors.generic"),
      );
    }
  };

  const answerChallenge = async (event) => {
    event.preventDefault();
    if (!fetchJobId || !answer.trim()) return;
    setAnswering(true);
    setAnswerError("");
    try {
      const result = await api.post(`/jobs/${fetchJobId}/challenge/answer`, {
        answer: answer.trim(),
      });
      setAnswer("");
      setFetchJob(result.job);
      if (result.accepted) {
        toast.success(
          t("tenders.documentsPanel.answerAccepted", {
            defaultValue: "Answer sent — the fetch is continuing in the same browser.",
          }),
        );
      } else {
        setAnswerError(
          t("tenders.documentsPanel.answerRejected", {
            defaultValue:
              "That answer was not accepted. The portal drew a new picture; try the new one.",
          }),
        );
      }
    } catch (err) {
      setAnswerError(
        err.message === "NETWORK_ERROR" ? t("errors.network") : err.message || t("errors.generic"),
      );
    } finally {
      setAnswering(false);
    }
  };

  const extract = async (document) => {
    setExtractingId(document.id);
    try {
      const result = await api.post(`/tender-documents/${document.id}/extract`, {});
      toast.info(
        t("tenders.documentsPanel.extractQueued", {
          state: result.state,
          defaultValue: `Requirement extraction queued (${result.state}).`,
        }),
      );
      onExtracted?.();
    } catch (err) {
      toast.error(err.message === "NETWORK_ERROR" ? t("errors.network") : err.message || t("errors.generic"));
    } finally {
      setExtractingId(null);
    }
  };

  const openPdf = (document) => setPdfDoc(document);

  const openPages = (document) => {
    setPagesDoc(document);
    setPages([]);
    setSelectedPage(null);
  };

  useEffect(() => {
    if (!pagesDoc) return undefined;
    let active = true;
    setPagesLoading(true);
    api
      .get(`/tender-documents/${pagesDoc.id}/pages`)
      .then((data) => {
        if (!active) return;
        setPages(data.items || []);
        if (data.items?.length) {
          const first = data.items.find((page) => page.char_count > 0) || data.items[0];
          openPage(first.page_index, active);
        }
      })
      .catch((err) => {
        if (active) toast.error(err.message || t("errors.generic"));
      })
      .finally(() => {
        if (active) setPagesLoading(false);
      });
    return () => {
      active = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pagesDoc]);

  const openPage = async (pageIndex, stillActive) => {
    if (!pagesDoc) return;
    setPageLoading(true);
    try {
      const page = await api.get(`/tender-documents/${pagesDoc.id}/pages/${pageIndex}`);
      if (stillActive === false) return;
      setSelectedPage(page);
    } catch (err) {
      toast.error(err.message || t("errors.generic"));
    } finally {
      setPageLoading(false);
    }
  };

  const statusLabel = (status) =>
    t(`tenders.documentsPanel.statuses.${status}`, { defaultValue: status });

  const coverMissing = fetchState?.missing_covers || [];

  return (
    <Card>
      <CardHeader
        action={
          <div className="flex flex-wrap gap-2">
            {fetchActive ? (
              <Button size="sm" variant="outline" onClick={cancelFetch}>
                <X className="h-4 w-4" />
                {t("tenders.documentsPanel.cancelFetch", {
                  defaultValue: "Cancel fetch",
                })}
              </Button>
            ) : null}
            <Button
              size="sm"
              variant="outline"
              onClick={fetchFromSource}
              loading={fetching}
              disabled={fetchActive}
            >
              <RefreshCw className="h-4 w-4" />
              {t("tenders.documentsPanel.fetchFromSource", { defaultValue: "Fetch from source" })}
            </Button>
            <Button size="sm" onClick={() => setAddOpen(true)}>
              <Plus className="h-4 w-4" />
              {t("tenders.documentsPanel.add")}
            </Button>
          </div>
        }
      >
        <CardTitle>{t("tenders.documentsPanel.title")}</CardTitle>
        <CardDescription>{t("tenders.documentsPanel.subtitle")}</CardDescription>
      </CardHeader>

      <CardContent className="space-y-4 pt-2">
        {error ? (
          <div className="flex items-center justify-between gap-3 rounded-xl border border-red-200 bg-red-50 p-3 text-sm text-red-700 dark:border-red-900 dark:bg-red-950/40 dark:text-red-200">
            <span>{error}</span>
            <Button size="sm" variant="outline" onClick={load}>
              {t("common.retry")}
            </Button>
          </div>
        ) : null}

        {challenge ? (
          <div className="space-y-3 rounded-xl border border-amber-200 bg-amber-50 px-3.5 py-3 text-xs leading-5 text-amber-900 dark:border-amber-900 dark:bg-amber-950/40 dark:text-amber-100">
            <p className="font-semibold">
              {t("tenders.documentsPanel.challengeTitle", {
                defaultValue: "Action needed — the portal is waiting for you",
              })}
            </p>
            <p>
              {challenge.context?.instructions ||
                t("tenders.documentsPanel.challengeInstructions", {
                  defaultValue:
                    "Type the characters in the picture into the box below and press Submit. BidKaro carries your answer into the portal's form once and never stores it.",
                })}
            </p>
            {challenge.context?.document ? (
              <p className="text-[11px] opacity-80">
                {t("tenders.documentsPanel.challengeDocument", {
                  document: challenge.context.document,
                  defaultValue: `File: ${challenge.context.document}`,
                })}
              </p>
            ) : null}
            <div className="flex flex-wrap items-start gap-3">
              {capture ? (
                <figure className="m-0">
                  <img
                    src={capture}
                    alt={t("tenders.documentsPanel.challengeAlt", {
                      defaultValue: "The portal's captcha",
                    })}
                    className="max-h-40 w-auto rounded border border-amber-200 bg-white dark:border-amber-900"
                  />
                  <figcaption className="mt-1 text-[10px] opacity-75">
                    {t("tenders.documentsPanel.challengeCaption", {
                      defaultValue: "The captcha itself",
                    })}
                  </figcaption>
                </figure>
              ) : null}
              {screenshot ? (
                <figure className="m-0 min-w-0">
                  <img
                    src={screenshot}
                    alt={t("tenders.documentsPanel.screenshotAlt", {
                      defaultValue: "The portal page the captcha was on",
                    })}
                    className="max-h-40 w-auto rounded border border-amber-200 dark:border-amber-900"
                  />
                  <figcaption className="mt-1 text-[10px] opacity-75">
                    {t("tenders.documentsPanel.screenshotCaption", {
                      defaultValue: "The full portal page",
                    })}
                  </figcaption>
                </figure>
              ) : null}
            </div>
            <form onSubmit={answerChallenge} className="flex flex-wrap items-end gap-2">
              <label className="flex flex-col gap-1 text-[11px] font-semibold">
                {t("tenders.documentsPanel.challengeInputLabel", {
                  defaultValue: "Characters in the picture",
                })}
                <Input
                  value={answer}
                  onChange={(event) => setAnswer(event.target.value)}
                  autoComplete="off"
                  spellCheck={false}
                  maxLength={16}
                  className="w-44 bg-white text-navy-900 dark:bg-navy-900 dark:text-white"
                  disabled={answering}
                />
              </label>
              <Button type="submit" size="sm" disabled={answering || !answer.trim()}>
                {answering ? <Spinner className="h-3.5 w-3.5" /> : null}
                {t("tenders.documentsPanel.submitAnswer", {
                  defaultValue: "Submit answer",
                })}
              </Button>
            </form>
            {answerError ? (
              <p className="text-[11px] font-medium text-red-700 dark:text-red-300">
                {answerError}
              </p>
            ) : null}
            <p className="text-[11px] opacity-80">
              {t("tenders.documentsPanel.answerHint", {
                defaultValue:
                  "Your answer is carried into the portal's form once and is never saved. The BidKaro browser window stays minimized; everything you need is pictured here.",
              })}
            </p>
            <p className="text-[11px] opacity-80">
              {t("tenders.documentsPanel.challengeExpires", {
                time: formatDateTime(challenge.expires_at, currentLocale()),
                defaultValue: `This request expires at ${formatDateTime(
                  challenge.expires_at,
                  currentLocale(),
                )}.`,
              })}
            </p>
          </div>
        ) : null}

        {fetchJob && ["queued", "running"].includes(fetchJob.state) ? (
          <p className="flex items-center gap-2 text-xs text-teal-700 dark:text-teal-300">
            <Spinner className="h-3.5 w-3.5" />
            {t("tenders.documentsPanel.fetchRunning", {
              defaultValue: "Reading the portal in the visible browser window…",
            })}
          </p>
        ) : null}

        {fetchJob && ["done", "partial", "failed", "cancelled"].includes(fetchJob.state) ? (
          <p className="text-xs muted">
            {t("tenders.documentsPanel.fetchFinished", {
              state: fetchJob.state,
              defaultValue: `Last fetch: ${fetchJob.state}.`,
            })}
            {fetchJob.last_error ? ` — ${fetchJob.last_error}` : ""}
          </p>
        ) : null}

        {fetchState ? (
          <div className="rounded-xl border border-navy-100 px-3.5 py-3 text-xs dark:border-navy-800">
            <span className="font-semibold text-navy-700 dark:text-navy-200">
              {t("tenders.documentsPanel.fetchState", { defaultValue: "Fetch state" })}:
            </span>{" "}
            <span className="muted">
              {t("tenders.documentsPanel.fetchCounts", {
                present: fetchState.documents_present,
                expected: fetchState.expected_covers ?? "—",
                attempts: fetchState.attempts,
                defaultValue: `${fetchState.documents_present} document(s), ${fetchState.expected_covers ?? "—"} expected, ${fetchState.attempts} attempt(s)`,
              })}
            </span>
            {fetchState.last_error ? (
              <span className="text-red-600 dark:text-red-400"> — {fetchState.last_error}</span>
            ) : null}
          </div>
        ) : null}

        {covers.length ? (
          <div className="rounded-xl border border-navy-100 px-3.5 py-3 dark:border-navy-800">
            <p className="text-xs font-semibold text-navy-700 dark:text-navy-200">
              {t("tenders.documentsPanel.covers", { defaultValue: "Expected documents from the cover" })}
            </p>
            <ul className="mt-2 flex flex-wrap gap-2">
              {covers.map((cover) => (
                <li
                  key={cover.id || `${cover.cover_no}-${cover.doc_type}`}
                  className="rounded-full border border-navy-200 px-2.5 py-1 text-[11px] text-navy-700 dark:border-navy-700 dark:text-navy-200"
                >
                  {cover.description || cover.doc_type || `#${cover.cover_no}`}
                </li>
              ))}
            </ul>
            {coverMissing.length ? (
              <p className="mt-2 text-[11px] text-amber-700 dark:text-amber-300">
                {t("tenders.documentsPanel.coversMissing", {
                  count: coverMissing.length,
                  defaultValue: `${coverMissing.length} expected document(s) have no matching file yet.`,
                })}
              </p>
            ) : null}
          </div>
        ) : null}

        {loading ? (
          <div className="space-y-2">
            <Skeleton className="h-24 w-full" />
            <Skeleton className="h-24 w-full" />
          </div>
        ) : documents.length ? (
          <div className="space-y-3">
            {documents.map((document) => {
              const state = documentState(document);
              return (
                <div
                  key={document.id}
                  className="rounded-xl border border-navy-100 p-4 dark:border-navy-800"
                >
                  <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
                    <div className="flex min-w-0 items-start gap-3">
                      <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg bg-navy-100 text-navy-600 dark:bg-navy-800 dark:text-navy-300">
                        <FileText className="h-5 w-5" />
                      </span>
                      <div className="min-w-0">
                        <div className="flex flex-wrap items-center gap-2">
                          <p className="truncate text-sm font-semibold text-navy-900 dark:text-white">
                            {document.filename}
                          </p>
                          <StatusBadge status={state} label={statusLabel(state)} />
                          {document.document_key ? (
                            <Badge tone="neutral">{document.document_key}</Badge>
                          ) : null}
                          {document.is_current === false ? (
                            <Badge tone="amber">{t("tenders.documentsPanel.oldVersion", { defaultValue: "Old version" })}</Badge>
                          ) : null}
                          {document.ocr_page_count > 0 ? (
                            <Badge tone="amber">
                              {t("tenders.documentsPanel.ocrPages", {
                                count: document.ocr_page_count,
                              })}
                            </Badge>
                          ) : null}
                        </div>
                        <p className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs muted">
                          <span>
                            {t("tenders.documentsPanel.pages", { count: document.page_count ?? 0 })}
                          </span>
                          <span>{formatBytes(document.bytes)}</span>
                          <span>
                            {document.source_url
                              ? t("tenders.documentsPanel.sourceDownloaded")
                              : t("tenders.documentsPanel.sourceUploaded")}
                          </span>
                          <span>
                            {formatDateTime(
                              document.fetched_at || document.created_at,
                              currentLocale(),
                            )}
                          </span>
                        </p>
                        {!document.has_text_layer && !document.ocr_page_count ? (
                          <p className="mt-2 rounded-lg bg-amber-50 px-2.5 py-1.5 text-xs text-amber-800 dark:bg-amber-950/40 dark:text-amber-200">
                            {t("tenders.documentsPanel.noTextHelp")}
                          </p>
                        ) : null}
                        {state === "failed" && document.extraction_error ? (
                          <p className="mt-2 rounded-lg bg-red-50 px-2.5 py-1.5 text-xs text-red-700 dark:bg-red-950/40 dark:text-red-200">
                            {document.extraction_error}
                          </p>
                        ) : null}
                      </div>
                    </div>

                    <div className="flex shrink-0 flex-wrap gap-2">
                      <Button size="sm" variant="outline" onClick={() => openPdf(document)}>
                        <Eye className="h-3.5 w-3.5" />
                        {t("tenders.documentsPanel.viewPdf", { defaultValue: "View PDF" })}
                      </Button>
                      <Button size="sm" variant="ghost" onClick={() => openPages(document)}>
                        <ScrollText className="h-3.5 w-3.5" />
                        {t("tenders.documentsPanel.viewText", { defaultValue: "Text" })}
                      </Button>
                      <Button
                        size="sm"
                        variant="outline"
                        onClick={() =>
                          api.download(
                            `/tender-documents/${document.id}/download`,
                            document.filename,
                          )
                        }
                      >
                        <Download className="h-3.5 w-3.5" />
                        {t("tenders.documentsPanel.download")}
                      </Button>
                      <Button
                        size="sm"
                        onClick={() => extract(document)}
                        loading={extractingId === document.id}
                        disabled={
                          (!document.has_text_layer && !document.ocr_page_count) ||
                          state === "failed"
                        }
                      >
                        <Sparkles className="h-3.5 w-3.5" />
                        {extractingId === document.id
                          ? t("tenders.documentsPanel.extracting")
                          : t("tenders.documentsPanel.extract")}
                      </Button>
                    </div>
                  </div>
                </div>
              );
            })}
          </div>
        ) : (
          <EmptyState
            icon={UploadCloud}
            title={t("tenders.documentsPanel.emptyTitle")}
            description={t("tenders.documentsPanel.emptyBody")}
            action={
              <Button size="sm" onClick={() => setAddOpen(true)}>
                <Plus className="h-4 w-4" />
                {t("tenders.documentsPanel.add")}
              </Button>
            }
          />
        )}
      </CardContent>

      <Modal
        open={addOpen}
        onClose={() => setAddOpen(false)}
        title={t("tenders.documentsPanel.addTitle")}
        description={t("tenders.documentsPanel.addSubtitle")}
        footer={
          <>
            <Button variant="outline" onClick={() => setAddOpen(false)} disabled={uploading}>
              {t("common.cancel")}
            </Button>
            <Button type="submit" form="tender-document-form" loading={uploading}>
              {uploading
                ? t("tenders.documentsPanel.uploading")
                : t("tenders.documentsPanel.upload")}
            </Button>
          </>
        }
      >
        <form id="tender-document-form" onSubmit={upload} className="space-y-4">
          {file ? (
            <div className="flex items-center gap-3 rounded-xl border border-teal-200 bg-teal-50/60 p-3 dark:border-teal-800 dark:bg-teal-950/40">
              <span className="flex h-10 w-10 items-center justify-center rounded-lg bg-white text-teal-700 dark:bg-navy-900 dark:text-teal-300">
                <FileText className="h-5 w-5" />
              </span>
              <div className="min-w-0 flex-1">
                <p className="truncate text-sm font-semibold text-navy-900 dark:text-white">
                  {file.name}
                </p>
                <p className="text-xs muted">{formatBytes(file.size)}</p>
              </div>
              <Button size="sm" variant="ghost" onClick={() => setFile(null)}>
                {t("common.remove")}
              </Button>
            </div>
          ) : (
            <label className="flex cursor-pointer flex-col items-center justify-center rounded-xl border-2 border-dashed border-navy-200 bg-navy-50/50 px-6 py-8 text-center transition hover:border-teal-400 dark:border-navy-700 dark:bg-navy-800/40">
              <span className="mb-2 flex h-11 w-11 items-center justify-center rounded-xl bg-white text-teal-700 shadow-sm dark:bg-navy-900 dark:text-teal-300">
                <UploadCloud className="h-5 w-5" />
              </span>
              <span className="text-sm font-medium text-navy-800 dark:text-navy-100">
                {t("tenders.documentsPanel.drop")}
              </span>
              <span className="mt-1 text-xs muted">
                {t("tenders.documentsPanel.supported")}
              </span>
              <input
                type="file"
                accept="application/pdf,.pdf,application/zip,.zip"
                className="hidden"
                onChange={(event) => chooseFile(event.target.files?.[0])}
              />
            </label>
          )}

          <FieldError>{formError}</FieldError>

          <div>
            <Label htmlFor="tender-document-source">
              {t("tenders.documentsPanel.sourceUrl", { defaultValue: "Source URL (optional)" })}
            </Label>
            <div className="relative">
              <Link2 className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-navy-400" />
              <Input
                id="tender-document-source"
                value={sourceUrl}
                onChange={(event) => setSourceUrl(event.target.value)}
                className="pl-9"
                placeholder="https://..."
              />
            </div>
          </div>
        </form>
      </Modal>

      <Modal
        open={Boolean(pagesDoc)}
        onClose={() => setPagesDoc(null)}
        title={t("tenders.documentsPanel.pagesTitle")}
        description={t("tenders.documentsPanel.pagesSubtitle")}
        size="lg"
        footer={
          <Button variant="outline" onClick={() => setPagesDoc(null)}>
            {t("common.close")}
          </Button>
        }
      >
        {pagesLoading ? (
          <Skeleton className="h-48 w-full" />
        ) : (
          <div className="grid gap-4 sm:grid-cols-[9rem_1fr]">
            <div className="flex max-h-64 flex-wrap gap-1.5 overflow-y-auto sm:max-h-[26rem] sm:flex-col">
              {pages.map((page) => (
                <button
                  key={page.page_index}
                  type="button"
                  onClick={() => openPage(page.page_index)}
                  className={
                    selectedPage?.page_index === page.page_index
                      ? "rounded-lg bg-teal-50 px-2.5 py-1.5 text-left text-xs font-semibold text-teal-800 dark:bg-teal-950 dark:text-teal-200"
                      : "rounded-lg px-2.5 py-1.5 text-left text-xs font-medium text-navy-600 hover:bg-navy-50 dark:text-navy-300 dark:hover:bg-navy-800"
                  }
                >
                  {t("tenders.documentsPanel.page", { number: page.page_index })}
                  {page.printed_label && page.printed_label !== String(page.page_index) ? (
                    <span className="ml-1 text-[10px] opacity-70">({page.printed_label})</span>
                  ) : null}
                  {page.is_ocr ? (
                    <span className="ml-1 text-[10px] font-bold text-amber-600">OCR</span>
                  ) : page.char_count === 0 ? (
                    <span className="ml-1 text-[10px] text-amber-600">!</span>
                  ) : null}
                </button>
              ))}
            </div>
            <div className="min-w-0">
              {pageLoading ? (
                <Skeleton className="h-40 w-full" />
              ) : selectedPage ? (
                <div>
                  <div className="mb-2 flex flex-wrap items-center gap-2 text-xs muted">
                    <span>
                      {t("tenders.documentsPanel.page", { number: selectedPage.page_index })}
                    </span>
                    {selectedPage.printed_label &&
                    selectedPage.printed_label !== String(selectedPage.page_index) ? (
                      <span>
                        {t("tenders.documentsPanel.printedPage", {
                          label: selectedPage.printed_label,
                        })}
                      </span>
                    ) : null}
                  </div>
                  {selectedPage.is_ocr ? (
                    <p className="mb-2 rounded-xl border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800 dark:border-amber-900 dark:bg-amber-950/40 dark:text-amber-200">
                      {t("tenders.documentsPanel.ocrPageNote")}
                    </p>
                  ) : null}
                  {selectedPage.char_count > 0 ? (
                    <pre className="max-h-[26rem] overflow-auto whitespace-pre-wrap rounded-xl bg-navy-50 p-3 text-xs leading-5 text-navy-800 dark:bg-navy-950 dark:text-navy-100">
                      {selectedPage.text}
                    </pre>
                  ) : (
                    <p className="rounded-xl bg-amber-50 p-3 text-xs text-amber-800 dark:bg-amber-950/40 dark:text-amber-200">
                      {t("tenders.documentsPanel.pageNoText")}
                    </p>
                  )}
                </div>
              ) : null}
            </div>
          </div>
        )}
      </Modal>

      <PdfViewerModal
        doc={pdfDoc}
        onClose={() => setPdfDoc(null)}
        onViewText={(document) => {
          setPdfDoc(null);
          openPages(document);
        }}
      />
    </Card>
  );
}
