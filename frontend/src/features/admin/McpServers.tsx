import { useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type McpServer, type McpTool } from "../../api";
import {
  Banner,
  Button,
  Card,
  Checkbox,
  CodeBlock,
  ConfirmDialog,
  DataTable,
  Dialog,
  ErrorNote,
  TextArea,
  TextField,
  formatRelative,
  useToast,
} from "../../components/ui";

const KEY = ["admin-mcp"];
const STATUS_LABEL: Record<McpTool["status"], string> = {
  disabled: "Off",
  enabled: "On (read)",
  changed: "Changed: check it again",
  removed: "No longer listed",
};

function AddServer({ onDone }: { onDone: () => void }) {
  const qc = useQueryClient();
  const toast = useToast();
  const [name, setName] = useState("");
  const [url, setUrl] = useState("");
  const [header, setHeader] = useState("Authorization");
  const [value, setValue] = useState("");
  const [members, setMembers] = useState(true);
  const add = useMutation({
    mutationFn: () =>
      api<McpServer>("/api/admin/mcp-servers", {
        method: "POST",
        json: {
          name: name.trim(), url: url.trim(), auth_header: value ? header.trim() : null, auth_value: value || null,
          roles: members ? ["admin", "member"] : ["admin"],
        },
      }), // prettier-ignore
    onSuccess: (s) => {
      qc.invalidateQueries({ queryKey: KEY });
      toast({ title: `${s.name} added`, tone: s.last_error ? "error" : "success", description: s.last_error ?? `${s.tools.length} tools listed, all off.` });
      onDone();
    },
  });
  return (
    <form
      className="stack"
      aria-label="Add an MCP server"
      onSubmit={(e: FormEvent) => {
        e.preventDefault();
        add.mutate();
      }}
    >
      <TextField label="Name" required pattern="[a-z0-9][a-z0-9-]{1,31}" maxLength={32} value={name} hint="Lowercase; tools are named mcp.<name>.<tool>." onChange={(e) => setName(e.target.value)} />
      <TextField label="Server URL (streamable HTTP)" required type="url" value={url} placeholder="https://mcp.example.com/mcp" onChange={(e) => setUrl(e.target.value)} />
      <TextField label="Auth header" value={header} maxLength={64} onChange={(e) => setHeader(e.target.value)} />
      <TextField
        label="Auth header value"
        type="password"
        autoComplete="off"
        value={value}
        hint="Stored encrypted, shared by everyone who uses the server's tools. Use a read-only credential."
        onChange={(e) => setValue(e.target.value)}
      />
      <Checkbox label="Members may use its tools (otherwise admins only)" checked={members} onChange={setMembers} />
      <ErrorNote error={add.error} />
      <div className="row">
        <Button type="submit" variant="primary" pending={add.isPending}>
          Add server
        </Button>
        <Button type="button" onClick={onDone}>
          Cancel
        </Button>
      </div>
    </form>
  );
}

function EnableDialog({ tool, onClose }: { tool: McpTool; onClose: () => void }) {
  const qc = useQueryClient();
  const [reason, setReason] = useState("");
  const enable = useMutation({
    mutationFn: () => api<McpTool>(`/api/admin/mcp-tools/${tool.id}`, { method: "PUT", json: { enabled: true, reason } }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: KEY });
      onClose();
    },
  });
  return (
    <Dialog
      open
      onOpenChange={(o) => !o && onClose()}
      title={`Turn on ${tool.full_name}?`}
      description="Chat and agents can then call it for every allowed user, with the server's credential. Its answers are treated as untrusted text."
      actions={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button variant="primary" pending={enable.isPending} disabled={reason.trim().length < 5} onClick={() => enable.mutate()}>
            Turn on as read-only
          </Button>
        </>
      }
    >
      <div className="stack">
        <p className="small pre-wrap">{tool.description || "No description."}</p>
        <CodeBlock label="Input schema">{JSON.stringify(tool.input_schema, null, 2)}</CodeBlock>
        <p className="muted small">The model sees this description and schema. If the server changes either, the tool turns off again until you look.</p>
        <TextArea label="Why it's needed, and why it only reads" rows={3} maxLength={500} value={reason} onChange={(e) => setReason(e.target.value)} />
        <ErrorNote error={enable.error} />
      </div>
    </Dialog>
  );
}

function ServerCard({ s }: { s: McpServer }) {
  const qc = useQueryClient();
  const toast = useToast();
  const [enabling, setEnabling] = useState<McpTool | null>(null);
  const [removing, setRemoving] = useState(false);
  const done = () => qc.invalidateQueries({ queryKey: KEY });
  const refresh = useMutation({
    mutationFn: () => api<McpServer>(`/api/admin/mcp-servers/${s.id}/refresh`, { method: "POST" }),
    onSuccess: (r) => {
      done();
      toast({ title: r.last_error ? `${s.name} didn't answer` : `${s.name} refreshed`, tone: r.last_error ? "error" : "success", description: r.last_error ?? undefined });
    },
  });
  const turnOff = useMutation({
    mutationFn: (t: McpTool) => api(`/api/admin/mcp-tools/${t.id}`, { method: "PUT", json: { enabled: false } }),
    onSuccess: done,
  });
  const roles = useMutation({
    mutationFn: (members: boolean) => api(`/api/admin/mcp-servers/${s.id}`, { method: "PATCH", json: { roles: members ? ["admin", "member"] : ["admin"] } }),
    onSuccess: done,
  });
  const remove = useMutation({ mutationFn: () => api(`/api/admin/mcp-servers/${s.id}`, { method: "DELETE" }), onSuccess: done });
  return (
    <Card as="section" className="stack" aria-label={`MCP server ${s.name}`}>
      <div className="row" style={{ justifyContent: "space-between" }}>
        <div>
          <h3 style={{ margin: 0 }}>{s.name}</h3>
          <span className="mono small">{s.url}</span>
          <div className="muted small">
            {s.has_auth ? `Credential in ${s.auth_header}` : "No credential"} · {s.last_refreshed_at ? `listed ${formatRelative(s.last_refreshed_at)}` : "never listed"}
          </div>
        </div>
        <div className="row-actions">
          <Button size="sm" pending={refresh.isPending} onClick={() => refresh.mutate()}>
            Refresh tools
          </Button>
          <Button size="sm" variant="danger" onClick={() => setRemoving(true)}>
            Remove
          </Button>
        </div>
      </div>
      {s.last_error && <Banner tone="danger">The server didn't answer: {s.last_error}</Banner>}
      <Checkbox label="Members may use its tools" checked={s.roles.includes("member")} disabled={roles.isPending} onChange={(on) => roles.mutate(on)} />
      <DataTable
        label={`Tools of ${s.name}`}
        rows={s.tools}
        rowKey={(t) => t.id}
        rowLabel={(t) => t.full_name}
        empty="The server lists no tools."
        columns={[
          { key: "name", header: "Tool", sortValue: (t) => t.name, cell: (t) => <span className="mono small">{t.full_name}</span> },
          { key: "desc", header: "Description", cell: (t) => <span className="small">{t.description}</span> },
          {
            key: "status",
            header: "State",
            cell: (t) => (t.write_hint ? <span className="muted">Write tool: can't be turned on. {t.write_hint}</span> : STATUS_LABEL[t.status]),
          },
          {
            key: "actions",
            header: <span className="visually-hidden">Actions</span>,
            cell: (t) =>
              t.write_hint || t.status === "removed" ? null : t.status === "enabled" ? (
                <Button size="sm" pending={turnOff.isPending && turnOff.variables?.id === t.id} onClick={() => turnOff.mutate(t)}>
                  Turn off
                </Button>
              ) : (
                <Button size="sm" onClick={() => setEnabling(t)}>
                  {t.status === "changed" ? "Review and turn on" : "Turn on"}
                </Button>
              ),
          },
        ]}
      />
      <ErrorNote error={turnOff.error ?? roles.error ?? remove.error} />
      {enabling && <EnableDialog tool={enabling} onClose={() => setEnabling(null)} />}
      <ConfirmDialog
        open={removing}
        onOpenChange={setRemoving}
        title={`Remove ${s.name}?`}
        body="Its tools disappear from chat and agents, and its credential is deleted."
        confirmLabel="Remove server"
        danger
        pending={remove.isPending}
        onConfirm={() => remove.mutate()}
      />
    </Card>
  );
}

/** MCP servers (08 §F12): extra read-only data sources. Every tool starts off; an admin turns on read tools one by
    one, with a reason; a tool whose definition changes is off again until someone looks. */
export default function McpServers() {
  const [adding, setAdding] = useState(false);
  const servers = useQuery({ queryKey: KEY, queryFn: () => api<McpServer[]>("/api/admin/mcp-servers") });
  return (
    <section className="stack" aria-labelledby="mcp-title">
      <h2 id="mcp-title">MCP servers</h2>
      <p className="muted">
        Extra sources chat and agents can read through the Model Context Protocol. Only read tools can be turned on; write tools need per-call
        approvals, which aren't available yet.
      </p>
      <ErrorNote error={servers.error} />
      {servers.data?.map((s) => <ServerCard key={s.id} s={s} />)}
      {servers.data && servers.data.length === 0 && !adding && <p className="muted">No MCP servers yet.</p>}
      {adding ? (
        <Card>
          <AddServer onDone={() => setAdding(false)} />
        </Card>
      ) : (
        <div>
          <Button onClick={() => setAdding(true)}>Add MCP server</Button>
        </div>
      )}
    </section>
  );
}
