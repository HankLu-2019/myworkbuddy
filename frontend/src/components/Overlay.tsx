import { type ReactNode } from "react";
import * as RadixDialog from "@radix-ui/react-dialog";
import * as RadixPopover from "@radix-ui/react-popover";
import * as RadixMenu from "@radix-ui/react-dropdown-menu";
import * as RadixTooltip from "@radix-ui/react-tooltip";
import { X } from "lucide-react";

/** Focus is trapped while open and returned on close; Esc closes (07 §7). */
export function Dialog({
  open,
  onOpenChange,
  title,
  description,
  children,
  actions,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  description?: ReactNode;
  children?: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <RadixDialog.Root open={open} onOpenChange={onOpenChange}>
      <RadixDialog.Portal>
        <RadixDialog.Overlay className="overlay" />
        <RadixDialog.Content className="dialog">
          <RadixDialog.Title className="dialog-title">{title}</RadixDialog.Title>
          {description ? (
            <RadixDialog.Description asChild>
              <div>{description}</div>
            </RadixDialog.Description>
          ) : (
            <RadixDialog.Description className="visually-hidden">{title}</RadixDialog.Description>
          )}
          {children}
          {actions && <div className="dialog-actions">{actions}</div>}
        </RadixDialog.Content>
      </RadixDialog.Portal>
    </RadixDialog.Root>
  );
}

/** A dialog that names the object and the consequences, with an explicit verb (07 §6.7). */
export function ConfirmDialog({
  open,
  onOpenChange,
  title,
  body,
  confirmLabel,
  danger,
  pending,
  disabled,
  onConfirm,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  body: ReactNode;
  confirmLabel: string;
  danger?: boolean;
  pending?: boolean;
  /** Keep the confirm button disabled, e.g. until a name is typed. */
  disabled?: boolean;
  onConfirm: () => void;
}) {
  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      title={title}
      description={body}
      actions={
        <>
          <button type="button" className="btn btn-secondary" onClick={() => onOpenChange(false)}>
            Cancel
          </button>
          <button
            type="button"
            className={`btn ${danger ? "btn-danger" : "btn-primary"}`}
            disabled={pending || disabled}
            aria-busy={pending || undefined}
            onClick={onConfirm}
          >
            {confirmLabel}
          </button>
        </>
      }
    />
  );
}

export function Drawer({
  open,
  onOpenChange,
  title,
  side = "right",
  children,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  side?: "left" | "right";
  children: ReactNode;
}) {
  return (
    <RadixDialog.Root open={open} onOpenChange={onOpenChange}>
      <RadixDialog.Portal>
        <RadixDialog.Overlay className="overlay" />
        <RadixDialog.Content className={`drawer${side === "left" ? " drawer-left" : ""}`}>
          <div className="row">
            <RadixDialog.Title className="dialog-title grow">{title}</RadixDialog.Title>
            <RadixDialog.Close className="btn btn-ghost btn-icon" aria-label="Close">
              <X size={18} aria-hidden />
            </RadixDialog.Close>
          </div>
          <RadixDialog.Description className="visually-hidden">{title}</RadixDialog.Description>
          {children}
        </RadixDialog.Content>
      </RadixDialog.Portal>
    </RadixDialog.Root>
  );
}

export function Popover({
  trigger,
  children,
  label,
  open,
  onOpenChange,
}: {
  trigger: ReactNode;
  children: ReactNode;
  label: string;
  open?: boolean;
  onOpenChange?: (open: boolean) => void;
}) {
  return (
    <RadixPopover.Root open={open} onOpenChange={onOpenChange}>
      <RadixPopover.Trigger asChild>{trigger}</RadixPopover.Trigger>
      <RadixPopover.Portal>
        <RadixPopover.Content className="popover" aria-label={label} sideOffset={6} collisionPadding={8}>
          {children}
        </RadixPopover.Content>
      </RadixPopover.Portal>
    </RadixPopover.Root>
  );
}

export type MenuItem = { label: string; onSelect: () => void; icon?: ReactNode; danger?: boolean };

export function Menu({ trigger, items, label }: { trigger: ReactNode; items: MenuItem[]; label: string }) {
  return (
    <RadixMenu.Root>
      <RadixMenu.Trigger asChild>{trigger}</RadixMenu.Trigger>
      <RadixMenu.Portal>
        <RadixMenu.Content className="menu" aria-label={label} sideOffset={6} collisionPadding={8}>
          {items.map((item) => (
            <RadixMenu.Item key={item.label} className={`menu-item${item.danger ? " error-text" : ""}`} onSelect={item.onSelect}>
              {item.icon}
              {item.label}
            </RadixMenu.Item>
          ))}
        </RadixMenu.Content>
      </RadixMenu.Portal>
    </RadixMenu.Root>
  );
}

export function TooltipProvider({ children }: { children: ReactNode }) {
  return <RadixTooltip.Provider delayDuration={400}>{children}</RadixTooltip.Provider>;
}

/** Supplementary text only: the trigger must already have an accessible name. */
export function Tooltip({ content, children, side = "top" }: { content: string; children: ReactNode; side?: "top" | "right" | "bottom" | "left" }) {
  // Its own provider, so a tooltip works anywhere (including isolated tests); the app-level one sets the delay.
  return (
    <RadixTooltip.Provider delayDuration={400}>
      <RadixTooltip.Root>
        <RadixTooltip.Trigger asChild>{children}</RadixTooltip.Trigger>
        <RadixTooltip.Portal>
          <RadixTooltip.Content className="tooltip" side={side} sideOffset={6}>
            {content}
          </RadixTooltip.Content>
        </RadixTooltip.Portal>
      </RadixTooltip.Root>
    </RadixTooltip.Provider>
  );
}
