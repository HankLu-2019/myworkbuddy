import { useState, type FormEvent, type ReactNode } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Plus, Trash2 } from "lucide-react";
import { api, ApiError, type Plan, type PlanDoc, type Revision } from "../../api";
import { UnsavedGuard } from "../../app/UnsavedGuard";
import { Button, Card, ErrorNote, IconButton, Select, TextArea, TextField, useToast } from "../../components/ui";

type Row = Record<string, string>;

/** Rows of a list field: each row a few text inputs, with Add and Remove. */
function RowsEditor<T extends Row>({
  legend,
  rows,
  blank,
  onChange,
  fields,
}: {
  legend: string;
  rows: T[];
  blank: T;
  onChange: (rows: T[]) => void;
  fields: { key: keyof T & string; label: string; options?: readonly (readonly [string, string])[]; wide?: boolean }[];
}) {
  const set = (i: number, key: string, value: string) => onChange(rows.map((r, j) => (j === i ? { ...r, [key]: value } : r)));
  return (
    <fieldset className="stack">
      <legend className="field-label">{legend}</legend>
      {rows.map((row, i) => (
        <div key={i} className="row editor-row">
          {fields.map((f) =>
            f.options ? (
              <Select key={f.key} label={`${f.label} ${i + 1}`} options={f.options} value={row[f.key]} onChange={(e) => set(i, f.key, e.target.value)} />
            ) : (
              <TextField
                key={f.key}
                label={`${f.label} ${i + 1}`}
                className={f.wide ? "grow" : undefined}
                value={row[f.key]}
                onChange={(e) => set(i, f.key, e.target.value)}
                required
              />
            ),
          )}
          <IconButton label={`Remove ${legend.toLowerCase()} ${i + 1}`} icon={<Trash2 size={16} aria-hidden />} onClick={() => onChange(rows.filter((_, j) => j !== i))} />
        </div>
      ))}
      <div>
        <Button size="sm" icon={<Plus size={14} aria-hidden />} onClick={() => onChange([...rows, { ...blank }])}>
          Add
        </Button>
      </div>
    </fieldset>
  );
}

function Group({ title, children }: { title: string; children: ReactNode }) {
  return (
    <Card as="section" className="stack" aria-label={title}>
      <h2>{title}</h2>
      {children}
    </Card>
  );
}

const lines = (text: string) => text.split("\n").map((l) => l.trim()).filter(Boolean);

/** Edit a copy of a revision field by field; saving posts the whole document as a new, unapproved revision (03 §6). */
export function PlanEditor({ plan, revision, onDone }: { plan: Plan; revision: Revision; onDone: () => void }) {
  const qc = useQueryClient();
  const toast = useToast();
  const [doc, setDoc] = useState<PlanDoc>(() => structuredClone(revision.content));
  const [verification, setVerification] = useState(doc.verification.join("\n"));
  const [hosts, setHosts] = useState(doc.sandbox.optional_hosts.join(", "));
  const [note, setNote] = useState("");
  const [dirty, setDirty] = useState(false);
  const update = <K extends keyof PlanDoc>(key: K, value: PlanDoc[K]) => {
    setDoc((d) => ({ ...d, [key]: value }));
    setDirty(true);
  };
  const save = useMutation({
    mutationFn: () => {
      const content: PlanDoc = {
        ...doc,
        verification: lines(verification),
        sandbox: { ...doc.sandbox, optional_hosts: hosts.split(",").map((h) => h.trim()).filter(Boolean) },
      };
      return api<Plan>(`/api/plans/${plan.id}/revisions`, {
        method: "POST",
        json: { base_revision: revision.revision, content, change_note: note.trim() || null },
      });
    },
    onSuccess: (p) => {
      setDirty(false);
      qc.setQueryData(["plans", p.id], p);
      qc.invalidateQueries({ queryKey: ["plans", p.id] });
      qc.invalidateQueries({ queryKey: ["work"] });
      toast({ title: `Saved as revision ${p.current_revision}`, tone: "success" });
      onDone();
    },
  });
  const fieldError = save.error instanceof ApiError && save.error.code === "validation_error";
  return (
    <form
      className="stack"
      aria-label={`Edit revision ${revision.revision}`}
      onSubmit={(e: FormEvent) => {
        e.preventDefault();
        save.mutate();
      }}
      onChange={() => setDirty(true)}
    >
      <UnsavedGuard when={dirty && !save.isSuccess} what="your plan edits" />
      <p className="muted">
        You're editing a copy of revision {revision.revision}. Saving creates revision {plan.current_revision + 1}, checked like the
        planner's; it still needs approving.
      </p>
      <Group title="Summary">
        <TextField label="Title" value={doc.title} maxLength={200} onChange={(e) => update("title", e.target.value)} required />
        <TextArea label="Summary" rows={4} value={doc.summary} onChange={(e) => update("summary", e.target.value)} required />
        <Select
          label="Estimate"
          options={[["S", "S"], ["M", "M"], ["L", "L"], ["XL", "XL"]]}
          value={doc.estimate}
          onChange={(e) => update("estimate", e.target.value as PlanDoc["estimate"])}
        />
      </Group>
      <Group title="Questions and assumptions">
        <RowsEditor
          legend="Question"
          rows={doc.ambiguities}
          blank={{ question: "", assumption: "" }}
          onChange={(rows) => update("ambiguities", rows)}
          fields={[
            { key: "question", label: "Question", wide: true },
            { key: "assumption", label: "Assumption", wide: true },
          ]}
        />
      </Group>
      <Group title="Approach">
        <TextArea label="Approach" rows={8} value={doc.approach} onChange={(e) => update("approach", e.target.value)} required />
        <p className="muted small">Alternatives and knowledge references are kept as the planner wrote them.</p>
      </Group>
      <Group title="File changes">
        <RowsEditor
          legend="File change"
          rows={doc.changes}
          blank={{ path: "", action: "modify", description: "" }}
          onChange={(rows) => update("changes", rows as PlanDoc["changes"])}
          fields={[
            { key: "path", label: "Path", wide: true },
            { key: "action", label: "Action", options: [["create", "Create"], ["modify", "Modify"], ["delete", "Delete"]] },
            { key: "description", label: "What changes", wide: true },
          ]}
        />
      </Group>
      <Group title="Tests">
        <RowsEditor
          legend="Test"
          rows={doc.tests}
          blank={{ name: "", kind: "unit", description: "" }}
          onChange={(rows) => update("tests", rows as PlanDoc["tests"])}
          fields={[
            { key: "name", label: "Name", wide: true },
            { key: "kind", label: "Kind", options: [["unit", "Unit"], ["integration", "Integration"], ["e2e", "End to end"], ["manual", "Manual"]] },
            { key: "description", label: "Description", wide: true },
          ]}
        />
      </Group>
      <Group title="Verification and risks">
        <TextArea
          label="Verification commands"
          hint="One command per line. They run in the sandbox."
          rows={4}
          inputClassName="mono"
          value={verification}
          onChange={(e) => setVerification(e.target.value)}
          required
        />
        <RowsEditor
          legend="Risk"
          rows={doc.risks}
          blank={{ risk: "", mitigation: "" }}
          onChange={(rows) => update("risks", rows)}
          fields={[
            { key: "risk", label: "Risk", wide: true },
            { key: "mitigation", label: "Mitigation", wide: true },
          ]}
        />
        <TextArea label="Rollback" rows={3} value={doc.rollback} onChange={(e) => update("rollback", e.target.value)} required />
      </Group>
      <Group title="Sandbox and commit">
        <TextField
          label="Extra sandbox hosts"
          hint="Comma-separated. Only hosts an admin allows are accepted."
          value={hosts}
          onChange={(e) => setHosts(e.target.value)}
        />
        <TextArea label="Commit message" rows={3} inputClassName="mono" value={doc.commit_message} onChange={(e) => update("commit_message", e.target.value)} required />
      </Group>
      <TextField label="What you changed (optional)" maxLength={500} value={note} onChange={(e) => setNote(e.target.value)} />
      {fieldError && <p className="field-error">{(save.error as ApiError).message}</p>}
      {!fieldError && <ErrorNote error={save.error} />}
      <div className="row sticky-actions">
        <Button type="submit" variant="primary" pending={save.isPending}>
          Save as revision {plan.current_revision + 1}
        </Button>
        <Button onClick={onDone}>Cancel</Button>
      </div>
    </form>
  );
}
