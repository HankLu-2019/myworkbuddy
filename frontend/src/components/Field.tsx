import { forwardRef, useId, type InputHTMLAttributes, type ReactNode, type SelectHTMLAttributes, type TextareaHTMLAttributes } from "react";
import * as RadixCheckbox from "@radix-ui/react-checkbox";
import * as RadixSwitch from "@radix-ui/react-switch";
import { Check } from "lucide-react";

interface FieldBase {
  label: string;
  /** Keep the label for screen readers but don't show it (compact toolbars). */
  hideLabel?: boolean;
  hint?: ReactNode;
  error?: string | null;
  className?: string;
}

/** Label, control, hint and error wired together with ids and aria-describedby (07 §7). */
function FieldShell({
  id,
  label,
  hideLabel,
  hint,
  error,
  className,
  children,
}: FieldBase & { id: string; children: ReactNode }) {
  return (
    <div className={["field", className].filter(Boolean).join(" ")}>
      <label htmlFor={id} className={hideLabel ? "visually-hidden" : "field-label"}>
        {label}
      </label>
      {children}
      {hint && (
        <div id={`${id}-hint`} className="field-hint">
          {hint}
        </div>
      )}
      {error && (
        <div id={`${id}-error`} className="field-error" role="alert">
          {error}
        </div>
      )}
    </div>
  );
}

function describedBy(id: string, hint: unknown, error: unknown, extra?: string) {
  return [hint && `${id}-hint`, error && `${id}-error`, extra].filter(Boolean).join(" ") || undefined;
}

export type TextFieldProps = FieldBase & Omit<InputHTMLAttributes<HTMLInputElement>, "className"> & { inputClassName?: string };

export const TextField = forwardRef<HTMLInputElement, TextFieldProps>(function TextField(
  { label, hideLabel, hint, error, className, inputClassName, id, "aria-describedby": extra, ...input },
  ref,
) {
  const auto = useId();
  const fid = id ?? auto;
  return (
    <FieldShell id={fid} label={label} hideLabel={hideLabel} hint={hint} error={error} className={className}>
      <input
        ref={ref}
        id={fid}
        className={inputClassName}
        aria-invalid={error ? true : undefined}
        aria-describedby={describedBy(fid, hint, error, extra)}
        {...input}
      />
    </FieldShell>
  );
});

export type TextAreaProps = FieldBase & Omit<TextareaHTMLAttributes<HTMLTextAreaElement>, "className"> & { inputClassName?: string };

export const TextArea = forwardRef<HTMLTextAreaElement, TextAreaProps>(function TextArea(
  { label, hideLabel, hint, error, className, inputClassName, id, "aria-describedby": extra, ...input },
  ref,
) {
  const auto = useId();
  const fid = id ?? auto;
  return (
    <FieldShell id={fid} label={label} hideLabel={hideLabel} hint={hint} error={error} className={className}>
      <textarea
        ref={ref}
        id={fid}
        className={inputClassName}
        aria-invalid={error ? true : undefined}
        aria-describedby={describedBy(fid, hint, error, extra)}
        {...input}
      />
    </FieldShell>
  );
});

export type SelectProps = FieldBase &
  Omit<SelectHTMLAttributes<HTMLSelectElement>, "className"> & { options: readonly (readonly [string, string])[] };

export function Select({ label, hideLabel, hint, error, className, id, options, ...select }: SelectProps) {
  const auto = useId();
  const fid = id ?? auto;
  return (
    <FieldShell id={fid} label={label} hideLabel={hideLabel} hint={hint} error={error} className={className}>
      <select id={fid} aria-invalid={error ? true : undefined} aria-describedby={describedBy(fid, hint, error)} {...select}>
        {options.map(([value, text]) => (
          <option key={value} value={value}>
            {text}
          </option>
        ))}
      </select>
    </FieldShell>
  );
}

export function Checkbox({
  label,
  checked,
  onChange,
  disabled,
}: {
  label: string;
  checked: boolean;
  onChange: (checked: boolean) => void;
  disabled?: boolean;
}) {
  const id = useId();
  return (
    <div className="checkbox">
      <RadixCheckbox.Root id={id} className="checkbox-box" checked={checked} disabled={disabled} onCheckedChange={(v) => onChange(v === true)}>
        <RadixCheckbox.Indicator>
          <Check size={14} aria-hidden />
        </RadixCheckbox.Indicator>
      </RadixCheckbox.Root>
      <label htmlFor={id}>{label}</label>
    </div>
  );
}

export function Toggle({
  label,
  checked,
  onChange,
  disabled,
}: {
  label: string;
  checked: boolean;
  onChange: (checked: boolean) => void;
  disabled?: boolean;
}) {
  const id = useId();
  return (
    <div className="checkbox">
      <RadixSwitch.Root id={id} className="toggle" checked={checked} disabled={disabled} onCheckedChange={onChange}>
        <RadixSwitch.Thumb className="toggle-thumb" />
      </RadixSwitch.Root>
      <label htmlFor={id}>{label}</label>
    </div>
  );
}

export function RadioGroup<T extends string>({
  legend,
  value,
  options,
  onChange,
}: {
  legend: string;
  value: T;
  options: readonly (readonly [T, string])[];
  onChange: (value: T) => void;
}) {
  const name = useId();
  return (
    <fieldset className="radio-group">
      <legend className="field-label">{legend}</legend>
      {options.map(([v, text]) => (
        <label key={v} className="checkbox">
          <input type="radio" name={name} value={v} checked={value === v} onChange={() => onChange(v)} />
          {text}
        </label>
      ))}
    </fieldset>
  );
}
