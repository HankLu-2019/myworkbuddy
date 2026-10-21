import { useState, type ReactNode } from "react";
import { Navigate } from "react-router-dom";
import { MoreHorizontal, Plus, Trash2 } from "lucide-react";
import { useAuth } from "../../auth";
import { usePageTitle } from "../../app/pageTitle";
import {
  Banner,
  Button,
  Card,
  Checkbox,
  CodeBlock,
  ConfirmDialog,
  CountBadge,
  DataTable,
  Dialog,
  DiffView,
  Dot,
  Drawer,
  EmptyState,
  ErrorNote,
  IconButton,
  KeyValue,
  Markdown,
  Menu,
  PageHeader,
  Popover,
  RadioGroup,
  Select,
  Skeleton,
  SkeletonRows,
  StatusPill,
  Tabs,
  TextArea,
  TextField,
  Toggle,
  VisibilityBadge,
  VisibilityControl,
  useToast,
} from "../../components/ui";
import type { Visibility } from "../../api";

/** One status per visual state (07 §6.1), so the baselines cover all six. */
const STATUSES = ["queued", "running", "needs_decision", "ready", "failed", "cancelled"] as const;

type Row = { id: number; name: string; status: string; chunks: number };
const ROWS: Row[] = [
  { id: 1, name: "payments-api", status: "ready", chunks: 1240 },
  { id: 2, name: "billing-worker", status: "running", chunks: 312 },
  { id: 3, name: "legacy-gateway", status: "failed", chunks: 0 },
];

const SAMPLE_MD = `### Markdown

Text with **bold**, \`code\` and a [relative link](/wiki).

- one
- two

\`\`\`python
def answer() -> int:
    return 42
\`\`\``;

const SAMPLE_DIFF = `--- a/notes/oom.md
+++ b/notes/oom.md
@@ -1,3 +1,3 @@
 # Spark OOM
-Raise executor memory.
+Raise executor memory overhead, not heap.
 See SPIN-4412.`;

function Section({ title, children }: { title: string; children: ReactNode }) {
  const id = `g-${title.toLowerCase().replace(/\W+/g, "-")}`;
  return (
    <Card as="section" aria-labelledby={id} className="stack">
      <h2 id={id}>{title}</h2>
      {children}
    </Card>
  );
}

/**
 * Every shared component in every state (07 §8). Dev builds show it to everyone; production builds only to
 * admins, so the Playwright visual baselines and axe checks run against the real production bundle.
 */
export default function GalleryPage() {
  const { user } = useAuth();
  const toast = useToast();
  usePageTitle("Components");
  const [dialog, setDialog] = useState(false);
  const [confirm, setConfirm] = useState(false);
  const [drawer, setDrawer] = useState(false);
  const [visibility, setVisibility] = useState<Visibility>("private");
  const [checked, setChecked] = useState(true);
  const [toggle, setToggle] = useState(false);
  const [radio, setRadio] = useState<"all" | "mine">("all");
  const [tab, setTab] = useState("one");

  if (!import.meta.env.DEV && user?.role !== "admin") return <Navigate to="/chat" replace />;

  return (
    <div className="page gallery">
      <PageHeader title="Components">
        <Button variant="primary" icon={<Plus size={16} aria-hidden />}>
          Header action
        </Button>
      </PageHeader>

      <Section title="Buttons">
        <div className="row">
          <Button variant="primary">Primary</Button>
          <Button>Secondary</Button>
          <Button variant="danger" icon={<Trash2 size={16} aria-hidden />}>
            Danger
          </Button>
          <Button variant="ghost">Ghost</Button>
          <Button variant="link">Link</Button>
          <Button pending>Pending</Button>
          <Button disabled>Disabled</Button>
          <Button size="sm">Small</Button>
          <IconButton label="More actions" icon={<MoreHorizontal size={16} aria-hidden />} />
        </div>
      </Section>

      <Section title="Fields">
        <div className="form-grid">
          <TextField label="Text field" placeholder="Placeholder" hint="A hint under the field." />
          <TextField label="With an error" defaultValue="bad value" error="That doesn't look right." />
          <Select
            label="Select"
            options={[
              ["a", "Option A"],
              ["b", "Option B"],
            ]}
          />
          <TextField label="Disabled field" disabled defaultValue="Can't change this" />
        </div>
        <TextArea label="Text area" rows={3} defaultValue="Several lines of text." />
        <div className="row">
          <Checkbox label="Checkbox" checked={checked} onChange={setChecked} />
          <Toggle label="Toggle" checked={toggle} onChange={setToggle} />
        </div>
        <RadioGroup
          legend="Radio group"
          value={radio}
          onChange={setRadio}
          options={[
            ["all", "Everything"],
            ["mine", "Only mine"],
          ]}
        />
      </Section>

      <Section title="Status and visibility">
        <div className="row">
          {STATUSES.map((s) => (
            <StatusPill key={s} status={s} />
          ))}
        </div>
        <div className="row">
          <VisibilityBadge visibility="public" />
          <VisibilityBadge visibility="private" />
          <VisibilityControl visibility={visibility} item="document" name="notes.md" onChange={setVisibility} />
          <CountBadge count={3} label="3 failed" />
          <Dot label="Needs attention" />
        </div>
      </Section>

      <Section title="Feedback">
        <Banner tone="info">An informational banner.</Banner>
        <Banner tone="success">Saved.</Banner>
        <Banner tone="warning">Something needs a look.</Banner>
        <Banner tone="danger">Something went wrong.</Banner>
        <ErrorNote error="An inline error message." />
        <EmptyState action={<Button variant="primary">Add the first one</Button>}>Nothing here yet.</EmptyState>
        <Skeleton width={240} label="Loading example" />
        <SkeletonRows rows={2} />
      </Section>

      <Section title="Overlays">
        <div className="row">
          <Button onClick={() => setDialog(true)}>Open dialog</Button>
          <Button variant="danger" onClick={() => setConfirm(true)}>
            Open confirmation
          </Button>
          <Button onClick={() => setDrawer(true)}>Open drawer</Button>
          <Popover label="Example popover" trigger={<Button>Open popover</Button>}>
            <p>Popover content.</p>
          </Popover>
          <Menu
            label="Example menu"
            trigger={<Button>Open menu</Button>}
            items={[
              { label: "Rename", onSelect: () => undefined },
              { label: "Delete", onSelect: () => undefined, danger: true },
            ]}
          />
          <Button onClick={() => toast({ title: "Saved", description: "A toast message.", tone: "success" })}>
            Show toast
          </Button>
        </div>
        <Dialog
          open={dialog}
          onOpenChange={setDialog}
          title="Example dialog"
          description="Dialogs trap focus and close on Esc."
          actions={
            <Button variant="primary" onClick={() => setDialog(false)}>
              Close
            </Button>
          }
        />
        <ConfirmDialog
          open={confirm}
          onOpenChange={setConfirm}
          title="Delete payments-api?"
          body="Its code index is removed. This can't be undone."
          confirmLabel="Delete repository"
          danger
          onConfirm={() => setConfirm(false)}
        />
        <Drawer open={drawer} onOpenChange={setDrawer} title="Example drawer">
          <p>Drawer content.</p>
        </Drawer>
      </Section>

      <Section title="Tabs">
        <Tabs
          label="Example tabs"
          value={tab}
          onValueChange={setTab}
          tabs={[
            { value: "one", label: "First", content: <p>First tab.</p> },
            { value: "two", label: "Second", content: <p>Second tab.</p> },
          ]}
        />
      </Section>

      <Section title="Data">
        <DataTable
          label="Example table"
          rows={ROWS}
          rowKey={(r) => r.id}
          rowLabel={(r) => r.name}
          onLoadMore={() => undefined}
          columns={[
            { key: "name", header: "Name", cell: (r) => r.name, sortValue: (r) => r.name },
            { key: "status", header: "Status", cell: (r) => <StatusPill status={r.status} /> },
            { key: "chunks", header: "Chunks", numeric: true, cell: (r) => r.chunks, sortValue: (r) => r.chunks },
          ]}
        />
        <KeyValue
          items={[
            ["Branch", "main"],
            ["Chunks", "1,240"],
          ]}
        />
        <CodeBlock label="Example code">{"docker compose exec api pytest"}</CodeBlock>
        <DiffView text={SAMPLE_DIFF} />
        <Markdown>{SAMPLE_MD}</Markdown>
      </Section>
    </div>
  );
}
