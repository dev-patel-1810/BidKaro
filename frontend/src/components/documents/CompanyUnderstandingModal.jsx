import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  Briefcase,
  Check,
  Pencil,
  RotateCcw,
  Sparkles,
  X,
} from "lucide-react";

import { api } from "../../lib/api";
import { currentLocale } from "../../i18n";
import { useToast } from "../../contexts/ToastContext";
import { Badge, StatusBadge } from "../ui/Badge";
import { Button } from "../ui/Button";
import { Input } from "../ui/Field";
import { Modal } from "../ui/Modal";

const STATE_TONE = {
  confirmed: "green",
  proposed: "amber",
  rejected: "neutral",
};

function StateBadge({ state }) {
  const { t } = useTranslation();
  return (
    <Badge tone={STATE_TONE[state] || "neutral"}>
      {t(`documents.understanding.states.${state}`, { defaultValue: state })}
    </Badge>
  );
}

function pageLabel(item) {
  const label =
    item.printed_label && item.printed_label !== String(item.page_index)
      ? `${item.page_index} (${item.printed_label})`
      : item.page_index;
  return label;
}

function ItemShell({ item, busy, children, actions, footer }) {
  const { t } = useTranslation();
  return (
    <li className="rounded-xl border border-navy-100 p-3 dark:border-navy-800">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">{children}</div>
        <div className="flex shrink-0 flex-wrap justify-end gap-1">{actions}</div>
      </div>
      {item.excerpt ? (
        <p className="mt-2 border-l-2 border-teal-200 pl-2 text-xs italic text-navy-600 dark:border-teal-800 dark:text-navy-300">
          “{item.excerpt}”
        </p>
      ) : null}
      <p className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] muted">
        <span>
          {t("documents.understanding.page", { page: pageLabel(item) })}
        </span>
        {item.quote_verified ? (
          <span className="inline-flex items-center gap-1 text-emerald-700 dark:text-emerald-300">
            <Check className="h-3 w-3" />
            {t("documents.understanding.quoteChecked")}
          </span>
        ) : (
          <span className="inline-flex items-center gap-1 text-amber-700 dark:text-amber-300">
            {t("documents.understanding.quoteUnchecked")}
          </span>
        )}
        {item.is_ocr ? (
          <Badge tone="amber">{t("documents.understanding.ocr")}</Badge>
        ) : null}
        {busy ? <span>{t("documents.understanding.saving")}</span> : null}
      </p>
      {footer}
    </li>
  );
}

/**
 * What the company's own documents say it does and has delivered.
 *
 * Each item carries the page and the verbatim line it came from, and only a
 * human-confirmed item is used to compare tenders. Nothing on this screen can
 * pass an eligibility criterion: that gate stays deterministic and separate.
 */
export function CompanyUnderstandingModal({ open, onClose, onChanged }) {
  const { t } = useTranslation();
  const toast = useToast();
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState("");
  const [editing, setEditing] = useState(null);
  const [draft, setDraft] = useState({});

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      setData(await api.get("/company/understanding"));
    } catch (err) {
      setError(
        err.message === "NETWORK_ERROR"
          ? t("errors.network")
          : err.message || t("errors.generic"),
      );
    } finally {
      setLoading(false);
    }
  }, [t]);

  useEffect(() => {
    if (!open) {
      setData(null);
      setError("");
      setBusy("");
      setEditing(null);
      setDraft({});
      return;
    }
    load();
  }, [open, load]);

  const capabilities = data?.capabilities || [];
  const experiences = data?.experiences || [];
  const counts = data?.counts || {};
  const documents = data?.documents || [];

  const recalculate = (next) => {
    const rows = next || data;
    const capabilitiesNext = rows?.capabilities || [];
    const experiencesNext = rows?.experiences || [];
    return {
      ...rows,
      counts: {
        capabilities: capabilitiesNext.length,
        experiences: experiencesNext.length,
        confirmed_capabilities: capabilitiesNext.filter(
          (row) => row.state === "confirmed",
        ).length,
        confirmed_experiences: experiencesNext.filter(
          (row) => row.state === "confirmed",
        ).length,
      },
    };
  };

  const update = async (kind, item, body) => {
    const path = kind === "capability" ? "capabilities" : "experiences";
    const key = kind === "capability" ? "capabilities" : "experiences";
    setBusy(item.id);
    setError("");
    try {
      const updated = await api.patch(`/company/${path}/${item.id}`, body);
      setData((current) => {
        if (!current) return current;
        return recalculate({
          ...current,
          [key]: current[key].map((row) =>
            row.id === updated.id ? updated : row,
          ),
        });
      });
      setEditing(null);
      setDraft({});
      onChanged?.();
      return updated;
    } catch (err) {
      const message =
        err.message === "NETWORK_ERROR"
          ? t("errors.network")
          : err.message || t("errors.generic");
      setError(message);
      toast.error(message);
      return null;
    } finally {
      setBusy("");
    }
  };

  const setState = (kind, item, state) => update(kind, item, { state });

  const startEdit = (kind, item) => {
    setEditing({ kind, id: item.id });
    setDraft(
      kind === "capability"
        ? {
            name: item.name || "",
            description: item.description || "",
            aliases: (item.aliases || []).join(", "),
          }
        : {
            title: item.title || "",
            client: item.client || "",
            work_family: item.work_family || "",
            description: item.description || "",
          },
    );
  };

  const saveEdit = async (kind, item) => {
    const body =
      kind === "capability"
        ? {
            name: draft.name?.trim() || item.name,
            description: draft.description?.trim() || null,
            aliases: (draft.aliases || "")
              .split(",")
              .map((value) => value.trim())
              .filter(Boolean),
          }
        : {
            title: draft.title?.trim() || item.title,
            client: draft.client?.trim() || null,
            work_family: draft.work_family?.trim() || null,
            description: draft.description?.trim() || null,
          };
    await update(kind, item, body);
  };

  const editFields = (kind, item) => {
    if (editing?.kind !== kind || editing?.id !== item.id) return null;
    const field = (key) => ({
      value: draft[key] ?? "",
      onChange: (event) =>
        setDraft((current) => ({ ...current, [key]: event.target.value })),
    });
    return (
      <div className="mt-2 space-y-2 rounded-lg bg-navy-50 p-2.5 dark:bg-navy-800/60">
        <Input
          value={draft[kind === "capability" ? "name" : "title"] ?? ""}
          onChange={(event) =>
            setDraft((current) => ({
              ...current,
              [kind === "capability" ? "name" : "title"]: event.target.value,
            }))
          }
          aria-label={
            kind === "capability"
              ? t("documents.understanding.nameLabel")
              : t("documents.understanding.titleLabel")
          }
          placeholder={
            kind === "capability"
              ? t("documents.understanding.nameLabel")
              : t("documents.understanding.titleLabel")
          }
        />
        {kind === "experience" ? (
          <div className="grid gap-2 sm:grid-cols-2">
            <Input
              {...field("client")}
              aria-label={t("documents.understanding.clientLabel")}
              placeholder={t("documents.understanding.clientLabel")}
            />
            <Input
              {...field("work_family")}
              aria-label={t("documents.understanding.familyLabel")}
              placeholder={t("documents.understanding.familyLabel")}
            />
          </div>
        ) : null}
        <Input
          {...field("description")}
          aria-label={t("documents.understanding.descriptionLabel")}
          placeholder={t("documents.understanding.descriptionLabel")}
        />
        {kind === "capability" ? (
          <Input
            {...field("aliases")}
            aria-label={t("documents.understanding.aliasesLabel")}
            placeholder={t("documents.understanding.aliasesPlaceholder")}
          />
        ) : null}
        <div className="flex flex-wrap gap-2">
          <Button
            size="sm"
            onClick={() => saveEdit(kind, item)}
            loading={busy === item.id}
          >
            {t("common.save")}
          </Button>
          <Button
            size="sm"
            variant="ghost"
            onClick={() => {
              setEditing(null);
              setDraft({});
            }}
          >
            {t("common.cancel")}
          </Button>
        </div>
      </div>
    );
  };

  const actions = (kind, item) => (
    <>
      {item.state !== "confirmed" ? (
        <Button
          size="sm"
          variant="subtle"
          onClick={() => setState(kind, item, "confirmed")}
          loading={busy === item.id}
          title={t("documents.understanding.confirmHint")}
        >
          <Check className="h-3.5 w-3.5" />
          {t("documents.understanding.confirm")}
        </Button>
      ) : null}
      <Button
        size="sm"
        variant="ghost"
        onClick={() => startEdit(kind, item)}
        aria-label={t("documents.understanding.edit")}
        title={t("documents.understanding.edit")}
      >
        <Pencil className="h-3.5 w-3.5" />
      </Button>
      {item.state !== "rejected" ? (
        <Button
          size="sm"
          variant="ghost"
          onClick={() => setState(kind, item, "rejected")}
          loading={busy === item.id}
          aria-label={t("documents.understanding.reject")}
          title={t("documents.understanding.reject")}
        >
          <X className="h-3.5 w-3.5" />
        </Button>
      ) : (
        <Button
          size="sm"
          variant="ghost"
          onClick={() => setState(kind, item, "proposed")}
          loading={busy === item.id}
          aria-label={t("documents.understanding.restore")}
          title={t("documents.understanding.restore")}
        >
          <RotateCcw className="h-3.5 w-3.5" />
        </Button>
      )}
    </>
  );

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={t("documents.understanding.title")}
      description={t("documents.understanding.subtitle")}
      size="lg"
      footer={<Button onClick={onClose}>{t("documents.understanding.close")}</Button>}
    >
      {error ? (
        <p className="mb-3 rounded-lg bg-red-50 px-3 py-2 text-sm text-red-700 dark:bg-red-950/40 dark:text-red-200">
          {error}
        </p>
      ) : null}

      {loading && !data ? (
        <p className="text-sm muted">{t("documents.understanding.loading")}</p>
      ) : null}

      {data ? (
        <div className="space-y-5">
          <div className="rounded-xl border border-teal-200 bg-teal-50/60 px-4 py-3 text-xs leading-5 text-teal-900 dark:border-teal-800 dark:bg-teal-950/40 dark:text-teal-100">
            <p className="font-semibold">
              {t("documents.understanding.ruleTitle")}
            </p>
            <p className="mt-1">{t("documents.understanding.ruleBody")}</p>
          </div>

          <div className="flex flex-wrap gap-2">
            <Badge tone="green">
              {t("documents.understanding.confirmedCount", {
                count: counts.confirmed_capabilities ?? 0,
              })}
            </Badge>
            <Badge tone="green">
              {t("documents.understanding.confirmedExperienceCount", {
                count: counts.confirmed_experiences ?? 0,
              })}
            </Badge>
            <Badge tone="neutral">
              {t("documents.understanding.proposedCount", {
                count:
                  (counts.capabilities ?? 0) -
                    (counts.confirmed_capabilities ?? 0) +
                    ((counts.experiences ?? 0) -
                      (counts.confirmed_experiences ?? 0)),
              })}
            </Badge>
          </div>

          {documents.length ? (
            <div>
              <p className="text-[11px] font-bold uppercase tracking-wide text-navy-400">
                {t("documents.understanding.documents")}
              </p>
              <ul className="mt-2 space-y-1.5">
                {documents.map((document) => (
                  <li
                    key={document.document_id}
                    className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-navy-100 px-3 py-2 dark:border-navy-800"
                  >
                    <span className="min-w-0 truncate text-xs font-medium text-navy-800 dark:text-navy-100">
                      {document.title}
                    </span>
                    <span className="flex items-center gap-2">
                      <StatusBadge
                        status={document.understanding_state}
                        label={t(
                          `documents.understanding.docStates.${document.understanding_state}`,
                          { defaultValue: document.understanding_state },
                        )}
                      />
                      <span className="text-[11px] muted">
                        {t("documents.understanding.documentCounts", {
                          capabilities: document.capability_count,
                          experiences: document.experience_count,
                        })}
                      </span>
                    </span>
                  </li>
                ))}
              </ul>
            </div>
          ) : null}

          <section>
            <div className="flex items-center gap-2">
              <Sparkles className="h-4 w-4 text-teal-600 dark:text-teal-300" />
              <h3 className="text-sm font-semibold text-navy-900 dark:text-white">
                {t("documents.understanding.capabilities")}
              </h3>
            </div>
            <p className="mt-1 text-xs muted">
              {t("documents.understanding.capabilitiesHint")}
            </p>
            {capabilities.length ? (
              <ul className="mt-2 space-y-2">
                {capabilities.map((item) => (
                  <ItemShell
                    key={item.id}
                    item={item}
                    busy={busy === item.id}
                    actions={actions("capability", item)}
                    footer={editFields("capability", item)}
                  >
                    <p className="flex flex-wrap items-center gap-2 text-sm font-semibold text-navy-900 dark:text-white">
                      <span className="break-words">{item.name}</span>
                      <StateBadge state={item.state} />
                      {item.origin === "human" ? (
                        <Badge tone="blue">
                          {t("documents.understanding.edited")}
                        </Badge>
                      ) : null}
                    </p>
                    {item.description ? (
                      <p className="mt-1 text-xs leading-5 muted">
                        {item.description}
                      </p>
                    ) : null}
                    {item.aliases?.length ? (
                      <p className="mt-1 text-[11px] muted">
                        {t("documents.understanding.aliases", {
                          list: item.aliases.join(", "),
                        })}
                      </p>
                    ) : null}
                  </ItemShell>
                ))}
              </ul>
            ) : (
              <p className="mt-2 rounded-xl border border-navy-100 p-3 text-xs muted dark:border-navy-800">
                {t("documents.understanding.noCapabilities")}
              </p>
            )}
          </section>

          <section>
            <div className="flex items-center gap-2">
              <Briefcase className="h-4 w-4 text-teal-600 dark:text-teal-300" />
              <h3 className="text-sm font-semibold text-navy-900 dark:text-white">
                {t("documents.understanding.experiences")}
              </h3>
            </div>
            <p className="mt-1 text-xs muted">
              {t("documents.understanding.experiencesHint")}
            </p>
            {experiences.length ? (
              <ul className="mt-2 space-y-2">
                {experiences.map((item) => (
                  <ItemShell
                    key={item.id}
                    item={item}
                    busy={busy === item.id}
                    actions={actions("experience", item)}
                    footer={editFields("experience", item)}
                  >
                    <p className="flex flex-wrap items-center gap-2 text-sm font-semibold text-navy-900 dark:text-white">
                      <span className="break-words">{item.title}</span>
                      <StateBadge state={item.state} />
                      {item.origin === "human" ? (
                        <Badge tone="blue">
                          {t("documents.understanding.edited")}
                        </Badge>
                      ) : null}
                    </p>
                    <p className="mt-1 flex flex-wrap gap-x-3 gap-y-1 text-[11px] muted">
                      {item.client ? (
                        <span>
                          {t("documents.understanding.client", {
                            value: item.client,
                          })}
                        </span>
                      ) : null}
                      {item.work_family ? (
                        <span>
                          {t("documents.understanding.workFamily", {
                            value: item.work_family,
                          })}
                        </span>
                      ) : null}
                      {item.completed_on ? (
                        <span>
                          {t("documents.understanding.completed", {
                            date: new Date(item.completed_on).toLocaleDateString(
                              currentLocale(),
                            ),
                          })}
                        </span>
                      ) : null}
                    </p>
                    {item.description ? (
                      <p className="mt-1 text-xs leading-5 muted">
                        {item.description}
                      </p>
                    ) : null}
                  </ItemShell>
                ))}
              </ul>
            ) : (
              <p className="mt-2 rounded-xl border border-navy-100 p-3 text-xs muted dark:border-navy-800">
                {t("documents.understanding.noExperiences")}
              </p>
            )}
          </section>

          <p className="text-xs muted">{t("documents.understanding.footer")}</p>
        </div>
      ) : null}
    </Modal>
  );
}
