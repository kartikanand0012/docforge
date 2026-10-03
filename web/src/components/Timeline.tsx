"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { finished, stageView, type Step } from "@/lib/stages";

const time = (iso: string) => new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });

/** Where a document is, live: each stage as it is reached, from the server's event stream,
 * with a fallback to asking every two seconds if the stream cannot be held open. */
export default function Timeline({
  id,
  compact = false,
  onChange,
}: {
  id: string;
  compact?: boolean;
  /** Called with the steps whenever they change, so the page can load what a stage made. */
  onChange?: (steps: Step[]) => void;
}) {
  const [steps, setSteps] = useState<Step[]>([]);

  useEffect(() => {
    let stopped = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let source: EventSource | null = null;
    const seen: Step[] = [];

    const settle = (next: Step[]) => {
      if (stopped) return;
      setSteps(next);
      onChange?.(next);
      if (finished(next)) source?.close();
    };
    const poll = async () => {
      try {
        const next = await api.timeline(id);
        settle(next);
        if (finished(next) || stopped) return;
      } catch {
        /* keep the last steps shown and ask again */
      }
      timer = setTimeout(poll, 2000);
    };

    if (typeof EventSource === "undefined") {
      void poll();
    } else {
      source = new EventSource(`/api/v1/documents/${encodeURIComponent(id)}/events`);
      source.addEventListener("stage", (event) => {
        seen.push(JSON.parse((event as MessageEvent<string>).data) as Step);
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
  }, [id, onChange]);

  const stages = stageView(steps);
  if (compact) {
    const current = stages.find((s) => s.state === "current" || s.state === "failed") ?? stages.at(-1);
    return (
      <p className="timeline-compact" aria-live="polite">
        <span className={`chip stage-${current?.state ?? "waiting"}`}>{current?.label ?? "Stored"}</span>
      </p>
    );
  }
  return (
    <ol className="timeline" aria-label="Processing stages" aria-live="polite">
      {stages.map((s) => (
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
  );
}
