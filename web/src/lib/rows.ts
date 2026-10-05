/** A refreshed first page merged into what is shown: fresh copies first, newest first, then
 * every older row already loaded, each row once. Older pages stay where they were. */
export function mergeFresh<T extends { id: string }>(shown: T[], fresh: T[]): T[] {
  const seen = new Set<string>();
  const out: T[] = [];
  for (const row of [...fresh, ...shown]) {
    if (seen.has(row.id)) continue;
    seen.add(row.id);
    out.push(row);
  }
  return out;
}
