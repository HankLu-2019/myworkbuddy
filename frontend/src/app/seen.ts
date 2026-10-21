/** When the user last looked at librarian changes, for the Wiki dot (07 §5.5). Per browser. */
const key = (userId: number | undefined) => `mwb:librarian-seen:${userId ?? "anon"}`;

export function markLibrarianSeen(userId: number | undefined) {
  try {
    localStorage.setItem(key(userId), new Date().toISOString());
  } catch {
    // not kept
  }
}

export function librarianUnseen(userId: number | undefined, changedAt: string | null | undefined): boolean {
  if (!changedAt) return false;
  try {
    const seen = localStorage.getItem(key(userId));
    return !seen || new Date(seen) < new Date(changedAt);
  } catch {
    return false;
  }
}
