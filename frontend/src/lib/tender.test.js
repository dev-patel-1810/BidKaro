import { describe, expect, it } from "vitest";

import { tenderStatus } from "./tender";

describe("tenderStatus", () => {
  it("reports a terminated bidding cycle before the closing date is read", () => {
    expect(
      tenderStatus({
        cycle_state: "terminated",
        closing_at: "2099-01-01T12:00:00Z",
      }),
    ).toBe("terminated");
  });

  it("still derives closed and open from the closing date", () => {
    expect(tenderStatus({ closing_at: "2000-01-01T12:00:00Z" })).toBe("closed");
    expect(tenderStatus({ closing_at: "2099-01-01T12:00:00Z" })).toBe("open");
  });
});
