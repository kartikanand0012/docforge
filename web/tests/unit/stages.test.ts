import { describe, expect, it } from "vitest";
import { stageView, type Step } from "@/lib/stages";

const at = (n: number) => `2026-10-03T10:00:0${n}Z`;

describe("stageView", () => {
  it("shows every stage in order: done, current, then waiting", () => {
    const steps: Step[] = [
      { stage: "stored", at: at(0) },
      { stage: "parsing", at: at(1) },
      { stage: "extracting", at: at(2) },
    ];

    const view = stageView(steps);

    expect(view.map((s) => [s.stage, s.state])).toEqual([
      ["stored", "done"],
      ["parsing", "done"],
      ["extracting", "current"],
      ["checking", "waiting"],
      ["indexing", "waiting"],
      ["ready", "waiting"],
    ]);
    expect(view[0].at).toBe(at(0));
    expect(view[3].at).toBeNull();
  });

  it("marks ready as done once the document can be chatted with", () => {
    const steps: Step[] = ["stored", "parsing", "extracting", "checking", "indexing", "ready"].map(
      (stage, n) => ({ stage, at: at(n) }),
    );
    const view = stageView(steps);
    expect(view.every((s) => s.state === "done")).toBe(true);
  });

  it("stops at a failure and gives its reason", () => {
    const steps: Step[] = [
      { stage: "stored", at: at(0) },
      { stage: "parsing", at: at(1) },
      { stage: "failed", at: at(2), detail: "The file could not be read as a PDF." },
    ];

    const view = stageView(steps);

    expect(view.at(-1)).toEqual({
      stage: "failed",
      label: "Failed",
      state: "failed",
      at: at(2),
      detail: "The file could not be read as a PDF.",
    });
    expect(view.some((s) => s.state === "waiting")).toBe(false);
  });

  it("ends at processed when nothing indexes the document for chat", () => {
    const steps: Step[] = ["stored", "parsing", "extracting", "checking", "processed"].map((stage, n) => ({
      stage,
      at: at(n),
    }));

    const view = stageView(steps);

    expect(view.map((s) => s.stage)).toEqual(["stored", "parsing", "extracting", "checking", "processed"]);
    expect(view.at(-1)?.state).toBe("done");
  });

  it("shows a retry as the current step with its reason, then carries on", () => {
    const steps: Step[] = [
      { stage: "stored", at: at(0) },
      { stage: "parsing", at: at(1) },
      { stage: "retrying", at: at(2), detail: "The model provider failed." },
    ];

    const view = stageView(steps);

    expect(view.find((s) => s.stage === "retrying")).toMatchObject({ state: "current", detail: "The model provider failed." });
  });

  it("shows converting only for a file that was converted", () => {
    const converted = stageView([
      { stage: "stored", at: at(0) },
      { stage: "converting", at: at(1) },
    ]);
    expect(converted.map((s) => [s.stage, s.state]).slice(0, 2)).toEqual([
      ["stored", "done"],
      ["converting", "current"],
    ]);
    expect(converted[1].label).toBe("Converting to PDF");

    const pdf = stageView([{ stage: "stored", at: at(0) }]);
    expect(pdf.map((s) => s.stage)).not.toContain("converting");
  });

  it("leaves out the stages a general document skips", () => {
    const steps: Step[] = ["stored", "converting", "parsing", "indexing", "ready"].map((stage, n) => ({ stage, at: at(n) }));

    const view = stageView(steps, "general");

    expect(view.map((s) => [s.stage, s.state])).toEqual([
      ["stored", "done"],
      ["converting", "done"],
      ["parsing", "done"],
      ["indexing", "done"],
      ["ready", "done"],
    ]);
  });

  it("does not promise extraction for a general document still being read", () => {
    const view = stageView([{ stage: "stored", at: at(0) }, { stage: "parsing", at: at(1) }], "general");
    expect(view.map((s) => s.stage)).toEqual(["stored", "parsing", "indexing", "ready"]);
  });
});
