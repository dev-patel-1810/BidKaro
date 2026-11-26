import { useTranslation } from "react-i18next";

import { Badge } from "../ui/Badge";
import {
  matchTypeLabelKey,
  matchTypeTone,
  similarityLabelKey,
  similarityTone,
} from "../../lib/discovery";
import { cn } from "../../lib/utils";

function EvidenceLine({ label, text, missing, tone }) {
  return (
    <div
      className={cn(
        "min-w-0 rounded-lg border-l-2 px-2.5 py-2",
        tone === "teal"
          ? "border-teal-300 bg-teal-50/60 dark:border-teal-700 dark:bg-teal-950/40"
          : "border-navy-300 bg-navy-50/70 dark:border-navy-600 dark:bg-navy-800/60",
      )}
    >
      <p className="text-[10px] font-bold uppercase tracking-wide text-navy-400">
        {label}
      </p>
      {text ? (
        <blockquote className="mt-1 break-words text-xs italic leading-5 text-navy-800 dark:text-navy-100">
          &ldquo;{text}&rdquo;
        </blockquote>
      ) : (
        <p className="mt-1 text-xs leading-5 muted">{missing}</p>
      )}
    </div>
  );
}

// Both sides of a work-comparison answer, with the exact stored lines. The
// component never builds a sentence the model did not quote: a missing side
// says so, and the quote is rendered as it was saved.
export function SimilarityEvidence({ similarity, compact = false, className }) {
  const { t } = useTranslation();
  if (!similarity) return null;

  const labelKey = similarityLabelKey(similarity);
  const matchKey = matchTypeLabelKey(similarity.match_type);
  const tenderEvidence = similarity.tender_evidence || similarity.anchor_text;
  const companyEvidence = similarity.company_evidence;
  const rank = Number(similarity.evidence_rank);

  return (
    <div
      className={cn(
        "rounded-xl border border-navy-100 px-3.5 py-2.5 dark:border-navy-800",
        className,
      )}
    >
      <div className="flex flex-wrap items-center gap-2">
        <p className="text-[11px] font-semibold uppercase tracking-wide text-navy-400">
          {t("discover.similarity.title")}
        </p>
        {labelKey ? (
          <Badge tone={similarityTone(similarity)}>{t(labelKey)}</Badge>
        ) : null}
        {matchKey ? (
          <Badge tone={matchTypeTone(similarity.match_type)}>
            {t(matchKey, { defaultValue: similarity.match_type })}
          </Badge>
        ) : null}
        {Number.isFinite(rank) && rank > 0 ? (
          <span className="text-[11px] font-medium muted">
            {t("discover.similarity.evidenceRank", { rank })}
          </span>
        ) : null}
      </div>

      {!compact && similarity.title ? (
        <p className="mt-1.5 text-xs font-medium text-navy-700 dark:text-navy-200">
          {t("discover.similarity.judgedAs", { title: similarity.title })}
        </p>
      ) : null}
      {similarity.reason ? (
        <p className="mt-1 text-xs leading-5 muted">{similarity.reason}</p>
      ) : null}

      <div className={cn("mt-2 grid gap-2", compact ? "" : "sm:grid-cols-2")}>
        <EvidenceLine
          label={t("discover.similarity.tenderEvidence")}
          text={tenderEvidence}
          missing={t("discover.similarity.noTenderEvidence")}
          tone="teal"
        />
        <EvidenceLine
          label={t("discover.similarity.companyEvidence")}
          text={companyEvidence}
          missing={t("discover.similarity.noCompanyEvidence")}
          tone="navy"
        />
      </div>

      {similarity.capabilities?.length ? (
        <p className="mt-1.5 text-[11px] muted">
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
  );
}
