import { describe, expect, it } from "vitest";
import { declaredTooLarge, forwardedFor, sameOrigin } from "@/lib/server";

const request = (method: string, headers: Record<string, string>) =>
  new Request("http://localhost:3000/api/session", { method, headers });

describe("sameOrigin", () => {
  it("accepts a request from the page's own origin", () => {
    expect(sameOrigin(request("POST", { origin: "http://127.0.0.1:3011", host: "127.0.0.1:3011" }))).toBe(true);
  });

  it("refuses a state-changing request from another site", () => {
    expect(sameOrigin(request("POST", { origin: "https://evil.example", host: "127.0.0.1:3011" }))).toBe(false);
  });

  it("refuses a state-changing request with no origin", () => {
    expect(sameOrigin(request("POST", { host: "127.0.0.1:3011" }))).toBe(false);
    expect(sameOrigin(request("DELETE", { host: "127.0.0.1:3011" }))).toBe(false);
  });

  it("allows a plain read without an origin", () => {
    expect(sameOrigin(request("GET", { host: "127.0.0.1:3011" }))).toBe(true);
  });

  it("refuses a malformed origin", () => {
    expect(sameOrigin(request("POST", { origin: "not a url", host: "127.0.0.1:3011" }))).toBe(false);
  });
});

describe("cookieOptions", () => {
  it("is HttpOnly and SameSite=Strict, and Secure when the browser used HTTPS", async () => {
    const { cookieOptions } = await import("@/lib/server");
    const plain = cookieOptions(new Request("http://127.0.0.1:3011/api/session"));
    const proxied = cookieOptions(
      new Request("http://10.0.0.5:3000/api/session", { headers: { "x-forwarded-proto": "https" } }),
    );

    expect(plain).toMatchObject({ httpOnly: true, sameSite: "strict", secure: false });
    expect(proxied.secure).toBe(true);
  });
});

describe("forwardedFor", () => {
  const request = (xff?: string) =>
    new Request("https://demo.example.com/api/session", { headers: xff ? { "x-forwarded-for": xff } : {} });

  it("passes on the address the front proxy set, only where the proxy is trusted", () => {
    expect(forwardedFor(request("198.51.100.7"), { DOCFORGE_TRUST_PROXY: "1" })).toEqual({
      "X-Forwarded-For": "198.51.100.7",
    });
  });

  it("drops it otherwise: a browser can send anything", () => {
    expect(forwardedFor(request("198.51.100.7"), {})).toEqual({});
    expect(forwardedFor(request(), { DOCFORGE_TRUST_PROXY: "1" })).toEqual({});
  });
});

describe("declaredTooLarge", () => {
  it("refuses a body declared larger than the limit", () => {
    const big = new Request("https://x/api", { method: "POST", headers: { "content-length": "20000000" } });
    const small = new Request("https://x/api", { method: "POST", headers: { "content-length": "100" } });
    expect(declaredTooLarge(big, 11 * 1024 * 1024)).toBe(true);
    expect(declaredTooLarge(small, 11 * 1024 * 1024)).toBe(false);
  });
});
