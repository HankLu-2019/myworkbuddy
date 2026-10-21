import { forwardRef, type ButtonHTMLAttributes, type ReactNode } from "react";
import { Loader2 } from "lucide-react";
import { Tooltip } from "./Overlay";

export type ButtonVariant = "primary" | "secondary" | "danger" | "ghost" | "link";

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  size?: "md" | "sm";
  /** Shows a spinner and disables the button until the request finishes (07 §6.6: no double submit). */
  pending?: boolean;
  icon?: ReactNode;
}

export function buttonClass(variant: ButtonVariant = "secondary", size: "md" | "sm" = "md", extra?: string) {
  return ["btn", `btn-${variant}`, size === "sm" && "btn-sm", extra].filter(Boolean).join(" ");
}

/** The only button in the app. Defaults to type="button" so it never submits a form by accident. */
export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  { variant = "secondary", size = "md", pending = false, icon, className, disabled, children, type = "button", ...rest },
  ref,
) {
  return (
    <button
      ref={ref}
      type={type}
      className={buttonClass(variant, size, className)}
      disabled={disabled || pending}
      aria-busy={pending || undefined}
      {...rest}
    >
      {pending ? <Loader2 size={16} className="spin" aria-hidden /> : icon}
      {children !== undefined && <span className="btn-label">{children}</span>}
    </button>
  );
});

export interface IconButtonProps extends Omit<ButtonProps, "children" | "icon"> {
  /** Accessible name; also shown as a tooltip. */
  label: string;
  icon: ReactNode;
}

export const IconButton = forwardRef<HTMLButtonElement, IconButtonProps>(function IconButton(
  { label, icon, variant = "ghost", className, ...rest },
  ref,
) {
  return (
    <Tooltip content={label}>
      <Button ref={ref} variant={variant} aria-label={label} icon={icon} className={["btn-icon", className].filter(Boolean).join(" ")} {...rest} />
    </Tooltip>
  );
});

/** A button that opens a file picker (the input stays accessible by its label). */
export function FileButton({
  label,
  accept,
  multiple,
  onFiles,
  variant = "primary",
  inputRef,
  icon,
}: {
  label: string;
  accept?: string;
  multiple?: boolean;
  onFiles: (files: FileList) => void;
  variant?: ButtonVariant;
  inputRef?: React.Ref<HTMLInputElement>;
  icon?: ReactNode;
}) {
  return (
    <label className={buttonClass(variant)}>
      {icon}
      <span className="btn-label">{label}</span>
      <input
        ref={inputRef}
        type="file"
        className="visually-hidden"
        accept={accept}
        multiple={multiple}
        onChange={(e) => e.target.files?.length && onFiles(e.target.files)}
      />
    </label>
  );
}
