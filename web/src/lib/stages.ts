/** A document's journey, as the person who uploaded it sees it. */
export type Step = { stage: string; at: string; detail?: string | null };

export type StageState = "done" | "current" | "waiting" | "failed";

export type StageView = { stage: string; label: string; state: StageState; at: string | null; detail: string | null };

const MAIN = ["stored", "parsing", "extracting", "checking", "indexing", "ready"] as const;
/** A general document is read and indexed, with nothing extracted or checked. */
const GENERAL = ["stored", "parsing", "indexing", "ready"] as const;

export const STAGE_LABELS: Record<string, string> = {
  stored: "Stored",
  converting: "Converting to PDF",
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
export function stageView(steps: Step[], docType?: string): StageView[] {
  const latest = new Map<string, Step>();
  for (const step of steps) latest.set(step.stage, step);
  const last = steps.at(-1);
  // Converting happens only to a file that is not a PDF, so it is shown only once it has.
  const base: readonly string[] = docType === "general" ? GENERAL : MAIN;
  const plan = latest.has("converting") ? [base[0], "converting", ...base.slice(1)] : base;
  const reached = plan.filter((stage) => latest.has(stage));

  if (last?.stage === "failed" || last?.stage === "processed") {
    const done = reached.map((stage) => view(stage, "done", latest.get(stage)));
    return [...done, view(last.stage, last.stage === "failed" ? "failed" : "done", last)];
  }
  if (last?.stage === "retrying") {
    const done = reached.map((stage) => view(stage, "done", latest.get(stage)));
    const rest = plan.filter((stage) => !latest.has(stage)).map((stage) => view(stage, "waiting"));
    return [...done, view("retrying", "current", last), ...rest];
  }
  return plan.map((stage) => {
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
