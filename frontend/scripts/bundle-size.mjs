// Fails when the JavaScript the first page load needs is over budget (07 §8: ≤ 250 KB gzipped).
// "Initial" = the entry script plus everything index.html preloads; lazy route chunks are excluded.
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { gzipSync } from "node:zlib";

const BUDGET = Number(process.env.MWB_BUNDLE_BUDGET_KB ?? 250) * 1024;
const dist = new URL("../dist/", import.meta.url).pathname;
const html = readFileSync(join(dist, "index.html"), "utf8");
const files = [
  ...html.matchAll(/<script[^>]+type="module"[^>]+src="\/([^"]+\.js)"/g),
  ...html.matchAll(/<link[^>]+rel="modulepreload"[^>]+href="\/([^"]+\.js)"/g),
].map((m) => m[1]);
if (!files.length) throw new Error("no entry script found in dist/index.html");

let total = 0;
for (const f of new Set(files)) {
  const gz = gzipSync(readFileSync(join(dist, f)), { level: 9 }).length;
  total += gz;
  console.log(`${(gz / 1024).toFixed(1).padStart(7)} KB  ${f}`);
}
console.log(`${(total / 1024).toFixed(1).padStart(7)} KB  initial JS, gzipped (budget ${BUDGET / 1024} KB)`);
if (total > BUDGET) {
  console.error("Initial JavaScript is over budget: lazy-load the new dependency or route.");
  process.exit(1);
}
