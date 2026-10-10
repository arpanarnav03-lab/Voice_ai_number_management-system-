import type { StackRow } from "./api";

export type Picked = Record<number, boolean>;

/** Text next to the Auto-select button. */
export function autoSelectHelp(): string {
  return "Ticks the next number that can be tested.";
}

/** What to tell the person after Auto-select when nothing could be ticked, otherwise null. */
export function autoSelectNote(got: number): string | null {
  return got === 0 ? "No numbers can be tested right now." : null;
}

/** Auto-select replaces every earlier tick, including ones made by hand. */
export function replaceSelection(ids: number[]): Picked {
  return Object.fromEntries(ids.map((id) => [id, true]));
}

/** A ticked row that is no longer testable (a call started, an awaiting result, not on Bolna) is
 *  unticked, so a stale selection never reaches the server. Returns what was dropped and why. */
export function pruneSelection(picked: Picked, rows: StackRow[]): { picked: Picked; dropped: { id: number; phone: string; reason: string }[] } {
  const byId = new Map(rows.map((r) => [r.id, r]));
  const next: Picked = {};
  const dropped: { id: number; phone: string; reason: string }[] = [];
  for (const id of Object.keys(picked).map(Number)) {
    if (!picked[id]) continue;
    const r = byId.get(id);
    if (r && r.eligible) next[id] = true;
    else dropped.push({ id, phone: r?.phone_number ?? `#${id}`, reason: r?.reason?.message ?? "No longer in the stack" });
  }
  return { picked: dropped.length ? next : picked, dropped };
}
