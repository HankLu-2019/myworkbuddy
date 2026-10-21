import { useEffect } from "react";
import { useBlocker } from "react-router-dom";
import { ConfirmDialog } from "../components/ui";

let unloadAllowed = false;

/** Session expired: the app is about to reload into sign-in, and the draft is already saved, so don't ask. */
export function allowUnload() {
  unloadAllowed = true;
}

/** Asks before leaving a screen with unsaved changes (07 §6.6): in-app navigation and closing the tab. */
export function UnsavedGuard({ when, what = "your changes" }: { when: boolean; what?: string }) {
  const blocker = useBlocker(({ currentLocation, nextLocation }) => when && currentLocation.pathname !== nextLocation.pathname);
  useEffect(() => {
    if (!when) return;
    const onBeforeUnload = (e: BeforeUnloadEvent) => {
      if (unloadAllowed) return;
      e.preventDefault();
      e.returnValue = "";
    };
    window.addEventListener("beforeunload", onBeforeUnload);
    return () => window.removeEventListener("beforeunload", onBeforeUnload);
  }, [when]);
  return (
    <ConfirmDialog
      open={blocker.state === "blocked"}
      onOpenChange={(open) => !open && blocker.state === "blocked" && blocker.reset()}
      title="Leave without saving?"
      body={`You haven't saved ${what}. A draft stays on this device, but nobody else sees it.`}
      confirmLabel="Leave"
      danger
      onConfirm={() => blocker.state === "blocked" && blocker.proceed()}
    />
  );
}
