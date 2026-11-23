import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { ChangeRow } from "./TenderChangesPanel";

vi.mock("react-i18next", async (importOriginal) => {
  const actual = await importOriginal();
  return {
    ...actual,
    useTranslation: () => ({
      t: (key, options) => {
        if (options?.defaultValue) return options.defaultValue;
        if (key.includes("changes.fields.")) {
          const field = key.split(".").pop();
          return field.replace(/_/g, " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
        }
        return key;
      },
    }),
  };
});

const change = {
  id: "change-1",
  tender_id: "tender-1",
  from_snapshot_id: "snapshot-1",
  to_snapshot_id: "snapshot-2",
  change_type: "changed",
  field_key: "closing_at",
  old_value: "2026-11-01",
  new_value: "2026-11-15",
  review_state: "pending",
  source_url: "https://example.invalid/notice",
  detected_at: "2026-10-20T09:00:00Z",
};

function renderRow(props = {}) {
  return render(
    <MemoryRouter>
      <ChangeRow change={change} onReview={() => {}} busy={false} {...props} />
    </MemoryRouter>,
  );
}

describe("ChangeRow", () => {
  it("shows the field and both values of a detected change", () => {
    const { unmount } = renderRow();
    expect(screen.getByText("Closing At")).toBeInTheDocument();
    expect(screen.getByText("changed")).toBeInTheDocument();
    expect(screen.getByText("2026-11-01")).toBeInTheDocument();
    expect(screen.getByText("2026-11-15")).toBeInTheDocument();
    unmount();
  });

  it("offers review actions only while a change is pending", () => {
    const { unmount } = renderRow();
    expect(screen.getByRole("button", { name: /changes.acknowledge/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /changes.dismiss/ })).toBeInTheDocument();
    unmount();

    renderRow({ change: { ...change, review_state: "acknowledged" } });
    expect(screen.queryByRole("button", { name: /changes.acknowledge/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /changes.dismiss/ })).not.toBeInTheDocument();
  });

  it("links to the source of the change when one was recorded", () => {
    renderRow();
    const link = screen.getByRole("link", { name: /tenders.requirements.source/ });
    expect(link).toHaveAttribute("href", "https://example.invalid/notice");
    expect(link).toHaveAttribute("rel", expect.stringContaining("noopener"));
  });
});
