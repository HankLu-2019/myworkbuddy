import { useEffect, useState } from "react";

// The signed-in user's timezone (rule R8), set by AuthProvider; the browser's until then.
let displayZone: string | undefined;

export function setDisplayTimezone(zone: string | null | undefined) {
  try {
    displayZone = zone ? (new Intl.DateTimeFormat(undefined, { timeZone: zone }), zone) : undefined;
  } catch {
    displayZone = undefined; // unknown zone: fall back to the browser's
  }
}

export function displayTimezone(): string {
  return displayZone ?? Intl.DateTimeFormat().resolvedOptions().timeZone;
}

/** Absolute local time in the user's timezone, e.g. "21 Oct 2026, 19:37". */
export function formatDate(iso: string): string {
  return new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short", timeZone: displayZone });
}

const UNITS: [Intl.RelativeTimeFormatUnit, number][] = [
  ["year", 365 * 24 * 3600],
  ["month", 30 * 24 * 3600],
  ["week", 7 * 24 * 3600],
  ["day", 24 * 3600],
  ["hour", 3600],
  ["minute", 60],
];

/** "just now", "5 minutes ago", "in 2 hours" (07 §6.10). */
export function formatRelative(iso: string, now: number = Date.now()): string {
  const seconds = Math.round((new Date(iso).getTime() - now) / 1000);
  if (Math.abs(seconds) < 45) return "just now";
  const rtf = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });
  for (const [unit, size] of UNITS) {
    if (Math.abs(seconds) >= size * 0.9 || unit === "minute") return rtf.format(Math.round(seconds / size), unit);
  }
  return "just now";
}

/** "5 minutes ago", with the absolute time on hover and for screen readers; refreshes every minute. */
export function RelativeTime({ iso }: { iso: string }) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 60_000);
    return () => clearInterval(t);
  }, []);
  const absolute = formatDate(iso);
  return (
    <time dateTime={iso} title={absolute} aria-label={absolute}>
      {formatRelative(iso, now)}
    </time>
  );
}

/** "3 min 20 s" for elapsed background work. */
export function formatElapsed(fromIso: string, now: number = Date.now()): string {
  const s = Math.max(0, Math.round((now - new Date(fromIso).getTime()) / 1000));
  if (s < 60) return `${s} s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m} min ${s % 60} s`;
  return `${Math.floor(m / 60)} h ${m % 60} min`;
}
