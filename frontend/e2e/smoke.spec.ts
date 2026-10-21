import { expect, test, type Browser, type Page } from "@playwright/test";

// Smoke flows from docs/design/01-hardening.md §H2. Needs an admin created by the CI workflow and the fake LLM.
const ADMIN = { username: "admin", password: process.env.MWB_E2E_ADMIN_PASSWORD ?? "admin-password-1" };
const PASSWORD = "member-password-1";
const run = Date.now().toString(36);
const ALICE = `alice${run}`;
const BOB = `bob${run}`;
const WORD = `zebracorn${run}`;
const DOC = `notes-${run}.md`;

let lastPage: Page | undefined;
const cspViolations: string[] = [];

// On failure, print what the user was looking at; CI turns this into a check annotation.
test.afterEach(async () => {
  // The strict CSP (H3) must never block our own app.
  expect(cspViolations, "CSP violations").toEqual([]);
});

test.afterEach(async ({}, testInfo) => {
  if (testInfo.status !== testInfo.expectedStatus && lastPage && !lastPage.isClosed()) {
    const text = await lastPage.locator("body").innerText().catch(() => "(page text unavailable)");
    console.log(`--- page at failure (${lastPage.url()}) ---\n${text.slice(0, 3000)}`);
  }
});

async function signIn(browser: Browser, username: string, password = PASSWORD): Promise<Page> {
  const page = await (await browser.newContext()).newPage();
  lastPage = page;
  // Browser-side events, printed in CI output to explain failures.
  page.on("requestfailed", (r) => console.log(`[${username}] request failed: ${r.method()} ${r.url()} ${r.failure()?.errorText}`));
  page.on("framenavigated", (f) => f === page.mainFrame() && console.log(`[${username}] navigated: ${f.url()}`));
  page.on("console", (m) => {
    if (m.type() !== "error") return;
    console.log(`[${username}] console error: ${m.text()}`);
    if (m.text().includes("Content Security Policy")) cspViolations.push(m.text());
  });
  page.on("pageerror", (e) => console.log(`[${username}] page error: ${e.message}\n${(e.stack ?? "").split("\n").slice(0, 6).join("\n")}`));
  await page.goto("/login");
  await page.getByLabel("Username").fill(username);
  await page.getByLabel("Password").fill(password);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("button", { name: "Sign out" })).toBeVisible();
  return page;
}

async function search(page: Page, q: string) {
  await page.goto("/search");
  await page.getByLabel("Search query").fill(q);
  await page.getByRole("button", { name: "Search", exact: true }).click();
}

test.describe.configure({ mode: "serial" });

test("1. admin creates two members", async ({ browser }) => {
  const admin = await signIn(browser, ADMIN.username, ADMIN.password);
  await admin.goto("/admin");
  for (const name of [ALICE, BOB]) {
    await admin.getByLabel("Username").fill(name);
    await admin.getByLabel("Initial password").fill(PASSWORD);
    await admin.getByRole("button", { name: "Create user" }).click();
    await expect(admin.getByRole("cell", { name, exact: true }).first()).toBeVisible();
  }
});

test("2. uploads are private until shared", async ({ browser }) => {
  const alice = await signIn(browser, ALICE);
  await alice.goto("/documents");
  await alice.getByLabel("Upload files").setInputFiles({
    name: DOC,
    mimeType: "text/markdown",
    buffer: Buffer.from(`# Notes\n\nThe ${WORD} service restarts every night after the backup job.\n`),
  });
  const row = alice.getByRole("row", { name: new RegExp(DOC) });
  await expect(row).toBeVisible();

  await search(alice, WORD);
  await expect(alice.getByRole("link", { name: DOC })).toBeVisible();

  const bob = await signIn(browser, BOB);
  await search(bob, WORD);
  await expect(bob.getByText("No matches")).toBeVisible();

  await alice.goto("/documents");
  const shared = alice.getByRole("row", { name: new RegExp(DOC) });
  await shared.getByRole("button", { name: "Share with team" }).click();
  await expect(shared.getByText("Team")).toBeVisible();
  await search(bob, WORD);
  try {
    await expect(bob.getByRole("link", { name: DOC })).toBeVisible();
  } catch (err) {
    // Diagnostics for an intermittent CI failure: is it the API's answer or the page?
    const url = `/api/knowledge/search?${new URLSearchParams({ q: WORD, scope: "all", limit: "20" })}`;
    for (const delay of [0, 3000]) {
      await bob.waitForTimeout(delay);
      const r = await bob.request.get(url);
      console.log(`[diag] bob search after ${delay} ms: ${r.status()} ${(await r.text()).slice(0, 500)}`);
    }
    const docs = await bob.request.get("/api/knowledge/documents");
    console.log(`[diag] bob documents: ${docs.status()} ${(await docs.text()).slice(0, 500)}`);
    throw err;
  }
});

test("3. chat streams an answer with a citation that opens the source", async ({ browser }) => {
  const alice = await signIn(browser, ALICE);
  await alice.goto("/chat");
  await alice.getByRole("button", { name: "New chat" }).click();
  await alice.getByLabel("Message").fill(`What do I know about ${WORD}?`);
  await alice.getByLabel("Message").press("Enter");
  await expect(alice.getByText("Here is what I found in your knowledge base")).toBeVisible();
  const chip = alice.locator("a.citation", { hasText: DOC }).first();
  await expect(chip).toBeVisible();
  await chip.click();
  await expect(alice).toHaveURL(/\/documents$/);
});

test("4. wiki page create, edit, history and diff", async ({ browser }) => {
  const alice = await signIn(browser, ALICE);
  await alice.goto("/wiki");
  await alice.getByRole("button", { name: "New page" }).click();
  await alice.getByLabel("Path").fill(`notes/e2e-${run}.md`);
  await alice.getByLabel("Content (Markdown)").fill("# E2E page\n\nfirst version\n");
  await alice.getByRole("button", { name: "Create page" }).click();
  await expect(alice.locator(".page-header h1", { hasText: "E2E page" })).toBeVisible();

  await alice.getByRole("button", { name: "Edit" }).click();
  await alice.getByLabel("Page content").fill("# E2E page\n\nsecond version\n");
  await alice.getByRole("button", { name: "Save" }).click();
  // Wait for the save to finish (the editor closes); the text is visible inside the editor before that.
  await expect(alice.getByLabel("Page content")).toBeHidden();
  await expect(alice.locator(".md", { hasText: "second version" })).toBeVisible();

  await alice.getByRole("button", { name: "History" }).click();
  const commits = alice.locator("ul.commits li button");
  await expect(commits).toHaveCount(2);
  await commits.first().click();
  await expect(alice.locator(".diff .add", { hasText: "second version" })).toBeVisible();
});

test("5. changing the password signs the user out", async ({ browser }) => {
  const alice = await signIn(browser, ALICE);
  await alice.goto("/settings");
  await alice.getByLabel("Current password").fill(PASSWORD);
  await alice.getByLabel("New password (10+ characters)").fill("member-password-2");
  await alice.getByRole("button", { name: "Change password" }).click();
  await expect(alice).toHaveURL(/\/login$/);
  await signIn(browser, ALICE, "member-password-2");
});
