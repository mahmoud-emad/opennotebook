// What the sources panel takes: which files, how large, and how many links at
// once. Checked here before anything is sent; the server checks the same.

import { UPLOAD_MAX_MB } from "../api";

/** What can be uploaded, in words. */
export const UPLOAD_KINDS = "PDF, Word, PowerPoint, Excel, Markdown, text or CSV";
/** What can be uploaded as a source, by extension: the file picker offers
 * these and nothing else is sent. The server reads the same list. */
export const UPLOAD_EXTS = ["pdf", "docx", "pptx", "xlsx", "md", "markdown", "txt", "csv"];
/** The picker's `accept`, from the same list. */
export const UPLOAD_ACCEPT = ".pdf,.docx,.pptx,.xlsx,.md,.markdown,.txt,.csv";
/** The largest file the server takes, in bytes. */
const UPLOAD_MAX = UPLOAD_MAX_MB * 1024 * 1024;
/** What can be uploaded, as the add box says it. */
export const uploadHint = () => `PDF, Office, Markdown, text or CSV, up to ${UPLOAD_MAX_MB} MB. Or drop them here.`;
/** The most links one Add takes: the server's limit. More stay in the box for
 * the next Add rather than being dropped. */
export const MAX_LINKS = 8;

/** Why a file would be refused, said before it is sent; null sends it. */
export function uploadProblem(name: string, size: number): string | null {
  const dot = name.lastIndexOf(".");
  const ext = dot >= 0 ? name.slice(dot + 1).toLowerCase() : "";
  if (!UPLOAD_EXTS.includes(ext)) return `not a file the studio reads: ${UPLOAD_KINDS}`;
  if (size > UPLOAD_MAX) return `${Math.ceil(size / (1024 * 1024))} MB, over the ${UPLOAD_MAX_MB} MB limit`;
  if (size === 0) return "the file is empty";
  return null;
}
