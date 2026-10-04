const COMMON = new Set(["the", "and", "for", "what", "which", "who", "how", "are", "was", "with", "that", "this", "from", "does", "did", "has", "have", "happens", "when", "where"]);

/** The words of a question worth looking for: three letters or more, not the common ones. */
function terms(question: string): string[] {
  const words = question.toLowerCase().match(/[\p{L}\p{N}]+/gu) ?? [];
  return [...new Set(words.filter((w) => w.length >= 3 && !COMMON.has(w)))];
}

/** At most `size` characters of `text`: the stretch with the most of the question's words,
 * or the start when none is there. Cut ends are marked with "…". */
export function snippet(text: string, question: string, size: number): string {
  if (text.length <= size) return text;
  const lower = text.toLowerCase();
  const wanted = terms(question);
  let best = { start: 0, score: 0 };
  for (const word of wanted) {
    for (let at = lower.indexOf(word); at !== -1; at = lower.indexOf(word, at + 1)) {
      const start = Math.max(0, Math.min(at - Math.floor(size / 3), text.length - size));
      const window = lower.slice(start, start + size);
      const score = wanted.filter((w) => window.includes(w)).length;
      if (score > best.score || (score === best.score && score > 0 && start < best.start)) best = { start, score };
    }
  }
  const start = best.start;
  const end = start + size;
  return `${start > 0 ? "…" : ""}${text.slice(start, end).trim()}${end < text.length ? "…" : ""}`;
}
