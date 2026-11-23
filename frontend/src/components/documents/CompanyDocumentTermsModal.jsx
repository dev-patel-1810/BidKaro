import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { RefreshCw, Sparkles, Trash2 } from "lucide-react";

import { api } from "../../lib/api";
import { currentLocale } from "../../i18n";
import { useToast } from "../../contexts/ToastContext";
import { Button } from "../ui/Button";
import { Modal } from "../ui/Modal";
import { cn, formatDateTime } from "../../lib/utils";

/**
 * What the reader took from an about-company profile.
 *
 * A term is shown with its page and the verbatim line it came from, because a
 * suggestion the user cannot trace is a suggestion they cannot trust. Removing
 * a term only changes matching; nothing here can make an eligibility criterion
 * pass.
 */
export function CompanyDocumentTermsModal({
  document,
  open,
  onClose,
  onChanged,
}) {
  const { t } = useTranslation();
  const toast = useToast();
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [rereading, setRereading] = useState(false);
  const [removing, setRemoving] = useState("");

  const load = useCallback(async () => {
    if (!document) return;
    setError("");
    setLoading(true);
    try {
      setData(await api.get(`/company/documents/${document.id}/terms`));
    } catch (err) {
      setError(
        err.message === "NETWORK_ERROR"
          ? t("errors.network")
          : err.message || t("errors.generic"),
      );
    } finally {
      setLoading(false);
    }
  }, [document, t]);

  useEffect(() => {
    if (!open) {
      setData(null);
      setError("");
      setRereading(false);
      setRemoving("");
      return;
    }
    load();
  }, [open, load]);

  const state = data?.extraction_state || document?.extraction_state || "pending";
  const terms = data?.items || [];
  const stateError = data?.extraction_error || document?.extraction_error;
  const extractedAt = data?.extracted_at || document?.extracted_at;
  const canReread = document?.mime === "application/pdf";

  const reread = async () => {
    if (!document) return;
    setRereading(true);
    setError("");
    try {
      await api.post(`/company/documents/${document.id}/extract`, {});
      toast.success(t("documents.intel.queued"));
      await load();
      onChanged?.();
    } catch (err) {
      setError(
        err.message === "NETWORK_ERROR"
          ? t("errors.network")
          : err.message || t("errors.generic"),
      );
    } finally {
      setRereading(false);
    }
  };

  const remove = async (term) => {
    if (!document) return;
    setRemoving(term.id);
    setError("");
    try {
      await api.delete(`/company/documents/${document.id}/terms/${term.id}`);
      setData((current) =>
        current
          ? {
              ...current,
              items: current.items.filter((item) => item.id !== term.id),
              term_count: Math.max(
                0,
                (current.term_count ?? current.items.length) - 1,
              ),
            }
          : current,
      );
      toast.success(t("documents.intel.removed"));
      onChanged?.();
    } catch (err) {
      setError(
        err.message === "NETWORK_ERROR"
          ? t("errors.network")
          : err.message || t("errors.generic"),
      );
    } finally {
      setRemoving("");
    }
  };

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={t("documents.intel.title")}
      description={t("documents.intel.subtitle")}
      size="lg"
      footer={
        <>
          {canReread ? (
            <Button variant="outline" onClick={reread} loading={rereading}>
              <RefreshCw className="h-4 w-4" />
              {t("documents.intel.reread")}
            </Button>
          ) : null}
          <Button onClick={onClose}>{t("documents.intel.close")}</Button>
        </>
      }
    >
      {error ? (
        <p className="mb-3 rounded-lg bg-red-50 px-3 py-2 text-sm text-red-700 dark:bg-red-950/40 dark:text-red-200">
          {error}
        </p>
      ) : null}

      {loading && !data ? (
        <p className="text-sm muted">{t("documents.intel.loading")}</p>
      ) : null}

      {extractedAt ? (
        <p className="mb-3 text-xs muted">
          {t("documents.intel.readAt", {
            date: formatDateTime(extractedAt, currentLocale()),
          })}
        </p>
      ) : null}

      {state !== "done" ? (
        <div className="rounded-xl border border-navy-100 bg-navy-50/60 p-4 dark:border-navy-800 dark:bg-navy-800/40">
          <p
            className={cn(
              "text-sm font-medium",
              state === "pending"
                ? "text-navy-800 dark:text-navy-100"
                : "text-amber-800 dark:text-amber-200",
            )}
          >
            {t(`documents.intel.states.${state}`, { defaultValue: state })}
          </p>
          {stateError ? <p className="mt-1 text-xs muted">{stateError}</p> : null}
          {state === "pending" ? (
            <Button
              variant="outline"
              size="sm"
              className="mt-3"
              onClick={() => {
                load();
                onChanged?.();
              }}
            >
              {t("documents.intel.checkAgain")}
            </Button>
          ) : null}
        </div>
      ) : terms.length === 0 ? (
        <p className="rounded-xl border border-navy-100 p-4 text-sm muted dark:border-navy-800">
          {t("documents.intel.noTerms")}
        </p>
      ) : (
        <ul className="space-y-2">
          {terms.map((term) => (
            <li
              key={term.id}
              className="rounded-xl border border-navy-100 p-3 dark:border-navy-800"
            >
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <p className="flex items-center gap-2 text-sm font-semibold text-navy-900 dark:text-white">
                    <Sparkles className="h-3.5 w-3.5 shrink-0 text-teal-600 dark:text-teal-300" />
                    <span className="break-words">{term.term}</span>
                  </p>
                  <p className="mt-1 text-xs muted">
                    {t("documents.intel.page", {
                      page: term.printed_label || term.page_index,
                    })}
                  </p>
                </div>
                <Button
                  variant="ghost"
                  size="icon"
                  onClick={() => remove(term)}
                  loading={removing === term.id}
                  aria-label={t("documents.intel.remove")}
                  title={t("documents.intel.remove")}
                >
                  <Trash2 className="h-4 w-4 text-red-600" />
                </Button>
              </div>
              {term.excerpt ? (
                <p className="mt-2 border-l-2 border-teal-200 pl-2 text-xs italic text-navy-600 dark:border-teal-800 dark:text-navy-300">
                  “{term.excerpt}”
                </p>
              ) : null}
            </li>
          ))}
        </ul>
      )}

      <p className="mt-4 text-xs muted">{t("documents.intel.note")}</p>
    </Modal>
  );
}
