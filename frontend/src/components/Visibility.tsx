import { Lock, Users } from "lucide-react";
import type { Visibility } from "../api";
import { Button } from "./Button";

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

/** "Share with team" / "Make private" for an owned item (07 §6.2). The per-type wording is passed in;
    confirmations with undo arrive in H16. */
export function VisibilityControl({
  visibility,
  onChange,
  shareLabel = "Share with team",
  unshareLabel = "Make private",
  pending,
  size = "md",
}: {
  visibility: Visibility | string;
  onChange: (next: Visibility) => void;
  shareLabel?: string;
  unshareLabel?: string;
  pending?: boolean;
  size?: "md" | "sm";
}) {
  const isPublic = visibility === "public";
  return (
    <Button
      size={size}
      pending={pending}
      icon={isPublic ? <Lock size={14} aria-hidden /> : <Users size={14} aria-hidden />}
      onClick={() => onChange(isPublic ? "private" : "public")}
    >
      {isPublic ? unshareLabel : shareLabel}
    </Button>
  );
}
