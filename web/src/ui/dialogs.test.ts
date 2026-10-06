import { describe, expect, it } from "vitest";
import { usd, usdRange, usdRangeSpoken } from "./dialogs";

// Ported from the old app's cost_format_tests.
describe("money", () => {
  it("never reads a paid step as free", () => {
    expect(usd(0)).toBe("$0.00");
    expect(usd(0.00004)).toBe("< $0.0001");
    expect(usd(0.000412)).toBe("$0.00041");
    expect(usd(0.0421)).toBe("$0.042");
    expect(usd(0.72)).toBe("$0.72");
    expect(usd(1.456)).toBe("$1.46");
  });
  it("collapses a range whose ends are close", () => {
    expect(usdRange(0.4, 0.5)).toBe("about $0.45");
    expect(usdRange(0.4, 1.6)).toBe("$0.40 – $1.60");
    expect(usdRangeSpoken(0.4, 1.6)).toBe("between $0.40 and $1.60");
  });
});
