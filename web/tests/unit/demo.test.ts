import { describe, expect, it } from "vitest";
import { demoSignIn } from "@/lib/demo";

describe("demoSignIn", () => {
  it("is nothing unless the demo is switched on", () => {
    expect(demoSignIn({})).toBeNull();
    expect(demoSignIn({ DOCFORGE_DEMO_PIN: "" })).toBeNull();
  });

  it("gives the shared demo account when a PIN is set", () => {
    expect(demoSignIn({ DOCFORGE_DEMO_PIN: "482915" })).toEqual({
      tenant: "demo",
      email: "demo@docforge.example",
      pin: "482915",
    });
  });

  it("ignores a PIN that the API would refuse", () => {
    expect(demoSignIn({ DOCFORGE_DEMO_PIN: "12ab" })).toBeNull();
  });
});
