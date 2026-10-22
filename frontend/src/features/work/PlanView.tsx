import type { ReactNode } from "react";
import { FilePlus2, FileX2, FilePen, AlertTriangle, XCircle } from "lucide-react";
import type { FieldDiff, PlanDoc, PlanValidation } from "../../api";
import { Card, Checkbox, CodeBlock, DataTable, KeyValue, Markdown, type Column } from "../../components/ui";
import { ackLabel } from "./states";

/** Plan sections in reading order (07 §4.10), with the PlanDoc fields each one shows. */
export const SECTIONS: readonly (readonly [string, string, readonly string[]])[] = [
  ["summary", "Summary", ["title", "summary", "estimate"]],
  ["ambiguities", "Questions and assumptions", ["ambiguities"]],
  ["approach", "Approach and alternatives", ["approach", "alternatives", "knowledge_refs"]],
  ["changes", "File changes", ["changes"]],
  ["tests", "Tests", ["tests"]],
  ["verification", "Verification commands", ["verification"]],
  ["risks", "Risks", ["risks"]],
  ["rollback", "Rollback", ["rollback"]],
  ["sandbox", "Sandbox request", ["sandbox"]],
  ["commit_message", "Commit message", ["commit_message"]],
];

/** The section a validation issue or diff entry belongs to, by its top-level field. */
export function sectionOf(field: string): string {
  const top = field.split(/[.[]/, 1)[0];
  return SECTIONS.find(([, , fields]) => fields.includes(top))?.[0] ?? "summary";
}

export const SECTION_LABEL: Record<string, string> = Object.fromEntries(SECTIONS.map(([id, label]) => [id, label]));

function Section({ id, title, changed, children }: { id: string; title: string; changed?: boolean; children: ReactNode }) {
  return (
    <Card as="section" className={`stack plan-section${changed ? " changed" : ""}`} aria-labelledby={`plan-${id}-title`} id={`plan-${id}`}>
      <h2 id={`plan-${id}-title`}>
        {title}
        {changed && <span className="pill st-needs plan-changed">Changed</span>}
      </h2>
      {children}
    </Card>
  );
}

const ACTION_ICON = { create: FilePlus2, modify: FilePen, delete: FileX2 } as const;
const ACTION_LABEL = { create: "Create", modify: "Modify", delete: "Delete" } as const;

/** The plan's sections (07 §4.10). `problems` marks fields with validation errors; `changed` marks sections that
    differ from the previous revision. */
export function PlanSections({ doc, problems, changed }: { doc: PlanDoc; problems: Map<string, string>; changed: Set<string> }) {
  type Change = PlanDoc["changes"][number] & { index: number };
  const columns: Column<Change>[] = [
    {
      key: "action",
      header: "Action",
      cell: (c) => {
        const Icon = ACTION_ICON[c.action];
        return (
          <span className="row nowrap">
            <Icon size={14} aria-hidden /> {ACTION_LABEL[c.action]}
          </span>
        );
      },
    },
    {
      key: "path",
      header: "Path",
      cell: (c) => {
        const problem = problems.get(`changes[${c.index}].path`);
        return (
          <span className={problem ? "mono path-problem" : "mono"}>
            {c.path}
            {problem && <span className="field-error small"> {problem}</span>}
          </span>
        );
      },
    },
    { key: "description", header: "What changes", cell: (c) => c.description },
  ];
  return (
    <div className="stack">
      <Section id="summary" title="Summary" changed={changed.has("summary")}>
        <h3 className="plan-title">{doc.title}</h3>
        <Markdown>{doc.summary}</Markdown>
        <p className="muted small">Estimate: {doc.estimate}</p>
      </Section>
      <Section id="ambiguities" title="Questions and assumptions" changed={changed.has("ambiguities")}>
        {doc.ambiguities.length ? (
          <ul className="plain-list stack">
            {doc.ambiguities.map((a) => (
              <li key={a.question}>
                <strong>{a.question}</strong>
                <div>Assuming: {a.assumption}</div>
              </li>
            ))}
          </ul>
        ) : (
          <p className="muted">The planner had no open questions.</p>
        )}
        <p className="muted small">To answer a question, write the answer in Revise with feedback.</p>
      </Section>
      <Section id="approach" title="Approach and alternatives" changed={changed.has("approach")}>
        <Markdown>{doc.approach}</Markdown>
        {doc.alternatives.map((alt) => (
          <div key={alt.name} className="stack-tight">
            <strong>Alternative: {alt.name}</strong>
            <span className="small">Pros: {alt.pros.join("; ") || "—"}</span>
            <span className="small">Cons: {alt.cons.join("; ") || "—"}</span>
          </div>
        ))}
        {doc.knowledge_refs.length > 0 && <p className="small muted">Knowledge used: documents {doc.knowledge_refs.join(", ")}</p>}
      </Section>
      <Section id="changes" title="File changes" changed={changed.has("changes")}>
        <DataTable label="File changes" columns={columns} rows={doc.changes.map((c, index) => ({ ...c, index }))} rowKey={(c) => c.index} rowLabel={(c) => c.path} />
      </Section>
      <Section id="tests" title="Tests" changed={changed.has("tests")}>
        {doc.tests.length ? (
          <ul className="plain-list stack">
            {doc.tests.map((t) => (
              <li key={t.name}>
                <span className="mono">{t.name}</span> <span className="muted small">({t.kind})</span>
                <div>{t.description}</div>
              </li>
            ))}
          </ul>
        ) : (
          <p className="muted">No tests planned.</p>
        )}
      </Section>
      <Section id="verification" title="Verification commands" changed={changed.has("verification")}>
        <CodeBlock label="Verification commands">{doc.verification.join("\n")}</CodeBlock>
        <p className="muted small">Runs in the sandbox.</p>
      </Section>
      <Section id="risks" title="Risks" changed={changed.has("risks")}>
        {doc.risks.length ? (
          <ul className="plain-list stack">
            {doc.risks.map((r) => (
              <li key={r.risk}>
                <strong>{r.risk}</strong>
                <div>Mitigation: {r.mitigation}</div>
              </li>
            ))}
          </ul>
        ) : (
          <p className="muted">No risks listed.</p>
        )}
      </Section>
      <Section id="rollback" title="Rollback" changed={changed.has("rollback")}>
        <Markdown>{doc.rollback}</Markdown>
      </Section>
      <Section id="sandbox" title="Sandbox request" changed={changed.has("sandbox")}>
        <KeyValue
          items={[
            ["Extra hosts", doc.sandbox.optional_hosts.length ? doc.sandbox.optional_hosts.join(", ") : "None"],
            ["CPUs", doc.sandbox.cpus],
            ["Memory", `${doc.sandbox.memory_gb} GB`],
            ["Time limit", `${doc.sandbox.timeout_min} min`],
            ["Attempts", doc.sandbox.max_attempts],
          ]}
        />
      </Section>
      <Section id="commit_message" title="Commit message" changed={changed.has("commit_message")}>
        <CodeBlock label="Commit message">{doc.commit_message}</CodeBlock>
      </Section>
    </div>
  );
}

/** Errors (with links to their section) and warnings, with checkboxes for those that need acknowledging (07 §4.10). */
export function ValidationPanel({
  validation,
  acks,
  onAck,
  readOnly,
}: {
  validation: PlanValidation;
  acks: Set<string>;
  onAck: (key: string, on: boolean) => void;
  readOnly?: boolean;
}) {
  const errors = validation.issues.filter((i) => i.level === "error");
  const warnings = validation.issues.filter((i) => i.level === "warning");
  if (!errors.length && !warnings.length && !validation.needs_ack.length)
    return <p className="muted small" role="status">Validation passed.</p>;
  return (
    <Card as="section" className="stack validation" aria-labelledby="validation-title">
      <h2 id="validation-title">Validation</h2>
      {errors.length > 0 && (
        <ul className="plain-list stack-tight" aria-label="Errors">
          {errors.map((e, i) => (
            <li key={i} className="problem-text">
              <XCircle size={14} aria-label="Error" /> <a href={`#plan-${sectionOf(e.field)}`}>{SECTION_LABEL[sectionOf(e.field)]}</a>: {e.message}
            </li>
          ))}
        </ul>
      )}
      {warnings.length > 0 && (
        <ul className="plain-list stack-tight" aria-label="Warnings">
          {warnings.map((w, i) => (
            <li key={i} className="warn-text">
              <AlertTriangle size={14} aria-label="Warning" /> <a href={`#plan-${sectionOf(w.field)}`}>{SECTION_LABEL[sectionOf(w.field)]}</a>: {w.message}
            </li>
          ))}
        </ul>
      )}
      {validation.needs_ack.length > 0 && (
        <fieldset className="stack-tight">
          <legend className="field-label">Acknowledge before approving</legend>
          {validation.needs_ack.map((key) => (
            <Checkbox key={key} label={ackLabel(key)} checked={acks.has(key)} disabled={readOnly} onChange={(on) => onAck(key, on)} />
          ))}
        </fieldset>
      )}
    </Card>
  );
}

function show(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "string") return value;
  return JSON.stringify(value, null, 1);
}

const KIND_LABEL = { added: "Added", removed: "Removed", changed: "Changed", reordered: "Reordered" } as const;

/** A field-level diff between two revisions, as the server computed it (03 §8). */
export function PlanDiffList({ fields }: { fields: FieldDiff[] }) {
  if (!fields.length) return <p className="muted">The two revisions are the same.</p>;
  return (
    <ul className="plain-list stack plan-diff" aria-label="Differences">
      {fields.map((f, i) => (
        <li key={i} className="stack-tight">
          <div>
            <strong>{SECTION_LABEL[sectionOf(f.field)]}</strong>{" "}
            <span className="muted small">
              {f.field}
              {f.key ? ` · ${f.key}` : ""} · {KIND_LABEL[f.kind]}
            </span>
          </div>
          {f.kind !== "added" && f.before !== null && f.before !== undefined && (
            <pre className="diff">
              <div className="del">- {show(f.before)}</div>
            </pre>
          )}
          {f.kind !== "removed" && f.after !== null && f.after !== undefined && (
            <pre className="diff">
              <div className="add">+ {show(f.after)}</div>
            </pre>
          )}
        </li>
      ))}
    </ul>
  );
}
