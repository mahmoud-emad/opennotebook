// The first screen's budget: what index.html loads before anything else, under
// 150 KB gzipped. Lazy routes are not counted; they load when first used.
import { readFileSync } from "node:fs";
import { gzipSync } from "node:zlib";
import { join } from "node:path";

const BUDGET = 150 * 1024;
const dist = new URL("../dist/", import.meta.url).pathname;
const html = readFileSync(join(dist, "index.html"), "utf8");
const assets = [...html.matchAll(/(?:src|href)="\/ui\/(assets\/[^"]+\.(?:js|css))"/g)].map((m) => m[1]);
let total = 0;
for (const a of assets) {
  const size = gzipSync(readFileSync(join(dist, a))).length;
  total += size;
  console.log(`${(size / 1024).toFixed(1).padStart(7)} KB  ${a}`);
}
console.log(`${(total / 1024).toFixed(1).padStart(7)} KB  first screen, gzipped (budget ${BUDGET / 1024} KB)`);
if (total > BUDGET) {
  console.error("The first screen is over its budget. Split the new code into a lazy route.");
  process.exit(1);
}
