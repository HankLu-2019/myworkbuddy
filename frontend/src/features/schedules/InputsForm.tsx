import { TextArea, TextField } from "../../components/ui";

/** The subset of JSON Schema our agents use for inputs (app/agents/seed/*.md). */
export type InputsSchema = {
  type?: string;
  required?: string[];
  properties?: Record<string, PropSchema>;
};
type PropSchema = {
  title?: string;
  description?: string;
  type?: string;
  default?: unknown;
  maxLength?: number;
  items?: { type?: string };
};

export type Inputs = Record<string, unknown>;
/** What the form edits: one text per input; lists are one item per line. */
export type InputTexts = Record<string, string>;

export function toTexts(inputs: Inputs): InputTexts {
  return Object.fromEntries(
    Object.entries(inputs).map(([k, v]) => [k, Array.isArray(v) ? v.join("\n") : v == null ? "" : String(v)]),
  );
}

/** The inputs to send: lists split by line, empty optional values left out (the server checks the schema). */
export function toInputs(schema: InputsSchema, texts: InputTexts): Inputs {
  const out: Inputs = {};
  for (const [key, prop] of Object.entries(schema.properties ?? {})) {
    const text = texts[key] ?? "";
    const value = prop.type === "array" ? text.split("\n").map((s) => s.trim()).filter(Boolean) : text.trim();
    const empty = value === "" || (Array.isArray(value) && value.length === 0);
    if (!empty || schema.required?.includes(key)) out[key] = value;
  }
  return out;
}

/** A form generated from the agent's inputs schema (07 §4.7). `errors` maps "inputs.<name>" to a message. */
export function InputsForm({
  schema,
  value,
  onChange,
  errors,
}: {
  schema: InputsSchema;
  value: InputTexts;
  onChange: (next: InputTexts) => void;
  errors: Record<string, string>;
}) {
  const props = Object.entries(schema.properties ?? {});
  if (!props.length) return <p className="muted">This check type has no settings.</p>;
  return (
    <div className="stack">
      {props.map(([name, prop]) => {
        const required = schema.required?.includes(name);
        const label = `${prop.title ?? name}${required ? "" : " (optional)"}`;
        const error = errors[`inputs.${name}`] ?? null;
        const set = (text: string) => onChange({ ...value, [name]: text });
        if (prop.type === "array" || (prop.maxLength ?? 0) > 200) {
          return (
            <TextArea
              key={name}
              label={label}
              hint={[prop.description, prop.type === "array" ? "One per line." : ""].filter(Boolean).join(" ")}
              value={value[name] ?? ""}
              onChange={(e) => set(e.target.value)}
              rows={prop.type === "array" ? 3 : 2}
              error={error}
              required={required}
            />
          );
        }
        return (
          <TextField
            key={name}
            label={label}
            hint={prop.description}
            value={value[name] ?? (prop.default === undefined ? "" : String(prop.default))}
            onChange={(e) => set(e.target.value)}
            error={error}
            required={required}
          />
        );
      })}
    </div>
  );
}
