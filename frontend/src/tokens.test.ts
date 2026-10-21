import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

/** Colour tokens must meet WCAG AA in both themes (07 §7): 4.5:1 for text, 3:1 for large text and UI parts. */

// Read from disk: Vitest replaces CSS imports (even ?raw) with empty modules. Tests run from frontend/.
const css = readFileSync(resolve(process.cwd(), "src/styles.css"), "utf8");

function block(selector: string): Record<string, string> {
  const start = css.indexOf(selector);
  if (start < 0) throw new Error(`no ${selector} block`);
  const open = css.indexOf("{", start);
  const close = css.indexOf("}", open);
  const vars: Record<string, string> = {};
  for (const m of css.slice(open + 1, close).matchAll(/--([\w-]+):\s*([^;]+);/g)) vars[m[1]] = m[2].trim();
  return vars;
}

const light = block(":root {");
const darkMedia = { ...light, ...block(':root:not([data-theme="light"])') };
const darkForced = { ...light, ...block(':root[data-theme="dark"]') };

function luminance(hex: string): number {
  const v = hex.replace("#", "");
  const [r, g, b] = [0, 2, 4].map((i) => parseInt(v.slice(i, i + 2), 16) / 255);
  const lin = (c: number) => (c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4);
  return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b);
}
function contrast(a: string, b: string): number {
  const [x, y] = [luminance(a), luminance(b)].sort((p, q) => q - p);
  return (x + 0.05) / (y + 0.05);
}

/** [foreground, background, minimum ratio] */
const TEXT: [string, string, number][] = [
  ["fg", "bg", 4.5],
  ["fg", "surface", 4.5],
  ["fg", "surface-2", 4.5],
  ["muted", "surface", 4.5],
  ["muted", "bg", 4.5],
  ["accent", "surface", 4.5],
  ["accent", "bg", 4.5],
  ["accent-fg", "accent", 4.5],
  ["fg", "accent-soft", 4.5],
  ["danger", "surface", 4.5],
  ["danger", "danger-soft", 4.5],
  ["team", "team-soft", 4.5],
  ["private", "private-soft", 4.5],
  ["fg", "code-bg", 4.5],
  ["fg", "add", 4.5],
  ["fg", "del", 4.5],
  ["sidebar-fg", "sidebar", 4.5],
  ["st-waiting", "st-waiting-bg", 4.5],
  ["st-working", "st-working-bg", 4.5],
  ["st-needs", "st-needs-bg", 4.5],
  ["st-done", "st-done-bg", 4.5],
  ["st-problem", "st-problem-bg", 4.5],
  ["st-off", "surface", 4.5],
  // Input borders are UI components: 3:1.
  ["line-strong", "surface", 3],
];

describe.each([
  ["light", light],
  ["dark (system)", darkMedia],
  ["dark (forced)", darkForced],
])("%s theme", (_, theme) => {
  it.each(TEXT)("%s on %s ≥ %s:1", (fg, bg, min) => {
    expect(theme[fg], fg).toMatch(/^#[0-9a-f]{6}$/i);
    expect(theme[bg], bg).toMatch(/^#[0-9a-f]{6}$/i);
    expect(contrast(theme[fg], theme[bg])).toBeGreaterThanOrEqual(min);
  });
});

it("keeps the two dark blocks identical", () => {
  expect(block(':root[data-theme="dark"]')).toEqual(block(':root:not([data-theme="light"])'));
});

it("computes contrast like WCAG", () => {
  expect(contrast("#000000", "#ffffff")).toBeCloseTo(21, 1);
  expect(contrast("#777777", "#ffffff")).toBeCloseTo(4.48, 1);
});
