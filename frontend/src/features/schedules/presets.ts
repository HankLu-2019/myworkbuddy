/** "When" presets in the schedule form (07 §4.7). Cron is only shown raw for Custom (07 §6.10). */
export type Preset = "weekday_morning" | "hourly" | "daily" | "custom";

export const PRESETS: readonly (readonly [Preset, string, string])[] = [
  ["weekday_morning", "Every weekday morning", "0 8 * * 1-5"],
  ["hourly", "Every hour", "0 * * * *"],
  ["daily", "Every day", "0 8 * * *"],
  ["custom", "Custom", ""],
];

export function presetOf(cron: string): Preset {
  const normalised = cron.trim().split(/\s+/).join(" ");
  return PRESETS.find(([key, , expr]) => key !== "custom" && expr === normalised)?.[0] ?? "custom";
}

export function cronOf(preset: Preset, current: string): string {
  return PRESETS.find(([key]) => key === preset)?.[2] || current;
}

export const NOTIFY_OPTIONS = [
  ["on_change", "On change"],
  ["on_alert", "On alert only"],
  ["always", "Every run"],
] as const;

export const NOTIFY_LABEL: Record<string, string> = {
  on_change: "Notify on change",
  on_alert: "Notify on alert only",
  always: "Notify on every run",
};
