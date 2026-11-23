import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeAll, describe, expect, it, vi } from "vitest";

vi.mock("react-i18next", async (importOriginal) => {
  const actual = await importOriginal();
  return {
    ...actual,
    useTranslation: () => ({
      t: (key, options) => options?.defaultValue ?? key,
    }),
  };
});

vi.mock("../../contexts/ToastContext", () => ({
  useToast: () => ({
    success: vi.fn(),
    error: vi.fn(),
    info: vi.fn(),
    show: vi.fn(),
  }),
}));

vi.mock("../../lib/api", () => ({
  api: {
    get: vi.fn((path) =>
      path.startsWith("/jobs")
        ? Promise.resolve({ items: [] })
        : Promise.resolve({ items: [], covers: [], fetch_state: null }),
    ),
    getBlob: vi.fn(() => Promise.resolve(new Blob())),
    post: vi.fn(),
    postForm: vi.fn(),
    download: vi.fn(),
  },
}));

vi.mock("../../lib/useCaptureUrl", () => ({
  capturePath: () => null,
  useCaptureUrl: () => null,
}));

import { TenderDocumentsPanel } from "./TenderDocumentsPanel";
import { api } from "../../lib/api";

beforeAll(() => {
  // jsdom has no blob URL implementation; the PDF viewer uses one.
  URL.createObjectURL = vi.fn(() => "blob:tender-pdf");
  URL.revokeObjectURL = vi.fn();
});

describe("TenderDocumentsPanel", () => {
  it("renders the empty state without crashing", async () => {
    render(<TenderDocumentsPanel tenderId="tender-1" />);

    expect(
      await screen.findByText("tenders.documentsPanel.emptyTitle"),
    ).toBeInTheDocument();
  });

  it("cancels an active document fetch", async () => {
    const job = {
      id: "job-1",
      state: "running",
      payload: { tender_id: "tender-1" },
    };
    api.get.mockImplementation((path) => {
      if (path.startsWith("/jobs?")) return Promise.resolve({ items: [job] });
      if (path === "/jobs/job-1") return Promise.resolve(job);
      return Promise.resolve({ items: [], covers: [], fetch_state: null });
    });
    api.post.mockResolvedValue({ ...job, state: "cancelled" });

    render(<TenderDocumentsPanel tenderId="tender-1" />);

    const button = await screen.findByRole("button", {
      name: /Cancel fetch/i,
    });
    fireEvent.click(button);

    await waitFor(() =>
      expect(api.post).toHaveBeenCalledWith("/jobs/job-1/cancel", {}),
    );
  });

  it("opens the PDF in a native viewer using the authenticated bytes", async () => {
    const documentItem = {
      id: "doc-1",
      filename: "tender.pdf",
      page_count: 8,
      bytes: 2048,
      has_text_layer: true,
      created_at: "2026-01-01T00:00:00Z",
    };
    api.get.mockImplementation((path) => {
      if (path.startsWith("/jobs?")) return Promise.resolve({ items: [] });
      if (path === "/tenders/tender-1/documents") {
        return Promise.resolve({ items: [documentItem], covers: [], fetch_state: null });
      }
      return Promise.resolve({ items: [], covers: [], fetch_state: null });
    });
    api.getBlob.mockResolvedValue(
      new Blob([new Uint8Array([37, 80, 68, 70])], { type: "application/pdf" }),
    );

    render(<TenderDocumentsPanel tenderId="tender-1" />);

    fireEvent.click(await screen.findByRole("button", { name: /View PDF/i }));

    await waitFor(() =>
      expect(api.getBlob).toHaveBeenCalledWith("/tender-documents/doc-1/download"),
    );
    const frame = await screen.findByTitle("tender.pdf");
    expect(frame).toBeInTheDocument();
    expect(frame.getAttribute("src")).toContain("blob:tender-pdf");
  });
});
