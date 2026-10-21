import { useCallback, useEffect, useRef, useState } from "react";

/** Unsent text kept in localStorage (07 §6.6), so a reload or an expired session doesn't lose it.
    Key: draft:<user>:<screen>:<id>. Storage can be missing or full (private windows); then drafts just
    aren't kept. */
export function draftKey(userId: number | undefined, screen: string, id: string | number): string {
  return `draft:${userId ?? "anon"}:${screen}:${id}`;
}

export function readDraft(key: string): string | null {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

export function writeDraft(key: string, value: string) {
  try {
    if (value) localStorage.setItem(key, value);
    else localStorage.removeItem(key);
  } catch {
    // storage unavailable or full: nothing to keep
  }
}

/**
 * State backed by a draft. `restored` is true when the initial value came from storage, so the screen can say
 * "Restored your unsaved text". `clear()` after a successful submit.
 */
export function useDraft(key: string | null, initial = "") {
  const [value, setValue] = useState(() => (key && readDraft(key)) || initial);
  const [restored, setRestored] = useState(() => !!(key && readDraft(key)));
  const keyRef = useRef(key);

  // A new key (another session or page): load its draft.
  useEffect(() => {
    if (keyRef.current === key) return;
    keyRef.current = key;
    const saved = key ? readDraft(key) : null;
    setValue(saved || initial);
    setRestored(!!saved);
    // initial is only the fallback for a fresh key
  }, [key]);

  const update = useCallback(
    (next: string) => {
      setValue(next);
      if (key) writeDraft(key, next);
    },
    [key],
  );
  const clear = useCallback(() => {
    setValue("");
    setRestored(false);
    if (key) writeDraft(key, "");
  }, [key]);
  return { value, setValue: update, clear, restored, acknowledge: () => setRestored(false) };
}
