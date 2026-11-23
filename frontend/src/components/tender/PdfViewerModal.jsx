import { useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { Download, ExternalLink, FileText, ScrollText, X } from "lucide-react";
import { useTranslation } from "react-i18next";

import { api } from "../../lib/api";
import { Badge } from "../ui/Badge";
import { Button } from "../ui/Button";
import { Spinner } from "../ui/Spinner";

/**
 * Full-screen reader for the tender PDF itself.
 *
 * The bytes are fetched with the auth header and handed to the browser as a
 * blob URL, so the native PDF viewer (thumbnails, zoom, search, print) renders
 * the document exactly as a PDF. The toolbar adds the actions the native
 * viewer cannot do from inside the app: open in a tab, download, and the
 * extracted-text view used for citations.
 */
export function PdfViewerModal({ doc, onClose, onViewText }) {
  const { t } = useTranslation();
  const [url, setUrl] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!doc) return undefined;
    let active = true;
    let objectUrl = null;

    setUrl(null);
    setError("");
    setLoading(true);
    api
      .getBlob(`/tender-documents/${doc.id}/download`)
      .then((blob) => {
        if (!active) return;
        // Some browsers download a blob whose type is missing instead of
        // rendering it, so the type is always stated explicitly.
        const pdf =
          blob.type === "application/pdf" ? blob : new Blob([blob], { type: "application/pdf" });
        objectUrl = URL.createObjectURL(pdf);
        setUrl(objectUrl);
      })
      .catch((err) => {
        if (!active) return;
        setError(
          err?.message === "NETWORK_ERROR" ? t("errors.network") : err?.message || t("errors.generic"),
        );
      })
      .finally(() => {
        if (active) setLoading(false);
      });

    return () => {
      active = false;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [doc?.id]);

  useEffect(() => {
    if (!doc) return undefined;
    const previousOverflow = window.document.body.style.overflow;
    window.document.body.style.overflow = "hidden";
    const onKeyDown = (event) => {
      if (event.key === "Escape") onClose?.();
    };
    window.document.addEventListener("keydown", onKeyDown);
    return () => {
      window.document.body.style.overflow = previousOverflow;
      window.document.removeEventListener("keydown", onKeyDown);
    };
  }, [doc, onClose]);

  if (!doc) return null;

  const openInTab = () => {
    if (url) window.open(url, "_blank", "noopener,noreferrer");
  };

  const download = () =>
    api.download(`/tender-documents/${doc.id}/download`, doc.filename);

  return createPortal(
    <div className="fixed inset-0 z-[90] flex flex-col bg-navy-950/80 backdrop-blur-sm sm:p-4">
      <div className="flex min-h-0 flex-1 flex-col overflow-hidden bg-white shadow-pop dark:bg-navy-900 sm:rounded-2xl">
        <div className="flex flex-wrap items-center justify-between gap-3 border-b border-navy-100 bg-white px-3 py-2.5 dark:border-navy-800 dark:bg-navy-900 sm:px-4">
          <div className="flex min-w-0 items-center gap-3">
            <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-red-50 text-red-600 dark:bg-red-950/50 dark:text-red-300">
              <FileText className="h-5 w-5" />
            </span>
            <div className="min-w-0">
              <p className="truncate text-sm font-semibold text-navy-900 dark:text-white">
                {doc.filename}
              </p>
              <div className="mt-0.5 flex flex-wrap items-center gap-1.5 text-[11px] muted">
                <Badge tone="red">PDF</Badge>
                <span>
                  {t("tenders.documentsPanel.pages", { count: doc.page_count ?? 0 })}
                </span>
              </div>
            </div>
          </div>

          <div className="flex flex-wrap items-center gap-1.5">
            {onViewText ? (
              <Button
                size="sm"
                variant="ghost"
                onClick={() => onViewText(doc)}
                title={t("tenders.documentsPanel.viewText", {
                  defaultValue: "View extracted text",
                })}
              >
                <ScrollText className="h-3.5 w-3.5" />
                <span className="hidden sm:inline">
                  {t("tenders.documentsPanel.viewText", { defaultValue: "Text" })}
                </span>
              </Button>
            ) : null}
            <Button
              size="sm"
              variant="ghost"
              onClick={openInTab}
              disabled={!url}
              title={t("tenders.documentsPanel.openInNewTab", {
                defaultValue: "Open in new tab",
              })}
            >
              <ExternalLink className="h-3.5 w-3.5" />
              <span className="hidden sm:inline">
                {t("tenders.documentsPanel.openInNewTab", {
                  defaultValue: "Open in new tab",
                })}
              </span>
            </Button>
            <Button size="sm" variant="outline" onClick={download}>
              <Download className="h-3.5 w-3.5" />
              <span className="hidden sm:inline">
                {t("tenders.documentsPanel.download", { defaultValue: "Download" })}
              </span>
            </Button>
            <Button
              size="icon"
              variant="ghost"
              onClick={onClose}
              aria-label={t("tenders.documentsPanel.closePdf", { defaultValue: "Close PDF" })}
              title={t("tenders.documentsPanel.closePdf", { defaultValue: "Close PDF" })}
            >
              <X className="h-4 w-4" />
            </Button>
          </div>
        </div>

        <div className="relative min-h-0 flex-1 bg-navy-200 dark:bg-navy-950">
          {loading ? (
            <div className="absolute inset-0 z-10 grid place-items-center gap-3 bg-white/80 dark:bg-navy-900/80">
              <div className="flex flex-col items-center gap-2">
                <Spinner className="h-7 w-7" />
                <p className="text-sm muted">
                  {t("tenders.documentsPanel.pdfLoading", {
                    defaultValue: "Loading PDF...",
                  })}
                </p>
              </div>
            </div>
          ) : null}

          {error ? (
            <div className="grid h-full place-items-center p-6">
              <div className="max-w-sm text-center">
                <p className="text-sm font-semibold text-navy-900 dark:text-white">
                  {t("tenders.documentsPanel.pdfLoadError", {
                    defaultValue: "The PDF could not be shown here.",
                  })}
                </p>
                <p className="mt-1 text-xs muted">{error}</p>
                <Button size="sm" className="mt-4" onClick={download}>
                  <Download className="h-3.5 w-3.5" />
                  {t("tenders.documentsPanel.download", { defaultValue: "Download" })}
                </Button>
              </div>
            </div>
          ) : null}

          {url ? (
            <iframe
              src={`${url}#toolbar=1&navpanes=0&view=FitH`}
              title={doc.filename}
              className="h-full w-full border-0 bg-white"
            />
          ) : null}
        </div>
      </div>
    </div>,
    window.document.body,
  );
}
