import { ApiError } from "../api";

/** What the UI shows for an error, and the one thing the user can do about it (07-ux §5.10, §6.8). */
export type ErrorView = {
  message: string;
  action?: { label: string; href: string };
  /** Request id to quote when reporting the problem; shown with a Copy button. */
  ref?: string | null;
};

// Codes whose server message is written for users (app/errors.py callers): show it as is.
const SERVER_WORDED = new Set([
  "invalid_credentials",
  "validation_error",
  "bad_request",
  "wiki_conflict",
  "session_busy",
  "plan_changed",
  "unsupported_file",
  "file_too_large",
  "disk_full",
  "password_change_required",
  "csrf_invalid",
]);

function minutes(seconds: number): string {
  const m = Math.ceil(seconds / 60);
  return m <= 1 ? "a minute" : `${m} minutes`;
}

export function describeError(error: unknown): ErrorView {
  if (!(error instanceof ApiError)) {
    if (error instanceof Error && error.name === "TypeError") {
      return { message: "Can't reach MyWorkBuddy. Check your connection and try again." };
    }
    return { message: error instanceof Error ? error.message : String(error) };
  }
  const { code, message } = error;
  switch (code) {
    case "not_found":
      return { message: "This item doesn't exist or isn't shared with you.", action: { label: "Go Home", href: "/" } };
    case "forbidden":
      return { message: "Only the owner can do this." };
    case "conflict":
      return { message: `${message} Reload to see the latest version.` };
    case "rate_limited":
      return { message: error.retryAfter ? `Too many attempts. Try again in ${minutes(error.retryAfter)}.` : message };
    case "budget_exceeded":
      return {
        message: message || "You've reached this month's AI budget. An admin can raise it.",
        action: { label: "View usage", href: "/settings/usage" },
      };
    case "llm_unavailable":
      return { message: "The AI service isn't responding. Your message is saved; try again in a minute." };
    case "credential_invalid":
      return { message, action: { label: "Update token", href: "/settings/integrations" } };
    case "not_signed_in":
      return { message: "You're signed out. Sign in again to continue.", action: { label: "Sign in", href: "/login" } };
  }
  if (SERVER_WORDED.has(code)) return { message };
  // internal, 5xx and anything unknown: never show raw server text (07 §6.8).
  return { message: "Something went wrong. It's been logged.", ref: error.requestId };
}
