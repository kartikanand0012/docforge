"use client";

import { useEffect, useRef, useState } from "react";
import { ApiError, api } from "@/lib/api";
import { currentStage, finished, stageView, type Step } from "@/lib/stages";

const time = (iso: string) => new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });

function parseStep(data: string): Step | null {
  try {
    const step = JSON.parse(data) as Partial<Step>;
    return typeof step.stage === "string" && typeof step.at === "string" ? (step as Step) : null;
  } catch {
    return null;
  }
}

/** Where a document is, live: each stage as it is reached, from the server's event stream,
 * with a fallback to asking every two seconds (longer after errors) if the stream cannot be
 * held open. A refusal (signed out, no such document) stops asking and says so. */
export default function Timeline({
  id,
  docType,
  compact = false,
  onChange,
}: {
  id: string;
  /** A general document skips extraction and checking, so they are not shown as to come. */
  docType?: string;
  compact?: boolean;
  /** Called when the latest stage changes, so the page can load what that stage made. */
  onChange?: (steps: Step[]) => void;
}) {
  const [steps, setSteps] = useState<Step[]>([]);
  const [problem, setProblem] = useState<string | null>(null);
  const changed = useRef(onChange);
  useEffect(() => {
    changed.current = onChange;
  }, [onChange]);

  useEffect(() => {
    let stopped = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let source: EventSource | null = null;
    let lastStage: string | undefined;
    let failures = 0;
    const seen: Step[] = [];

    const settle = (next: Step[]) => {
      if (stopped) return;
      setSteps(next);
      setProblem(null);
      const stage = next.at(-1)?.stage;
      if (stage !== lastStage) {
        lastStage = stage;
        changed.current?.(next);
      }
      if (finished(next)) source?.close();
    };
    const poll = async () => {
      try {
        const next = await api.timeline(id);
        failures = 0;
        settle(next);
        if (finished(next) || stopped) return;
      } catch (e) {
        if (stopped) return;
        if (e instanceof ApiError && e.status >= 400 && e.status < 500) {
          setProblem(e.message); // asking again will not help
          return;
        }
        failures += 1;
        setProblem("The stages cannot be loaded just now. Trying again…");
      }
      timer = setTimeout(poll, Math.min(2000 * 2 ** failures, 30_000));
    };

    if (typeof EventSource === "undefined") {
      void poll();
    } else {
      source = new EventSource(`/api/v1/documents/${encodeURIComponent(id)}/events`);
      source.addEventListener("stage", (event) => {
        const step = parseStep((event as MessageEvent<string>).data);
        if (!step) return;
        seen.push(step);
        settle([...seen]);
      });
      source.onerror = () => {
        // The stream ended (finished) or broke: one look at the timeline settles which.
        source?.close();
        if (!stopped && !finished(seen)) void poll();
      };
    }
    return () => {
      stopped = true;
      source?.close();
      if (timer) clearTimeout(timer);
    };
  }, [id]);

  const now = currentStage(steps, docType);
  const announcement = <p className="sr-only" aria-live="polite">{steps.length ? now.label : ""}</p>;
  const alert = problem && (
    <p className="error" role="alert">
      {problem}
    </p>
  );
  if (compact) {
    return (
      <>
        <p className="timeline-compact">
          <span className={`chip stage-${now.state}`}>{now.label}</span>
        </p>
        {announcement}
        {alert}
      </>
    );
  }
  return (
    <>
      <ol className="timeline" aria-label="Processing stages">
        {stageView(steps, docType).map((s) => (
          <li key={s.stage} className={`stage-${s.state}`} aria-current={s.state === "current" ? "step" : undefined}>
            <span className="stage-mark" aria-hidden="true">
              {s.state === "done" ? "✓" : s.state === "failed" ? "✕" : s.state === "current" ? "●" : "○"}
            </span>
            <span className="stage-label">{s.label}</span>
            {s.at && <time className="muted" dateTime={s.at}>{time(s.at)}</time>}
            {s.detail && <span className={s.state === "failed" ? "error" : "muted"}>{s.detail}</span>}
          </li>
        ))}
      </ol>
      {announcement}
      {alert}
    </>
  );
}
