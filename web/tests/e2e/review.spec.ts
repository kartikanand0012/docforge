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

test.describe.serial("a flagged invoice is resolved end to end", () => {
  let invoiceId = "";

  test("the order and the invoice are uploaded and processed", async ({ page }) => {
    await upload(page, "purchase_order.pdf", "purchase_order");
    await expect(page.getByText("It can be approved")).toBeVisible({ timeout: 60_000 });

    invoiceId = await upload(page, "invoice.pdf", "invoice");
    await expect(page.getByText("Needs a person")).toBeVisible({ timeout: 60_000 });
    await expect(page.getByText("order: match")).toBeVisible();
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

    // Selecting a value outlines its source on the page image.
    await doubtful.getByRole("button", { pressed: false }).first().click();
    await expect(page.locator(".mark.selected").first()).toBeVisible();
    await expect(page.getByRole("img", { name: "Page 1 of the original document" })).toBeVisible();

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
    await expect(signed).toContainText("The record matches what was signed.");
    await expect(signed.getByRole("heading", { name: "Payment approval draft" })).toBeVisible();
    await expect(signed).toContainText("INR");
    await expect(page.getByRole("button", { name: /^Correct / })).toHaveCount(0);
  });

  test("every action is in the audit log and the chain verifies", async ({ request }) => {
    const entries = (await (await request.get(`${API}/v1/documents/${invoiceId}/audit`)).json()) as { action: string; actor: string }[];
    const actions = entries.map((entry) => entry.action);

    expect(actions).toEqual(expect.arrayContaining(["document.received", "extraction.created", "assessment.created", "match.created", "review.corrected", "review.signed"]));
    expect(actions.indexOf("review.corrected")).toBeLessThan(actions.indexOf("review.signed"));
    expect(entries.find((e) => e.action === "review.signed")?.actor).toMatch(/^reviewer:/);
    const chain = (await (await request.get(`${API}/v1/audit/verification`)).json()) as { consistent: boolean };
    expect(chain.consistent).toBe(true);
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
