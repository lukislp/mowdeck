/**
 * Day headings of the inbox: "today", "yesterday" or the date, in the browser's time zone.
 * Pure and unit tested (`days.test.ts`).
 */

/** A key of the local calendar day of `at`. */
export function dayKey(at: string | number | Date): string {
  const date = new Date(at);
  return `${date.getFullYear()}-${date.getMonth()}-${date.getDate()}`;
}

/**
 * Which local day `at` falls on relative to `now`. "Yesterday" is the previous calendar day,
 * not 24 hours ago: a DST change makes a day 23 or 25 hours long.
 */
export function relativeDay(at: string, now: number): 'today' | 'yesterday' | null {
  const key = dayKey(at);
  if (key === dayKey(now)) return 'today';
  const yesterday = new Date(now);
  yesterday.setDate(yesterday.getDate() - 1);
  return key === dayKey(yesterday) ? 'yesterday' : null;
}
