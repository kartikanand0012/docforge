import { createHmac } from "node:crypto";
import { readFileSync } from "node:fs";
import { createServer } from "node:http";
import type { AddressInfo } from "node:net";
import path from "node:path";
import { expect, test, type Page } from "@playwright/test";

const FIXTURES = path.resolve(__dirname, "../../../tests/fixtures/synthetic/pair_002");
const GENERAL = path.resolve(__dirname, "../../../tests/fixtures/general");
const API = process.env.E2E_API_URL ?? "http://127.0.0.1:8011";
const EMAIL = "e2e@example.com";
const PIN = "246810";

async function upload(page: Page, file: string, type: "invoice" | "purchase_order" | "general"): Promise<string> {
  await page.goto("/upload");
  await page.getByLabel("Document type").selectOption(type);
  await page.getByLabel(/^PDF \(born-digital or scanned\), Word/).setInputFiles(file);
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
    await upload(page, path.join(FIXTURES, "purchase_order.pdf"), "purchase_order");
    await expect(page.getByText("It can be approved")).toBeVisible({ timeout: 60_000 });

    invoiceId = await upload(page, path.join(FIXTURES, "invoice.pdf"), "invoice");
    await expect(page.getByText("Needs a person")).toBeVisible({ timeout: 60_000 });
    await expect(page.getByText("order: match")).toBeVisible();
    // Indexed for search and chat after the extraction: the page says so as it happens.
    await expect(page.locator(".timeline-compact")).toContainText("Ready to chat", { timeout: 60_000 });

    // Both are listed, newest first, with where each one is.
    await page.getByRole("link", { name: "Documents" }).click();
    const rows = page.locator("table.documents tbody tr");
    await expect(rows).toHaveCount(2);
    await expect(rows.first()).toContainText("invoice.pdf");
    await expect(rows.first()).toContainText("Ready to chat");
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

  test("the audit log shows the signed review by its reviewer, checks the chain, and exports", async ({ page }) => {
    await page.getByRole("link", { name: "Audit log" }).click();
    await page.getByRole("button", { name: "Check the chain" }).click();
    await expect(page.getByRole("status").filter({ hasText: "Verified" })).toContainText("chain intact");

    const filters = page.getByRole("form", { name: "Filter the audit log" });
    await filters.getByLabel("Action").selectOption({ label: "Review signed" });
    await filters.getByRole("button", { name: "Filter" }).click();
    await expect(page).toHaveURL(/action=review\.signed/);
    const row = page.locator("table.documents tbody tr");
    await expect(row).toHaveCount(1);
    await expect(row).toContainText("E2E Reviewer");
    await expect(row).toContainText("invoice.pdf");

    const download = page.waitForEvent("download");
    await filters.getByRole("link", { name: "Export CSV" }).click();
    const file = await (await download).path();
    const csv = readFileSync(file!, "utf-8");
    expect(csv.split("\n")[0]).toBe("id,occurred_at,actor,actor_name,action,target_type,target_id,details,prev_hash,hash");
    expect(csv).toContain("review.signed");
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

  test("a Word document is converted, read and indexed for search and chat, with nothing extracted", async ({ page }) => {
    await upload(page, path.join(GENERAL, "sop-goods-receipt.docx"), "general");
    await expect(page.getByRole("heading", { name: /sop-goods-receipt\.docx/ })).toContainText("Word");

    const stages = page.getByRole("list", { name: "Processing stages" });
    await expect(stages.locator("li").last()).toHaveClass(/stage-done/, { timeout: 60_000 });
    await expect(stages).toContainText("Converting to PDF");
    await expect(stages.locator("li").last()).toContainText("Ready to chat");
    await expect(stages).not.toContainText("Extracting values");
    await expect(page.getByRole("img", { name: "Page 1 of sop-goods-receipt.docx" })).toBeVisible();

    // Asked about, it answers from the document with the quote it rests on, shown on the page.
    const chat = page.getByRole("region", { name: "Ask about this document" });
    await chat.getByLabel("Question").fill("What happens to goods above 8 °C?");
    await chat.getByRole("button", { name: "Ask" }).click();
    await expect(chat.locator(".answer")).toContainText("rejected", { timeout: 30_000 });
    const source = chat.getByRole("button", { name: "sop-goods-receipt.docx, page 1" });
    await expect(source).toBeVisible();
    await expect(chat.locator(".citations q").first()).toContainText("8 °C");
    await source.click();
    await expect(page.locator("#page-1 .mark.cited").first()).toBeVisible();

    // A question the document cannot answer is not answered.
    await chat.getByLabel("Question").fill("Who signed this procedure?");
    await chat.getByRole("button", { name: "Ask" }).click();
    await expect(chat.locator(".answer").nth(1)).toContainText("The documents do not say.", { timeout: 30_000 });

    await page.goto("/search");
    await page.getByLabel("Question or words").fill("What happens to goods above 8 °C?");
    await page.getByRole("button", { name: "Search" }).click();
    // The passage that answers comes first, shown where the question's words are.
    const first = page.getByRole("listitem").first();
    await expect(first.getByRole("link", { name: "sop-goods-receipt.docx" })).toBeVisible({ timeout: 30_000 });
    await expect(first).toContainText("anything above 8 °C is rejected");
  });

  test("a knowledge base is made, given the procedure, and asked within", async ({ page }) => {
    await page.getByRole("link", { name: "Knowledge bases" }).click();
    const form = page.getByRole("form", { name: "New knowledge base" });
    await form.getByLabel("Name").fill("Warehouse procedures");
    await form.getByRole("button", { name: "Create" }).click();
    await page.getByRole("link", { name: "Warehouse procedures" }).click();

    await page.getByLabel("Documents to add").selectOption({ label: "sop-goods-receipt.docx" });
    await page.getByRole("button", { name: "Add 1 document" }).click();
    const documents = page.getByRole("region", { name: "Documents in this knowledge base" });
    await expect(documents.getByRole("link", { name: "sop-goods-receipt.docx" })).toBeVisible();

    const chat = page.getByRole("region", { name: "Ask within Warehouse procedures" });
    await chat.getByLabel("Question").fill("What happens to goods above 8 °C?");
    await chat.getByRole("button", { name: "Ask" }).click();
    await expect(chat.locator(".answer")).toContainText("rejected", { timeout: 30_000 });
    await expect(chat.getByRole("link", { name: "sop-goods-receipt.docx, page 1" })).toBeVisible();
  });
});

test("an administrator connects an AI agent with a read-only key, and revokes it", async ({ page, request }) => {
  await signIn(page);
  await expect(page.getByRole("heading", { name: "Review queue" })).toBeVisible();
  await page.getByRole("link", { name: "AI agents" }).click();
  await page.getByLabel("What it is for").fill("E2E agent");
  await page.getByRole("button", { name: "Make key" }).click();

  const command = await page.locator("pre.command").innerText();
  expect(command).toContain("claude mcp add --transport http docforge");
  expect(command).toContain("/v1/mcp");
  const token = /Bearer (dfk_[0-9a-f]{12}_[A-Za-z0-9_-]{43})/.exec(command)![1];

  // The key works against the API itself, as an agent would use it.
  const list = () =>
    request.post(`${API}/v1/mcp`, {
      headers: { Authorization: `Bearer ${token}`, Accept: "application/json, text/event-stream" },
      data: { jsonrpc: "2.0", id: 1, method: "tools/list", params: {} },
    });
  const listed = await list();
  expect(listed.status()).toBe(200);
  expect(JSON.stringify(await listed.json())).toContain("search_documents");

  await page.getByRole("button", { name: /^Revoke E2E agent \(/ }).click();
  await expect(page.getByRole("row", { name: /E2E agent/ })).toContainText("Revoked");
  expect((await list()).status()).toBe(401);
});


test("an administrator manages a webhook end to end", async ({ page }) => {
  // A receiver on this machine that checks each delivery's signature with the secret shown.
  const received: { verified: boolean; type: string }[] = [];
  let secret = "";
  const server = createServer((request, response) => {
    let body = "";
    request.on("data", (chunk) => (body += chunk));
    request.on("end", () => {
      const header = String(request.headers["docforge-signature"] ?? "");
      const [t, v1] = header.split(",").map((part) => part.split("=")[1]);
      const expected = createHmac("sha256", secret).update(`${t}.${body}`).digest("hex");
      received.push({ verified: v1 === expected, type: JSON.parse(body).type });
      response.writeHead(200).end();
    });
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const { port } = server.address() as AddressInfo;
  page.on("dialog", (dialog) => void dialog.accept());
  try {
    await signIn(page);
    await expect(page.getByRole("heading", { name: "Review queue" })).toBeVisible();
    await page.getByRole("link", { name: "Webhooks" }).click();
    const form = page.getByRole("form", { name: "Make a webhook" });
    await form.getByLabel("URL (https)").fill(`http://127.0.0.1:${port}/hooks`);
    await form.getByLabel("review.signed").check();
    await form.getByRole("button", { name: "Make webhook" }).click();
    secret = (await page.locator("pre.command").innerText()).trim();
    expect(secret).toMatch(/^whsec_/);
    await page.getByRole("button", { name: "Done, I have copied it" }).click();

    await page.getByRole("button", { name: "Send test" }).click();
    await expect.poll(() => received.length, { timeout: 30_000 }).toBe(1);
    expect(received[0]).toEqual({ verified: true, type: "webhook.test" });

    await page.getByRole("button", { name: "Rotate secret" }).click();
    const rotated = (await page.locator("pre.command").innerText()).trim();
    expect(rotated).not.toBe(secret);
    secret = rotated;
    await page.getByRole("button", { name: "Done, I have copied it" }).click();
    await page.getByRole("button", { name: "Send test" }).click();
    await expect.poll(() => received.length, { timeout: 30_000 }).toBe(2);
    expect(received[1].verified).toBe(true); // signed with the new secret

    await page.getByRole("button", { name: "Deliveries" }).click();
    await expect(page.getByRole("region", { name: "Deliveries" }).locator("tbody tr")).toHaveCount(2);

    await page.getByRole("button", { name: "Disable" }).click();
    await expect(page.getByRole("button", { name: "Send test" })).toBeDisabled();
    await page.getByRole("button", { name: "Enable" }).click();
    await expect(page.getByRole("button", { name: "Send test" })).toBeEnabled();
    await page.getByRole("button", { name: /^Delete the webhook to/ }).click();
    await expect(page.getByText("No webhooks yet.")).toBeVisible();
  } finally {
    server.close();
  }
});
