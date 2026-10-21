import { expect, test, type Browser, type BrowserContext, type Page } from "@playwright/test";

// Toasts are also read out by Radix's live region ("Notification …"), so wait for them with exact: true.
// Cross-page flows from docs/design/07-ux.md §5 (fake LLM). Users are created through the API by the CI admin,
// so this file doesn't depend on the smoke spec's data.
const ADMIN = { username: "admin", password: process.env.MWB_E2E_ADMIN_PASSWORD ?? "admin-password-1" };
const PASSWORD = "member-password-1";
const run = Date.now().toString(36);
const CAROL = `carol${run}`;
const DAVE = `dave${run}`;
const WORD = `quokkafire${run}`;
const DOC = `carol-${run}.md`;

test.describe.configure({ mode: "serial" });

let lastPage: Page | undefined;
test.afterEach(async ({}, info) => {
  if (info.status !== info.expectedStatus && lastPage && !lastPage.isClosed()) {
    const text = await lastPage.locator("body").innerText().catch(() => "(page text unavailable)");
    console.log(`--- page at failure (${lastPage.url()}) ---\n${text.slice(0, 3000)}`);
  }
});

async function adminApi(browser: Browser) {
  const ctx = await browser.newContext();
  const login = await ctx.request.post("/api/auth/login", { data: ADMIN });
  expect(login.ok()).toBeTruthy();
  const csrf = (await login.json()).csrf_token as string;
  return { ctx, headers: { "X-CSRF-Token": csrf } };
}

/** An admin-created member with their one-time password (07 §5.12 step 1). */
async function createMember(browser: Browser, username: string): Promise<string> {
  const { ctx, headers } = await adminApi(browser);
  const r = await ctx.request.post("/api/admin/users", { data: { username }, headers });
  expect(r.ok()).toBeTruthy();
  const password = (await r.json()).initial_password as string;
  await ctx.close();
  return password;
}

async function open(browser: Browser, permissions: string[] = []): Promise<{ ctx: BrowserContext; page: Page }> {
  const ctx = await browser.newContext({ permissions });
  const page = await ctx.newPage();
  lastPage = page;
  return { ctx, page };
}

async function signIn(page: Page, username: string, password = PASSWORD) {
  await page.getByLabel("Username").fill(username);
  await page.getByLabel("Password").fill(password);
  await page.getByRole("button", { name: "Sign in" }).click();
}

async function signedIn(browser: Browser, username: string): Promise<Page> {
  const { page } = await open(browser);
  await page.goto("/login");
  await signIn(page, username, username === ADMIN.username ? ADMIN.password : PASSWORD);
  await expect(page.locator(".shell")).toBeAttached();
  return page;
}

function step(page: Page, label: string) {
  return page.getByRole("region", { name: "Getting started" }).getByRole("listitem").filter({ hasText: label });
}

test("5.12 + 5.1: a new member changes the one-time password and works through Getting started", async ({ browser }) => {
  const oneTime = await createMember(browser, CAROL);
  await createMember(browser, DAVE).then(async (pw) => {
    // Dave only needs a working password for the sharing flow.
    const { page } = await open(browser);
    await page.goto("/login");
    await signIn(page, DAVE, pw);
    await page.getByLabel("One-time password").fill(pw);
    await page.getByLabel("New password (10+ characters)").fill(PASSWORD);
    await page.getByLabel("Repeat new password").fill(PASSWORD);
    await page.getByRole("button", { name: "Save and continue" }).click();
    await expect(page.locator(".shell")).toBeAttached();
    await page.context().close();
  });

  const { page } = await open(browser);
  await page.goto("/");
  await expect(page).toHaveURL(/\/login\?next=%2F$/);
  await signIn(page, CAROL, oneTime);
  await expect(page.getByRole("heading", { name: "Choose your password" })).toBeVisible();
  await page.getByLabel("One-time password").fill(oneTime);
  await page.getByLabel("New password (10+ characters)").fill(PASSWORD);
  await page.getByLabel("Repeat new password").fill(PASSWORD);
  await page.getByRole("button", { name: "Save and continue" }).click();

  // Home with the checklist instead of an empty Recent section.
  const checklist = page.getByRole("region", { name: "Getting started" });
  await expect(checklist).toBeVisible();
  await expect(page.getByText("Nothing needs you right now.")).toBeVisible();

  await checklist.getByRole("link", { name: "Confirm your timezone" }).click();
  await expect(page).toHaveURL(/\/settings\/profile\?from=getting-started$/);
  await expect(page.getByRole("link", { name: "Back to getting started" })).toBeVisible();
  await page.getByRole("button", { name: /^Use / }).click();
  await expect(page.getByText("Profile saved", { exact: true })).toBeVisible();
  await page.getByRole("link", { name: "Back to getting started" }).click();
  await expect(step(page, "Confirm your timezone")).toHaveAttribute("data-done", "true");

  await checklist.getByRole("link", { name: "Add your GitLab, Gerrit or Jira token" }).click();
  await page.getByLabel("GitLab personal access token").fill("glpat-e2e-not-real");
  await page.getByRole("listitem").filter({ hasText: "GitLab personal access token" }).getByRole("button", { name: "Save" }).click();
  await expect(page.getByText("GitLab personal access token saved", { exact: true })).toBeVisible();
  await page.goto("/");
  await expect(step(page, "Add your GitLab, Gerrit or Jira token")).toHaveAttribute("data-done", "true");

  // 5.2 step 1: `c` opens a new chat with the composer focused.
  await page.locator("body").click({ position: { x: 5, y: 5 } });
  await page.keyboard.press("c");
  await expect(page).toHaveURL(/\/chat\/[0-9a-f-]{36}$/);
  await expect(page.getByLabel("Message")).toBeFocused();
  await page.getByLabel("Message").fill("What should I work on?");
  await page.getByLabel("Message").press("Enter");
  await expect(page.getByText("Here is what I found in your knowledge base")).toBeVisible();
  await page.goto("/");
  await expect(step(page, "Ask your first question")).toHaveAttribute("data-done", "true");
  await expect(page.getByRole("region", { name: "Recent" }).getByRole("link").first()).toBeVisible();

  await page.getByRole("region", { name: "Getting started" }).getByRole("button", { name: "Dismiss" }).click();
  await expect(page.getByRole("region", { name: "Getting started" })).toBeHidden();
  await page.reload();
  await expect(page.getByText("Nothing needs you right now.")).toBeVisible();
  await expect(page.getByRole("region", { name: "Getting started" })).toBeHidden();
});

test("5.2: the unsent chat message survives leaving the page", async ({ browser }) => {
  const page = await signedIn(browser, CAROL);
  await page.goto("/");
  await page.getByRole("region", { name: "Recent" }).getByRole("link").first().click();
  await page.getByLabel("Message").fill("a question I haven't finished");
  await page.getByRole("navigation", { name: "Primary" }).getByRole("link", { name: "Search" }).click();
  await expect(page.getByLabel("Search query")).toBeVisible(); // the chat is gone, not just covered
  await page.goBack();
  await expect(page.getByLabel("Message")).toHaveValue("a question I haven't finished");
  await expect(page.getByText("Restored your unsaved text", { exact: true })).toBeVisible();
});

test("5.3: adding a repository without a token, and a failed ingest reported where it's fixed", async ({ browser }) => {
  const page = await signedIn(browser, CAROL);
  await page.goto("/");
  await page.getByRole("link", { name: "Add repository" }).click();
  const dialog = page.getByRole("dialog", { name: "Add repository" });
  await dialog.getByLabel("Host").selectOption("gerrit");
  await dialog.getByLabel("Repository URL").fill(`https://gerrit.invalid/${run}/svc`);
  await expect(dialog.getByText(/You need a Gerrit username and HTTP password first/)).toBeVisible();
  await expect(dialog.getByRole("button", { name: "Add repository" })).toBeDisabled();
  await dialog.getByRole("link", { name: "Add token" }).click();

  await expect(page).toHaveURL(/\/settings\/integrations\?focus=gerrit_username/);
  await expect(page.getByLabel("Gerrit username")).toBeFocused();
  for (const [label, value] of [
    ["Gerrit username", "carol"],
    ["Gerrit HTTP password", "not-a-real-password"],
  ]) {
    await page.getByLabel(label).fill(value);
    await page.getByRole("listitem").filter({ hasText: label }).getByRole("button", { name: "Save" }).click();
    await expect(page.getByText(`${label} saved`, { exact: true })).toBeVisible();
  }
  await page.getByRole("link", { name: "Back to Add repository" }).click();
  const again = page.getByRole("dialog", { name: "Add repository" });
  await expect(again.getByLabel("Repository URL")).toHaveValue(`https://gerrit.invalid/${run}/svc`);
  await expect(again.getByLabel("Host")).toHaveValue("gerrit");
  await again.getByRole("button", { name: "Add repository" }).click();
  await expect(page.getByText("Indexing started", { exact: true })).toBeVisible();

  // The host doesn't exist, so the ingest fails; Home says so with the same message and a way to fix it.
  await page.goto("/");
  const card = page.getByRole("region", { name: "Needs you" }).getByRole("listitem").filter({ hasText: "Indexing svc failed" });
  await expect(async () => {
    await page.reload();
    await expect(card).toBeVisible({ timeout: 2000 });
  }).toPass({ timeout: 90_000 });
  await card.getByRole("link", { name: "Open repository" }).click();
  await expect(page.getByRole("heading", { name: "svc", level: 1 })).toBeVisible();
  await expect(page.getByText(/^Indexing svc failed:/)).toBeVisible();
  await expect(page.getByRole("button", { name: "Retry" })).toBeEnabled();
});

test("5.4: share asks first, offers Undo, and an admin can take it down", async ({ browser }) => {
  const carol = await signedIn(browser, CAROL);
  await carol.goto("/sources/documents");
  await carol.getByLabel("Upload files").setInputFiles({
    name: DOC,
    mimeType: "text/markdown",
    buffer: Buffer.from(`# Carol\n\nThe ${WORD} cache warms up at 06:00.\n`),
  });
  const row = carol.getByRole("row", { name: new RegExp(DOC) });
  await row.getByRole("button", { name: "Share with team" }).click();
  const confirm = carol.getByRole("dialog", { name: `Share ${DOC} with the team?` });
  await expect(confirm).toContainText("becomes searchable by everyone on the team");
  await confirm.getByRole("button", { name: "Share with team" }).click();
  await expect(row.getByText("Team", { exact: true })).toBeVisible();
  await carol.getByRole("button", { name: "Undo" }).click();
  await expect(row.getByText("Private", { exact: true })).toBeVisible();

  await row.getByRole("button", { name: "Share with team" }).click();
  await carol.getByRole("dialog", { name: `Share ${DOC} with the team?` }).getByRole("button", { name: "Share with team" }).click();
  await expect(row.getByRole("button", { name: "Make private" })).toBeVisible();

  const dave = await signedIn(browser, DAVE);
  await dave.goto(`/search?q=${WORD}`);
  await expect(dave.getByRole("link", { name: DOC })).toBeVisible();

  const admin = await signedIn(browser, ADMIN.username);
  await admin.goto("/sources/documents");
  const adminRow = admin.getByRole("row", { name: new RegExp(DOC) });
  await adminRow.getByRole("button", { name: "Remove from team space" }).click();
  await expect(adminRow).toBeHidden(); // private again, so it leaves the admin's list too
  await dave.goto(`/search?q=${WORD}`);
  await expect(dave.getByText(/No matches/)).toBeVisible();
});

test("5.5: wiki save, history and an edit conflict between two tabs", async ({ browser }) => {
  const { ctx, page: a } = await open(browser, ["clipboard-read", "clipboard-write"]);
  await a.goto("/login");
  await signIn(a, CAROL);
  await expect(a.locator(".shell")).toBeAttached();
  await a.goto("/wiki");
  await a.getByRole("button", { name: "New page" }).click();
  await a.getByLabel("Path").fill(`notes/flows-${run}.md`);
  await a.getByLabel("Content (Markdown)").fill("# Flows\n\nfirst\n");
  await a.getByRole("button", { name: "Create page" }).click();
  await expect(a.locator(".page-header h1", { hasText: "Flows" })).toBeVisible();

  const b = await ctx.newPage();
  await b.goto(a.url());
  await a.getByRole("button", { name: "Edit" }).click();
  await b.getByRole("button", { name: "Edit" }).click();
  await a.getByLabel("Page content").fill("# Flows\n\nfrom tab A\n");
  await a.getByRole("button", { name: "Save", exact: true }).click();
  await expect(a.getByText("Saved", { exact: true })).toBeVisible();

  await b.getByLabel("Page content").fill("# Flows\n\nfrom tab B\n");
  await b.getByRole("button", { name: "Save", exact: true }).click();
  const conflict = b.getByRole("alert", { name: "Edit conflict" });
  await expect(conflict).toContainText("from tab A");
  await conflict.getByRole("button", { name: "Copy my text and reload" }).click();
  await expect(b.getByText("Your text is on the clipboard")).toBeVisible();
  expect(await b.evaluate(() => navigator.clipboard.readText())).toBe("# Flows\n\nfrom tab B\n");
  await expect(b.locator(".md", { hasText: "from tab A" })).toBeVisible();

  await a.getByRole("button", { name: "History" }).click();
  await expect(a.locator("ul.commits li")).toHaveCount(2);
});

test("5.11: an expired session keeps the unsaved edit", async ({ browser }) => {
  const { ctx, page } = await open(browser);
  await page.goto("/login");
  await signIn(page, CAROL);
  await expect(page.locator(".shell")).toBeAttached();
  await page.goto("/wiki");
  await page.getByRole("link", { name: `notes/flows-${run}.md` }).click();
  const url = page.url();
  await page.getByRole("button", { name: "Edit" }).click();
  await page.getByLabel("Page content").fill("# Flows\n\nwritten while the session expired\n");
  await ctx.clearCookies(); // what an expired or revoked session looks like to the browser
  await page.getByRole("button", { name: "History" }).click(); // the next API call gets 401
  await expect(page).toHaveURL(/\/login\?next=.*&reason=expired$/);
  await expect(page.getByText(/Your session expired/)).toBeVisible();
  await signIn(page, CAROL);
  await expect(page).toHaveURL(url);
  await expect(page.getByLabel("Page content")).toHaveValue("# Flows\n\nwritten while the session expired\n");
  await expect(page.getByText("Restored your unsaved text", { exact: true })).toBeVisible();
});

test("5.12: an admin removes a user after seeing what happens, and the audit log shows it", async ({ browser }) => {
  const admin = await signedIn(browser, "admin");
  await admin.goto("/admin");
  await admin.getByRole("row", { name: DAVE }).getByRole("button", { name: "Delete" }).click();
  const dialog = admin.getByRole("dialog", { name: `Delete ${DAVE}` });
  await expect(dialog.getByText(/^Deleted: /)).toBeVisible();
  await expect(dialog.getByRole("button", { name: "Delete user" })).toBeDisabled();
  await dialog.getByLabel(`Type ${DAVE} to confirm`).fill(DAVE);
  await dialog.getByRole("button", { name: "Delete user" }).click();
  await expect(admin.getByText(`${DAVE} deleted`, { exact: true })).toBeVisible();
  await admin.goto("/admin/system");
  await expect(admin.getByRole("table", { name: "Audit log" }).getByRole("row").filter({ hasText: "user.delete" }).first()).toBeVisible();
});
