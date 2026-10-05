import { describe, expect, it } from "vitest";
import { uploadProblem, type UploadRules } from "./upload";

// The rules as the server sends them (`GET /api/collections/{cid}/options`).
const rules: UploadRules = {
  extensions: ["pdf", "docx", "pptx", "xlsx", "md", "markdown", "txt", "csv"],
  accept: ".pdf,.docx,.pptx,.xlsx,.md,.markdown,.txt,.csv",
  kinds: "PDF, Word, PowerPoint, Excel, Markdown, text or CSV",
  max_mb: 25,
  max_files: 10,
  max_links: 8,
  hint: "PDF, Office, Markdown, text or CSV, up to 25 MB. Or drop them here.",
  title: "PDF, Word, PowerPoint, Excel, Markdown, text or CSV, up to 25 MB each. Or drop files on this panel.",
};

describe("what the sources panel takes", () => {
  it("refuses a file before it is sent, by the server's rules, and says why", () => {
    expect(uploadProblem("Report.PDF", 1000, rules)).toBeNull();
    expect(uploadProblem("notes.markdown", 10, rules)).toBeNull();
    expect(uploadProblem("photo.jpg", 1000, rules)).toBe(
      "not a file the studio reads: PDF, Word, PowerPoint, Excel, Markdown, text or CSV",
    );
    expect(uploadProblem("no-extension", 1000, rules)).not.toBeNull();
    const big = uploadProblem("big.pdf", 30 * 1024 * 1024, rules)!;
    expect(big).toBe("30 MB, over the 25 MB limit");
    expect(uploadProblem("max.pdf", 25 * 1024 * 1024, rules)).toBeNull();
    expect(uploadProblem("empty.txt", 0, rules)).toBe("the file is empty");
  });

  it("follows the server's limit when it changes", () => {
    expect(uploadProblem("big.pdf", 30 * 1024 * 1024, { ...rules, max_mb: 50 })).toBeNull();
    expect(uploadProblem("a.jpg", 10, { ...rules, extensions: [...rules.extensions, "jpg"] })).toBeNull();
  });

  it("sends everything while the rules are not read yet, for the server to say", () => {
    expect(uploadProblem("photo.jpg", 1000, null)).toBeNull();
  });
});
