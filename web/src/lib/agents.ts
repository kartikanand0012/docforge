/** Connecting an AI agent (Claude Code, or any MCP client over HTTP) to DocForge. */

/** The MCP endpoint: the site's own `/v1/mcp`, which the proxy sends to the API. */
export function mcpEndpoint(origin: string): string {
  return `${origin.replace(/\/+$/, "")}/v1/mcp`;
}

/** The one command that adds DocForge to Claude Code, with the key sent as a header. */
export function claudeCodeCommand(endpoint: string, token: string): string {
  return `claude mcp add --transport http docforge ${endpoint} --header "Authorization: Bearer ${token}"`;
}

/** What each outcome of an agent's call means, in words. */
export const OUTCOME_TEXT: Record<string, string> = {
  ok: "Answered",
  not_found: "Not found",
  limited: "Limited",
  invalid: "Not understood",
  error: "Failed",
};
