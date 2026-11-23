import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { FileText, UploadCloud, X } from "lucide-react";

import { api } from "../../lib/api";
import { cn, formatBytes } from "../../lib/utils";
import { useToast } from "../../contexts/ToastContext";
import { Button } from "../ui/Button";
import { FieldError, Input, Label, Select, Textarea } from "../ui/Field";
import { Modal } from "../ui/Modal";

const TYPES = ["about_company", "registration", "iso", "turnover", "completion_certificate", "pan", "gst", "other"];
const MAX_BYTES = 25 * 1024 * 1024;
const ACCEPTED = ".pdf,.png,.jpg,.jpeg";

const EMPTY = {
  title: "",
  document_type: "registration",
  issuer: "",
  signatory: "",
  financial_year: "",
  issued_on: "",
  expires_on: "",
  project_value_amount: "",
  project_value_currency: "INR",
  notes: "",
};

export function DocumentUploadModal({ open, onClose, onUploaded, initialType = "registration" }) {
  const { t } = useTranslation();
  const toast = useToast();
  const inputRef = useRef(null);
  const [file, setFile] = useState(null);
  const [form, setForm] = useState(() => ({ ...EMPTY, document_type: initialType }));
  const [dragging, setDragging] = useState(false);
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    if (open) {
      setFile(null);
      setForm({ ...EMPTY, document_type: initialType });
      setError("");
      setSubmitting(false);
    }
  }, [open, initialType]);

  const update = (field) => (event) =>
    setForm((current) => ({ ...current, [field]: event.target.value }));

  const isPdfFile = (candidate) =>
    candidate.name.split(".").pop()?.toLowerCase() === "pdf";

  const chooseFile = (candidate) => {
    if (!candidate) return;
    const extension = candidate.name.split(".").pop()?.toLowerCase();
    if (!["pdf", "png", "jpg", "jpeg"].includes(extension)) {
      setError(t("documents.invalidType"));
      return;
    }
    if (form.document_type === "about_company" && extension !== "pdf") {
      setError(t("documents.intel.pdfOnly"));
      return;
    }
    if (candidate.size > MAX_BYTES) {
      setError(t("documents.fileTooLarge"));
      return;
    }
    setError("");
    setFile(candidate);
    setForm((current) => ({
      ...current,
      title: current.title || candidate.name.replace(/\.[^.]+$/, ""),
    }));
  };

  const submit = async (event) => {
    event.preventDefault();
    if (!file) {
      setError(t("documents.chooseFileRequired"));
      return;
    }
    if (form.document_type === "about_company" && !isPdfFile(file)) {
      setError(t("documents.intel.pdfOnly"));
      return;
    }
    setError("");
    setSubmitting(true);
    try {
      const data = new FormData();
      data.append("file", file);
      Object.entries(form).forEach(([key, value]) => {
        if (value !== "" && value !== null && value !== undefined) data.append(key, value);
      });
      const document = await api.postForm("/company/documents", data);
      toast.success(t("documents.uploadSuccess"));
      onUploaded?.(document);
      onClose?.();
    } catch (err) {
      setError(err.message === "NETWORK_ERROR" ? t("errors.network") : err.message || t("errors.generic"));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={t("documents.uploadTitle")}
      description={t("documents.uploadSubtitle")}
      size="lg"
      footer={
        <>
          <Button variant="outline" onClick={onClose} disabled={submitting}>
            {t("common.cancel")}
          </Button>
          <Button loading={submitting} type="submit" form="document-upload-form">
            {submitting ? t("documents.uploading") : t("documents.save")}
          </Button>
        </>
      }
    >
      <form id="document-upload-form" onSubmit={submit} className="space-y-4">
        {file ? (
          <div className="flex items-center gap-3 rounded-xl border border-teal-200 bg-teal-50/60 p-3 dark:border-teal-800 dark:bg-teal-950/40">
            <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-white text-teal-700 dark:bg-navy-900 dark:text-teal-300">
              <FileText className="h-5 w-5" />
            </div>
            <div className="min-w-0 flex-1">
              <p className="truncate text-sm font-semibold text-navy-900 dark:text-white">{file.name}</p>
              <p className="text-xs muted">{formatBytes(file.size)}</p>
            </div>
            <button
              type="button"
              onClick={() => setFile(null)}
              className="rounded-md p-1 text-navy-500 hover:bg-white hover:text-navy-900 dark:hover:bg-navy-800"
              aria-label={t("common.remove")}
            >
              <X className="h-4 w-4" />
            </button>
          </div>
        ) : (
          <div
            onDragOver={(event) => {
              event.preventDefault();
              setDragging(true);
            }}
            onDragLeave={() => setDragging(false)}
            onDrop={(event) => {
              event.preventDefault();
              setDragging(false);
              chooseFile(event.dataTransfer.files?.[0]);
            }}
            className={cn(
              "flex flex-col items-center justify-center rounded-xl border-2 border-dashed px-6 py-8 text-center transition",
              dragging
                ? "border-teal-500 bg-teal-50 dark:bg-teal-950/40"
                : "border-navy-200 bg-navy-50/50 dark:border-navy-700 dark:bg-navy-800/40",
            )}
          >
            <div className="mb-2 flex h-11 w-11 items-center justify-center rounded-xl bg-white text-teal-700 shadow-sm dark:bg-navy-900 dark:text-teal-300">
              <UploadCloud className="h-5 w-5" />
            </div>
            <p className="text-sm font-medium text-navy-800 dark:text-navy-100">{t("documents.drop")}</p>
            <p className="mt-1 text-xs muted">{t("documents.supported")}</p>
            <Button
              type="button"
              variant="outline"
              size="sm"
              className="mt-3"
              onClick={() => inputRef.current?.click()}
            >
              {t("documents.browse")}
            </Button>
            <input
              ref={inputRef}
              type="file"
              accept={ACCEPTED}
              className="hidden"
              onChange={(event) => chooseFile(event.target.files?.[0])}
            />
          </div>
        )}

        <FieldError>{error}</FieldError>

        <div className="grid gap-4 sm:grid-cols-2">
          <div className="sm:col-span-2">
            <Label htmlFor="document-title">{t("documents.titleLabel")} *</Label>
            <Input id="document-title" value={form.title} onChange={update("title")} required />
          </div>
          <div>
            <Label htmlFor="document-type">{t("documents.typeLabel")} *</Label>
            <Select id="document-type" value={form.document_type} onChange={update("document_type")}>
              {TYPES.map((type) => (
                <option key={type} value={type}>
                  {t(`documents.types.${type}`)}
                </option>
              ))}
            </Select>
            {form.document_type === "about_company" ? (
              <p className="mt-1.5 text-xs text-teal-700 dark:text-teal-300">
                {t("documents.aboutCompanyHint")}
              </p>
            ) : null}
          </div>
          <div>
            <Label htmlFor="document-issuer">{t("documents.issuer")}</Label>
            <Input id="document-issuer" value={form.issuer} onChange={update("issuer")} />
          </div>
          <div>
            <Label htmlFor="document-signatory">{t("documents.signatory")}</Label>
            <Input id="document-signatory" value={form.signatory} onChange={update("signatory")} />
          </div>
          <div>
            <Label htmlFor="document-fy">{t("documents.financialYear")}</Label>
            <Input id="document-fy" value={form.financial_year} onChange={update("financial_year")} placeholder="2025-26" />
          </div>
          <div>
            <Label htmlFor="document-issue">{t("documents.issueDate")}</Label>
            <Input id="document-issue" type="date" value={form.issued_on} onChange={update("issued_on")} />
          </div>
          <div>
            <Label htmlFor="document-expiry">{t("documents.expiryDate")}</Label>
            <Input id="document-expiry" type="date" value={form.expires_on} onChange={update("expires_on")} />
          </div>
          <div>
            <Label htmlFor="document-value">{t("documents.projectValue")}</Label>
            <Input
              id="document-value"
              type="number"
              min="0"
              step="0.01"
              value={form.project_value_amount}
              onChange={update("project_value_amount")}
            />
          </div>
          <div className="sm:col-span-2">
            <Label htmlFor="document-notes">{t("documents.notes")}</Label>
            <Textarea id="document-notes" rows={2} value={form.notes} onChange={update("notes")} />
          </div>
        </div>
      </form>
    </Modal>
  );
}
