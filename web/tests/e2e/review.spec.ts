import { readFileSync } from "node:fs";
import path from "node:path";
import { expect, test, type Page } from "@playwright/test";

const FIXTURES = path.resolve(__dirname, "../../../tests/fixtures/synthetic/pair_002");
const API = process.env.E2E_API_URL ?? "http://127.0.0.1:8011";
const EMAIL = "e2e@example.com";
const PIN = "246810";

async function upload(page: Page, file: string, type: "invoice" | "purchase_order"): Promise<string> {
  await page.goto("/upload");
  await page.getByLabel("Document type").selectOption(type);
  await page.getByLabel(/PDF, born-digital or scanned/).setInputFiles(path.join(FIXTURES, file));
  await page.getByRole("button", { name: "Upload" }).click();
  await page.waitForURL(/\/documents\/[0-9a-f-]{36}$/);
  return page.url().split("/").at(-1)!;
}

async function signIn(page: Page, pin = PIN): Promise<void> {
  await page.goto("/login");
  await page.getByLabel("Organisation").fill("default");
  await page.getByLabel("Email").fill(EMAIL);
  await page.getByLabel("PIN").fill(pin);
  await page.getByRole("button", { name: "Sign in" }).click();
}

test("without a session every page sends you to sign in, and a wrong PIN is refused", async ({ page, request }) => {
  await page.goto("/evals");
  await expect(page).toHaveURL(/\/login\?next=%2Fevals$/);
  await signIn(page, "000000");
  await expect(page.getByRole("form", { name: "Sign in" }).getByRole("alert")).toHaveText(
    "The organisation, email or PIN is not right.",
  );
  expect((await request.get("/api/v1/review/queue")).status()).toBe(401);
  expect((await request.get(`${API}/v1/review/queue`)).status()).toBe(401);
});

test.describe.serial("a flagged invoice is resolved end to end", () => {
  let invoiceId = "";

  test.beforeEach(async ({ page }) => {
    await signIn(page);
    await expect(page.getByRole("heading", { name: "Review queue" })).toBeVisible();
  });

  test("the order and the invoice are uploaded and processed", async ({ page }) => {
    await upload(page, "purchase_order.pdf", "purchase_order");
    await expect(page.getByText("It can be approved")).toBeVisible({ timeout: 60_000 });

    invoiceId = await upload(page, "invoice.pdf", "invoice");
    await expect(page.getByText("Needs a person")).toBeVisible({ timeout: 60_000 });
    await expect(page.getByText("order: match")).toBeVisible();
    // Indexed for search and chat after the extraction: the page says so as it happens.
    await expect(page.locator(".timeline-compact")).toContainText("Ready to chat", { timeout: 60_000 });
  });

  test("the invoice waits in the review queue with its reason", async ({ page }) => {
    await page.goto("/");
    const row = page.getByRole("row", { name: /invoice\.pdf/ });
    await expect(row).toBeVisible();
    await expect(row).toContainText("not found in the source text");
  });

  test("the doubtful value is shown on the page, confirmed, and the document approved", async ({ page }) => {
    await page.goto(`/documents/${invoiceId}`);
    const doubtful = page.getByRole("row").filter({ hasText: "not found in cited text" }).first();
    await expect(doubtful).toBeVisible();

    // Selecting a value outlines its source on the page image, inside the image.
    await doubtful.getByRole("button", { name: /^Show .* on the page$/ }).click();
    const image = page.getByRole("img", { name: "Page 1 of the original document" });
    const mark = page.locator(".mark.selected").first();
    await expect(mark).toBeVisible();
    const [imageBox, markBox] = [await image.boundingBox(), await mark.boundingBox()];
    expect(imageBox && markBox).toBeTruthy();
    expect(markBox!.x).toBeGreaterThanOrEqual(imageBox!.x - 1);
    expect(markBox!.y).toBeGreaterThanOrEqual(imageBox!.y - 1);
    expect(markBox!.x + markBox!.width).toBeLessThanOrEqual(imageBox!.x + imageBox!.width + 1);
    expect(markBox!.y + markBox!.height).toBeLessThanOrEqual(imageBox!.y + imageBox!.height + 1);
    await expect(page.getByText(/is outlined on page 1/)).toBeAttached();

    // Confirm it: the same text, a reason, and the PIN.
    await doubtful.getByRole("button", { name: /^Correct / }).click();
    const form = page.getByRole("form", { name: /^Correct / });
    await form.getByLabel("Reason").fill("checked against the paper copy");
    await form.getByLabel("Your email").fill(EMAIL);
    await form.getByLabel("Your PIN").fill(PIN);
    await form.getByRole("button", { name: "Save correction" }).click();
    await expect(page.getByText("confirmed by reviewer").first()).toBeVisible();
    await expect(page.getByText("It can be approved")).toBeVisible();
    await expect(page.getByRole("heading", { name: "Corrections" })).toBeVisible();

    // A wrong PIN is refused without saying which part was wrong.
    const sign = page.getByRole("form", { name: "Sign the review" });
    await sign.getByLabel("Reason", { exact: true }).fill("matches the order and the goods received");
    await sign.getByLabel("Your email").fill(EMAIL);
    await sign.getByLabel("Your PIN").fill("000000");
    await sign.getByRole("button", { name: "Sign and approve" }).click();
    await expect(sign.getByRole("alert")).toHaveText("The email or PIN is not right.");

    await sign.getByLabel("Your PIN").fill(PIN);
    await sign.getByRole("button", { name: "Sign and approve" }).click();
    const signed = page.getByLabel("Signed review");
    await expect(signed).toContainText("approved by E2E Reviewer");
    await expect(signed).toContainText("The stored review matches its signature.");
    await expect(signed).toContainText("I approve this invoice for payment");
    await expect(signed.getByRole("heading", { name: "Payment approval draft" })).toBeVisible();
    await expect(signed).toContainText("INR");
    await expect(page.getByRole("button", { name: /^Correct / })).toHaveCount(0);

    // Opening it again later shows the signed review, not a form.
    await page.reload();
    await expect(page.getByLabel("Signed review")).toContainText("approved by E2E Reviewer");
    await expect(page.getByRole("form", { name: "Sign the review" })).toHaveCount(0);
  });

  test("every action is in the audit log and the chain verifies", async ({ page }) => {
    const entries = (await (await page.request.get(`/api/v1/documents/${invoiceId}/audit`)).json()) as { action: string; actor: string }[];
    const actions = entries.map((entry) => entry.action);

    expect(actions).toEqual(expect.arrayContaining(["document.received", "extraction.created", "assessment.created", "match.created", "review.corrected", "review.signed"]));
    expect(actions.indexOf("review.corrected")).toBeLessThan(actions.indexOf("review.signed"));
    expect(entries.find((e) => e.action === "review.signed")?.actor).toMatch(/^reviewer:/);
    const chain = (await (await page.request.get("/api/v1/audit/verification")).json()) as { consistent: boolean };
    expect(chain.consistent).toBe(true);
  });

  test("search finds the invoice by its batch and opens it", async ({ page }) => {
    const label = JSON.parse(readFileSync(path.join(FIXTURES, "label.json"), "utf-8")) as {
      invoice: { lines: { batch_no: string }[] };
    };
    const batch = label.invoice.lines[0].batch_no;
    await page.goto("/search");
    await page.getByLabel("Question or words").fill(`Which invoice billed batch ${batch}?`);
    await page.getByRole("button", { name: "Search" }).click();
    const first = page.getByRole("listitem").first();
    await expect(first).toContainText(batch, { timeout: 30_000 });
    await first.getByRole("link", { name: "invoice.pdf" }).click();
    await expect(page).toHaveURL(new RegExp(`/documents/${invoiceId}$`));
  });

  test("the queue is empty again and the eval page shows the measured results", async ({ page }) => {
    await page.goto("/");
    await expect(page.getByText("Nothing is waiting for review.")).toBeVisible();

    await page.goto("/evals");
    await expect(page.getByRole("heading", { name: "Seeded defects" })).toBeVisible();
    await expect(page.getByText("9 / 9")).toBeVisible();
    await expect(page.getByText("No model prices are configured")).toBeVisible();
  });
});
