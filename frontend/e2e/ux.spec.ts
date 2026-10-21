import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";

// Design system checks from docs/design/07-ux.md §7–§8: no serious or critical axe violations on any page in
// either theme, the responsive shell, keyboard shortcuts, and visual baselines of the component gallery.
// Signs in as the CI admin only, so it doesn't depend on the smoke spec's data.
const ADMIN = { username: "admin", password: process.env.MWB_E2E_ADMIN_PASSWORD ?? "admin-password-1" };

const PAGES = [
  ["home", "/"],
  ["chat", "/chat"],
  ["search", "/search?q=runbook"],
  ["wiki", "/wiki"],
  ["repositories", "/sources/repos"],
  ["documents", "/sources/documents"],
  ["imports", "/sources/imports"],
  ["settings: profile", "/settings/profile"],
  ["settings: integrations", "/settings/integrations"],
  ["settings: usage", "/settings/usage"],
  ["settings: sessions", "/settings/sessions"],
  ["settings: password", "/settings/password"],
  ["librarian changes", "/wiki/changes"],
  ["users", "/admin"],
  ["integrations", "/admin/integrations"],
  ["system", "/admin/system"],
  ["component gallery", "/dev/components"],
] as const;

async function signIn(page: Page) {
  await page.goto("/login");
  await page.getByLabel("Username").fill(ADMIN.username);
  await page.getByLabel("Password").fill(ADMIN.password);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page).not.toHaveURL(/\/login/);
  await expect(page.locator(".shell")).toBeAttached();
}

/** Wait for the route chunk, data and fonts, so the checks see the finished page. */
async function settle(page: Page) {
  await page.waitForLoadState("networkidle");
  // The gallery shows skeletons on purpose; anywhere else one means data is still loading.
  await expect
    .poll(() => page.evaluate(() => document.querySelectorAll(".skeleton:not(.gallery .skeleton)").length))
    .toBe(0);
  await page.evaluate(() => document.fonts.ready);
}

async function axe(page: Page) {
  const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).analyze();
  const bad = results.violations.filter((v) => v.impact === "serious" || v.impact === "critical");
  return bad.map((v) => `${v.id} (${v.impact}): ${v.help}\n  ${v.nodes.map((n) => n.target.join(" ")).slice(0, 5).join("\n  ")}`);
}

for (const scheme of ["light", "dark"] as const) {
  test.describe(`accessibility, ${scheme}`, () => {
    test.use({ colorScheme: scheme });

    test("sign-in page", async ({ page }) => {
      await page.goto("/login");
      await settle(page);
      expect(await axe(page)).toEqual([]);
    });

    test("every signed-in page", async ({ page }) => {
      await signIn(page);
      const problems: string[] = [];
      for (const [name, path] of PAGES) {
        await page.goto(path);
        await settle(page);
        for (const p of await axe(page)) problems.push(`${name}: ${p}`);
      }
      await page.goto("/wiki");
      await page.getByRole("button", { name: "New page" }).click();
      await expect(page.getByLabel("Path")).toBeVisible();
      for (const p of await axe(page)) problems.push(`new wiki page: ${p}`);
      expect(problems).toEqual([]);
    });

    test("dialogs and drawers", async ({ page }) => {
      await signIn(page);
      await page.goto("/dev/components");
      await settle(page);
      await page.getByRole("button", { name: "Open confirmation" }).click();
      await expect(page.getByRole("dialog", { name: "Delete payments-api?" })).toBeVisible();
      expect(await axe(page)).toEqual([]);
      await page.keyboard.press("Escape");
      await page.getByRole("button", { name: "Open drawer" }).click();
      await expect(page.getByRole("dialog", { name: "Example drawer" })).toBeVisible();
      expect(await axe(page)).toEqual([]);
    });
  });
}

test.describe("shell", () => {
  test("keyboard shortcuts", async ({ page }) => {
    await signIn(page);
    await page.goto("/chat");
    await settle(page);
    await page.keyboard.press("/");
    await expect(page.getByRole("searchbox", { name: "Search everything" })).toBeFocused();
    await page.keyboard.type("runbook");
    await page.keyboard.press("Enter");
    await expect(page).toHaveURL(/\/search\?q=runbook$/);
    await expect(page.getByLabel("Search query")).toHaveValue("runbook");
    await page.locator("body").click({ position: { x: 5, y: 5 } });
    await page.keyboard.press("g");
    await page.keyboard.press("w");
    await expect(page).toHaveURL(/\/wiki$/);
    await page.keyboard.press("?");
    const help = page.getByRole("dialog", { name: "Keyboard shortcuts" });
    await expect(help).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(help).toBeHidden();
  });

  test("skip link moves focus to the content", async ({ page }) => {
    await signIn(page);
    await page.goto("/settings");
    await settle(page);
    await page.keyboard.press("Tab");
    const skip = page.getByRole("link", { name: "Skip to content" });
    await expect(skip).toBeFocused();
    await page.keyboard.press("Enter");
    await expect(page.locator("#main")).toBeFocused();
  });

  test("phones get a tab bar and a More sheet", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await signIn(page);
    await page.goto("/chat");
    await settle(page);
    const tabs = page.getByRole("navigation", { name: "Tabs" });
    await expect(tabs).toBeVisible();
    await expect(page.getByRole("navigation", { name: "Primary" })).toBeHidden();
    await tabs.getByRole("button", { name: "More" }).click();
    const more = page.getByRole("dialog", { name: "More" });
    await more.getByRole("link", { name: "Settings" }).click();
    await expect(page).toHaveURL(/\/settings\/profile$/);
    await expect(more).toBeHidden();
  });

  test("tablets get the icon sidebar with tooltips", async ({ page }) => {
    await page.setViewportSize({ width: 1000, height: 800 });
    await signIn(page);
    await page.goto("/chat");
    await settle(page);
    const wiki = page.getByRole("navigation", { name: "Primary" }).getByRole("link", { name: "Wiki" });
    await expect(wiki).toBeVisible();
    expect((await wiki.boundingBox())!.width).toBeLessThanOrEqual(48); // icon only, name kept for screen readers
    await wiki.hover();
    await expect(page.getByRole("tooltip", { name: "Wiki" })).toBeVisible();
  });
});

// Baselines live next to this file (ux.spec.ts-snapshots/). CI writes missing ones and fails, uploading them as
// the `visual-baselines` artifact to review and commit (docs/design/07-ux.md §8).
test.describe("visual baselines", () => {
  for (const scheme of ["light", "dark"] as const) {
    for (const [device, width, height] of [
      ["desktop", 1280, 900],
      ["phone", 390, 844],
    ] as const) {
      test(`component gallery, ${scheme}, ${device}`, async ({ page }) => {
        await page.emulateMedia({ colorScheme: scheme, reducedMotion: "reduce" });
        await page.setViewportSize({ width, height });
        await signIn(page);
        await page.goto("/dev/components");
        await settle(page);
        // The page scrolls inside <main>, which fullPage can't see: grow the viewport to fit it instead.
        const contentHeight = await page.evaluate(() => {
          const main = document.querySelector("#main")!;
          return main.scrollHeight + (window.innerHeight - main.clientHeight);
        });
        await page.setViewportSize({ width, height: Math.min(contentHeight, 8000) });
        await expect(page).toHaveScreenshot(`gallery-${scheme}-${device}.png`, {
          animations: "disabled",
          caret: "hide",
          maxDiffPixelRatio: 0.02,
          // The signed-in name and timezone come from the CI admin, not the design.
          mask: [page.locator(".who")],
        });
      });

      test(`sign-in page, ${scheme}, ${device}`, async ({ page }) => {
        await page.emulateMedia({ colorScheme: scheme, reducedMotion: "reduce" });
        await page.setViewportSize({ width, height });
        await page.goto("/login");
        await settle(page);
        await expect(page).toHaveScreenshot(`login-${scheme}-${device}.png`, {
          animations: "disabled",
          caret: "hide",
          maxDiffPixelRatio: 0.02,
        });
      });
    }
  }
});
