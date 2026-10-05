// What the sources panel takes, as the server says it (`UploadRules`): a file
// it would refuse is said at once, before it is sent. The server checks the
// same and words its own refusal for anything that gets past.

import type { StudioOptions } from "../api-studio";

export type UploadRules = StudioOptions["upload"];

/** Why a file would be refused, said before it is sent; null sends it. With
 * no rules yet read, everything is sent and the server says. */
export function uploadProblem(name: string, size: number, rules: UploadRules | null): string | null {
  if (rules === null) return null;
  const dot = name.lastIndexOf(".");
  const ext = dot >= 0 ? name.slice(dot + 1).toLowerCase() : "";
  if (!rules.extensions.includes(ext)) return `not a file the studio reads: ${rules.kinds}`;
  if (size > rules.max_mb * 1024 * 1024)
    return `${Math.ceil(size / (1024 * 1024))} MB, over the ${rules.max_mb} MB limit`;
  if (size === 0) return "the file is empty";
  return null;
}
