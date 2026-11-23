import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

vi.mock("react-i18next", async (importOriginal) => {
  const actual = await importOriginal();
  return {
    ...actual,
    useTranslation: () => ({
      t: (key, options = {}) => {
        const template = options?.defaultValue ?? key;
        return String(template).replace(/\{\{(\w+)\}\}/g, (_, name) =>
          String(options[name] ?? ""),
        );
      },
    }),
  };
});

import { Pagination } from "./Pagination";

describe("Pagination", () => {
  it("renders nothing when everything fits on one page", () => {
    const { container } = render(
      <Pagination page={1} pages={1} total={4} onPage={() => {}} />,
    );

    expect(container).toBeEmptyDOMElement();
  });

  it("asks for the neighbouring page and never re-sorts", async () => {
    const user = userEvent.setup();
    const onPage = vi.fn();
    render(<Pagination page={2} pages={4} total={35} onPage={onPage} />);

    expect(screen.getByText(/Showing 11–20 of 35/)).toBeInTheDocument();
    expect(screen.getByText(/Page 2 of 4/)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Next page" }));
    expect(onPage).toHaveBeenLastCalledWith(3);

    await user.click(screen.getByRole("button", { name: "Previous page" }));
    expect(onPage).toHaveBeenLastCalledWith(1);
    expect(onPage).toHaveBeenCalledTimes(2);
  });

  it("disables the edges and respects a disabled list", () => {
    const { rerender } = render(
      <Pagination page={1} pages={3} total={30} onPage={() => {}} />,
    );
    expect(screen.getByRole("button", { name: "Previous page" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Next page" })).toBeEnabled();

    rerender(<Pagination page={3} pages={3} total={30} onPage={() => {}} />);
    expect(screen.getByRole("button", { name: "Next page" })).toBeDisabled();

    rerender(
      <Pagination page={2} pages={3} total={30} onPage={() => {}} disabled />,
    );
    expect(screen.getByRole("button", { name: "Previous page" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Next page" })).toBeDisabled();
  });
});
