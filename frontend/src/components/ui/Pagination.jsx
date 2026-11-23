import { ChevronLeft, ChevronRight } from "lucide-react";
import { useTranslation } from "react-i18next";

import { Button } from "./Button";

// One page of tenders at a time. The server decides the order and the page, so
// this control only asks for the neighbouring offset and never re-sorts rows.
export function Pagination({ page, pages, total, pageSize = 10, onPage, disabled = false }) {
  const { t } = useTranslation();
  const safePages = Math.max(1, Number(pages) || 1);
  const safePage = Math.min(Math.max(1, Number(page) || 1), safePages);
  const safeTotal = Math.max(0, Number(total) || 0);
  if (safeTotal === 0 || safePages <= 1) return null;

  const start = (safePage - 1) * pageSize + 1;
  const end = Math.min(safePage * pageSize, safeTotal);

  return (
    <nav
      aria-label={t("pagination.label", { defaultValue: "Pagination" })}
      className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-navy-100 px-4 py-3 dark:border-navy-800"
    >
      <p className="text-xs muted">
        {t("pagination.showing", {
          start,
          end,
          total: safeTotal,
          defaultValue: "Showing {{start}}–{{end}} of {{total}}",
        })}
      </p>
      <div className="flex items-center gap-1.5">
        <Button
          variant="outline"
          size="sm"
          disabled={disabled || safePage <= 1}
          onClick={() => onPage(safePage - 1)}
          aria-label={t("pagination.previous", { defaultValue: "Previous page" })}
        >
          <ChevronLeft className="h-4 w-4" />
          <span className="hidden sm:inline">
            {t("pagination.previous", { defaultValue: "Previous" })}
          </span>
        </Button>
        <span className="px-2 text-xs font-semibold text-navy-700 dark:text-navy-200">
          {t("pagination.pageOf", {
            page: safePage,
            pages: safePages,
            defaultValue: "Page {{page}} of {{pages}}",
          })}
        </span>
        <Button
          variant="outline"
          size="sm"
          disabled={disabled || safePage >= safePages}
          onClick={() => onPage(safePage + 1)}
          aria-label={t("pagination.next", { defaultValue: "Next page" })}
        >
          <span className="hidden sm:inline">
            {t("pagination.next", { defaultValue: "Next" })}
          </span>
          <ChevronRight className="h-4 w-4" />
        </Button>
      </div>
    </nav>
  );
}
