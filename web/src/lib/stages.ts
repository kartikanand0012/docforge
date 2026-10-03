/** A document's journey, as the person who uploaded it sees it. */
export type Step = { stage: string; at: string; detail?: string | null };

export type StageState = "done" | "current" | "waiting" | "failed";

export type StageView = { stage: string; label: string; state: StageState; at: string | null; detail: string | null };

const MAIN = ["stored", "parsing", "extracting", "checking", "indexing", "ready"] as const;

export const STAGE_LABELS: Record<string, string> = {
  stored: "Stored",
  parsing: "Reading the pages",
  extracting: "Extracting values",
  checking: "Checking",
  indexing: "Indexing for search and chat",
  ready: "Ready to chat",
  processed: "Processed",
  retrying: "Retrying",
  failed: "Failed",
};

const view = (stage: string, state: StageState, step?: Step): StageView => ({
  stage,
  label: STAGE_LABELS[stage] ?? stage,
  state,
  at: step?.at ?? null,
  detail: step?.detail ?? null,
});

/** Every stage in order, done or current or still to come; a failure or the end of
 * processing (when nothing indexes the document) ends the list where it happened. */
export function stageView(steps: Step[]): StageView[] {
  const latest = new Map<string, Step>();
  for (const step of steps) latest.set(step.stage, step);
  const last = steps.at(-1);
  const reached = MAIN.filter((stage) => latest.has(stage));

  if (last?.stage === "failed" || last?.stage === "processed") {
    const done = reached.map((stage) => view(stage, "done", latest.get(stage)));
    return [...done, view(last.stage, last.stage === "failed" ? "failed" : "done", last)];
  }
  if (last?.stage === "retrying") {
    const done = reached.map((stage) => view(stage, "done", latest.get(stage)));
    const rest = MAIN.filter((stage) => !latest.has(stage)).map((stage) => view(stage, "waiting"));
    return [...done, view("retrying", "current", last), ...rest];
  }
  return MAIN.map((stage) => {
    const step = latest.get(stage);
    if (!step) return view(stage, "waiting");
    const current = stage === last?.stage && stage !== "ready";
    return view(stage, current ? "current" : "done", step);
  });
}

/** True once nothing more will happen without someone asking. */
export function finished(steps: Step[]): boolean {
  const last = steps.at(-1)?.stage;
  return last === "ready" || last === "processed" || last === "failed";
}
