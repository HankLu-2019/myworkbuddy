import { useEffect, useRef } from "react";

export type ShortcutHandlers = {
  focusSearch: () => void;
  newChat: () => void;
  go: (where: "home" | "inbox" | "wiki" | "board") => void;
  help: () => void;
};

/** True while the user types somewhere: shortcuts must not fire then (07 §6.11). */
export function isTyping(target: EventTarget | null): boolean {
  const el = target as HTMLElement | null;
  if (!el) return false;
  const tag = el.tagName;
  return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || el.isContentEditable === true;
}

const GO: Record<string, "home" | "inbox" | "wiki" | "board"> = { h: "home", i: "inbox", w: "wiki", b: "board" };

/** `/` search · `c` new chat · `g` then `h`/`i`/`w`/`b` · `?` help. Esc and form keys are handled by the controls. */
export function useShortcuts(handlers: ShortcutHandlers) {
  const ref = useRef(handlers);
  ref.current = handlers;
  useEffect(() => {
    let pendingG = 0;
    function onKey(e: KeyboardEvent) {
      if (e.defaultPrevented || e.metaKey || e.ctrlKey || e.altKey || isTyping(e.target)) return;
      const h = ref.current;
      if (pendingG && Date.now() - pendingG < 1500 && GO[e.key]) {
        e.preventDefault();
        pendingG = 0;
        h.go(GO[e.key]);
        return;
      }
      pendingG = 0;
      if (e.key === "/") {
        e.preventDefault();
        h.focusSearch();
      } else if (e.key === "c") {
        e.preventDefault();
        h.newChat();
      } else if (e.key === "g") {
        pendingG = Date.now();
      } else if (e.key === "?") {
        e.preventDefault();
        h.help();
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);
}

export const SHORTCUTS: [string, string][] = [
  ["/", "Search everything"],
  ["c", "New chat"],
  ["g then w", "Go to Wiki"],
  ["g then h", "Go Home"],
  ["Esc", "Close a dialog or drawer"],
  ["Enter / Shift+Enter", "Send a message / new line"],
  ["?", "Show these shortcuts"],
];
