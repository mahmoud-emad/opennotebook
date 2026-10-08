// Times on the watch page: as the player shows them, and what is on when.

/** A time as the player shows it: 1:24, or 1:02:03 past the hour. */
export function clock(ms: number): string {
  const s = Math.max(0, Math.floor(ms / 1000));
  const h = Math.floor(s / 3600);
  const mm = Math.floor((s % 3600) / 60);
  const ss = String(s % 60).padStart(2, "0");
  return h > 0 ? `${h}:${String(mm).padStart(2, "0")}:${ss}` : `${mm}:${ss}`;
}

/** The index of the item on at `t`: the last to start by then, or -1. */
export function onAt(items: { start_ms: number }[], t: number): number {
  let on = -1;
  items.forEach((it, i) => {
    if (it.start_ms <= t) on = i;
  });
  return on;
}
