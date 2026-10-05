import { describe, expect, it } from "vitest";
import { UPLOAD_ACCEPT, UPLOAD_EXTS, uploadProblem } from "./upload";

// Ported from the old app's collection.rs tests.
describe("what the sources panel takes", () => {
  /** The picker offers exactly what the check lets through. */
  it("offers in the picker exactly what the check lets through", () => {
    expect(UPLOAD_ACCEPT.split(",")).toEqual(UPLOAD_EXTS.map((e) => `.${e}`));
  });

  it("refuses a file before it is sent, and says why", () => {
    expect(uploadProblem("Report.PDF", 1000)).toBeNull();
    expect(uploadProblem("notes.markdown", 10)).toBeNull();
    expect(uploadProblem("photo.jpg", 1000)).toContain("PDF");
    expect(uploadProblem("no-extension", 1000)).not.toBeNull();
    const big = uploadProblem("big.pdf", 30 * 1024 * 1024)!;
    expect(big).toContain("30 MB");
    expect(big).toContain("25 MB");
    expect(uploadProblem("max.pdf", 25 * 1024 * 1024)).toBeNull();
    expect(uploadProblem("empty.txt", 0)).not.toBeNull();
  });
});
