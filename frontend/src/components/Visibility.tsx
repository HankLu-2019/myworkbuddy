import { useState, type ReactNode } from "react";
import { Lock, Users } from "lucide-react";
import type { Visibility } from "../api";
import { Button } from "./Button";
import { ConfirmDialog } from "./Overlay";
import { useToast } from "./Toast";

export function VisibilityBadge({ visibility }: { visibility: Visibility | string }) {
  return visibility === "public" ? (
    <span className="badge badge-public" title="Visible to the whole team">
      <Users size={12} aria-hidden />
      Team
    </span>
  ) : (
    <span className="badge badge-private" title="Only you can see this">
      <Lock size={12} aria-hidden />
      Private
    </span>
  );
}

export type SharedItem = "document" | "repository" | "page" | "chat";

/** What becomes visible, per item type (07 §5.4). */
export function shareConsequence(item: SharedItem, name: string): string {
  switch (item) {
    case "document":
      return `“${name}” becomes searchable by everyone on the team. Only you can change or delete it.`;
    case "repository":
      return `Your code index and repo docs for ${name} become searchable by everyone. Your project card stays private unless you share it separately.`;
    case "page":
      return `${name} moves to the team wiki, where everyone can read it. Only you and admins can edit it.`;
    case "chat":
      return "Teammates can read this chat. Only you can add to it.";
  }
}

const NOUN: Record<SharedItem, string> = { document: "document", repository: "code index", page: "page", chat: "chat" };

/**
 * "Share with team" asks first and says what becomes visible; "Make private" doesn't. Either way a toast offers
 * Undo for 10 s (07 §5.4, §6.7). Admins see "Remove from team space" on other people's shared items.
 */
export function VisibilityControl({
  visibility,
  onChange,
  item,
  name,
  mine = true,
  shareLabel = "Share with team",
  unshareLabel,
  pending,
  size = "md",
}: {
  visibility: Visibility | string;
  /** Resolve when the change is saved; reject to keep the old state. */
  onChange: (next: Visibility) => Promise<unknown> | void;
  item: SharedItem;
  name: string;
  mine?: boolean;
  shareLabel?: string;
  unshareLabel?: string;
  pending?: boolean;
  size?: "md" | "sm";
}) {
  const toast = useToast();
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const isPublic = visibility === "public";
  const offLabel = unshareLabel ?? (mine ? "Make private" : "Remove from team space");

  async function change(next: Visibility, undoable = true) {
    setBusy(true);
    try {
      await onChange(next);
    } catch {
      return; // the caller shows the error
    } finally {
      setBusy(false);
    }
    if (!undoable) return;
    const what: ReactNode = next === "public" ? `Shared ${NOUN[item]} with the team` : `Made ${NOUN[item]} private`;
    toast({
      title: String(what),
      description: name,
      duration: 10_000,
      action: mine ? { label: "Undo", onClick: () => void change(next === "public" ? "private" : "public", false) } : undefined,
    });
  }

  return (
    <>
      <Button
        size={size}
        pending={pending || busy}
        icon={isPublic ? <Lock size={14} aria-hidden /> : <Users size={14} aria-hidden />}
        onClick={() => (isPublic ? void change("private") : setConfirming(true))}
      >
        {isPublic ? offLabel : shareLabel}
      </Button>
      <ConfirmDialog
        open={confirming}
        onOpenChange={setConfirming}
        title={`Share ${name} with the team?`}
        body={shareConsequence(item, name)}
        confirmLabel="Share with team"
        pending={busy}
        onConfirm={() => {
          setConfirming(false);
          void change("public");
        }}
      />
    </>
  );
}
