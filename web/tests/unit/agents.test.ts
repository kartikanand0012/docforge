import { describe, expect, it } from "vitest";
import { OUTCOME_TEXT, claudeCodeCommand, mcpEndpoint } from "@/lib/agents";

describe("connecting an AI agent", () => {
  it("is reached at the site's own /v1/mcp", () => {
    expect(mcpEndpoint("https://docforge.example")).toBe("https://docforge.example/v1/mcp");
    expect(mcpEndpoint("https://docforge.example/")).toBe("https://docforge.example/v1/mcp");
  });

  it("gives Claude Code a command with the key as a header", () => {
    expect(claudeCodeCommand("https://docforge.example/v1/mcp", "dfk_0123456789ab_secret")).toBe(
      'claude mcp add --transport http docforge https://docforge.example/v1/mcp --header "Authorization: Bearer dfk_0123456789ab_secret"',
    );
  });

  it("says what each outcome of a call means", () => {
    expect(Object.keys(OUTCOME_TEXT).sort()).toEqual(["error", "invalid", "limited", "not_found", "ok"]);
  });
});
