// Small pieces several screens draw or do the same way: the line that says
// why a row's action failed, and saving something as a file.

import { Icon } from "./Icon";

/** What went wrong with one row's action (a retry, a rename, a delete), said
 * right under that row rather than in a banner away from it. */
export function RowErr({ text, onDismiss }: { text: string; onDismiss: () => void }) {
  return (
    <div className="opt-err row-err" role="alert">
      <span className="grow">{text}</span>
      <button className="icon-btn" title="Dismiss" aria-label="Dismiss" onClick={onDismiss}>
        <Icon name="x-lg" />
      </button>
    </div>
  );
}

/** A title as a file name: letters, digits, spaces and dashes, at most 60
 * characters; "Mind map" when nothing is left. */
export function fileStem(title: string): string {
  const s = [...title]
    .map((c) => (/[\p{L}\p{N}]/u.test(c) || c === " " || c === "-" ? c : " "))
    .join("")
    .split(/\s+/)
    .filter((w) => w !== "")
    .join(" ");
  const cut = [...s].slice(0, 60).join("");
  return cut === "" ? "Mind map" : cut;
}

/** Save a blob as a file, through a link the browser downloads. */
export function saveBlob(b: Blob, name: string): void {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(b);
  a.download = name;
  document.body.appendChild(a);
  a.click();
  setTimeout(() => {
    URL.revokeObjectURL(a.href);
    a.remove();
  }, 1000);
}

/** Save text as a file, through a link the browser downloads. */
export function download(name: string, mime: string, body: string): void {
  saveBlob(new Blob([body], { type: mime }), name);
}
