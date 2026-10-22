import { useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type ChannelTestResult, type NotifyChannel, type NotifyChannelIn } from "../../api";
import {
  Banner,
  Button,
  Card,
  Checkbox,
  ConfirmDialog,
  DataTable,
  ErrorNote,
  Select,
  TextField,
  useToast,
  type Column,
} from "../../components/ui";

type Kind = NotifyChannel["kinds"][number];
type Severity = NotifyChannel["min_severity"];

const KIND_LABEL: Record<Kind, string> = {
  job: "Jobs",
  plan: "Plans",
  alert: "Alerts",
  recovered: "Recoveries",
  run: "Runs",
  proposal: "Suggestions",
  credential: "Credentials",
  system: "System",
};
const SEVERITY_LABEL: Record<Severity, string> = {
  info: "Everything",
  warning: "Warnings and alerts",
  alert: "Alerts only",
};
const APP_LABEL: Record<NotifyChannel["kind"], string> = { slack: "Slack", teams: "Microsoft Teams" };
const KEY = ["notify-channels"];

function sends(ch: NotifyChannel): string {
  const kinds = ch.kinds.length ? ch.kinds.map((k) => KIND_LABEL[k]).join(", ") : "Everything in your inbox";
  return `${kinds} · ${SEVERITY_LABEL[ch.min_severity].toLowerCase()}${ch.include_details ? " · with details" : ""}`;
}

function AddChannel({ onDone }: { onDone: () => void }) {
  const qc = useQueryClient();
  const toast = useToast();
  const [kind, setKind] = useState<NotifyChannel["kind"]>("slack");
  const [name, setName] = useState("");
  const [url, setUrl] = useState("");
  const [severity, setSeverity] = useState<Severity>("warning");
  const [kinds, setKinds] = useState<Kind[]>([]);
  const [details, setDetails] = useState(false);
  const add = useMutation({
    mutationFn: () => {
      const json: NotifyChannelIn = { kind, name: name.trim(), url: url.trim(), kinds, min_severity: severity, include_details: details };
      return api<NotifyChannel>("/api/notify-channels", { method: "POST", json });
    },
    onSuccess: (ch) => {
      qc.invalidateQueries({ queryKey: KEY });
      toast({ title: `${ch.name} added`, tone: "success", description: "Send a test message to check it works." });
      onDone();
    },
  });
  const toggle = (k: Kind, on: boolean) => setKinds((ks) => (on ? [...ks, k] : ks.filter((x) => x !== k)));
  return (
    <form
      className="stack"
      aria-label="Add a chat channel"
      onSubmit={(e: FormEvent) => {
        e.preventDefault();
        add.mutate();
      }}
    >
      <Select
        label="App"
        value={kind}
        options={Object.entries(APP_LABEL)}
        onChange={(e) => setKind(e.target.value as NotifyChannel["kind"])}
      />
      <TextField label="Name" required maxLength={80} value={name} placeholder="#payments-team" onChange={(e) => setName(e.target.value)} />
      <TextField
        label="Webhook URL"
        required
        type="password"
        autoComplete="off"
        maxLength={1000}
        value={url}
        hint={
          kind === "slack"
            ? "In Slack: Apps → Incoming Webhooks → Add to a channel. The URL is stored encrypted and never shown again."
            : "In Teams: the channel's Workflows → “Post to a channel when a webhook request is received”. The URL is stored encrypted and never shown again."
        }
        onChange={(e) => setUrl(e.target.value)}
      />
      <Select
        label="Send"
        value={severity}
        options={Object.entries(SEVERITY_LABEL)}
        onChange={(e) => setSeverity(e.target.value as Severity)}
      />
      <fieldset className="stack-tight">
        <legend>About (none ticked: everything)</legend>
        {(Object.keys(KIND_LABEL) as Kind[]).map((k) => (
          <Checkbox key={k} label={KIND_LABEL[k]} checked={kinds.includes(k)} onChange={(on) => toggle(k, on)} />
        ))}
      </fieldset>
      <Checkbox label="Include the first lines of each item" checked={details} onChange={setDetails} />
      {details && (
        <Banner tone="warning">Details can contain ticket text and log lines. Everyone in the chat channel will see them.</Banner>
      )}
      <ErrorNote error={add.error} />
      <div className="row">
        <Button type="submit" variant="primary" pending={add.isPending}>
          Add channel
        </Button>
        <Button type="button" onClick={onDone}>
          Cancel
        </Button>
      </div>
    </form>
  );
}

function ChannelActions({ ch }: { ch: NotifyChannel }) {
  const qc = useQueryClient();
  const toast = useToast();
  const [removing, setRemoving] = useState(false);
  const refresh = () => qc.invalidateQueries({ queryKey: KEY });
  const test = useMutation({
    mutationFn: () => api<ChannelTestResult>(`/api/notify-channels/${ch.id}/test`, { method: "POST" }),
    onSuccess: (r) =>
      r.ok
        ? toast({ title: `Test message sent to ${ch.name}`, tone: "success" })
        : toast({ title: `${ch.name} didn't get the test message`, tone: "error", description: r.error ?? undefined }),
  });
  const onOff = useMutation({
    mutationFn: () => api<NotifyChannel>(`/api/notify-channels/${ch.id}`, { method: "PATCH", json: { enabled: !ch.enabled } }),
    onSuccess: (c) => {
      refresh();
      toast({ title: c.enabled ? `${c.name} turned on` : `${c.name} turned off`, tone: "success" });
    },
  });
  const remove = useMutation({
    mutationFn: () => api(`/api/notify-channels/${ch.id}`, { method: "DELETE" }),
    onSuccess: () => {
      setRemoving(false);
      refresh();
      toast({ title: `${ch.name} removed`, tone: "success" });
    },
  });
  return (
    <div className="row">
      <Button size="sm" pending={test.isPending} onClick={() => test.mutate()}>
        Send test
      </Button>
      <Button size="sm" pending={onOff.isPending} onClick={() => onOff.mutate()}>
        {ch.enabled ? "Turn off" : "Turn on"}
      </Button>
      <Button size="sm" variant="danger" onClick={() => setRemoving(true)}>
        Remove
      </Button>
      <ConfirmDialog
        open={removing}
        onOpenChange={setRemoving}
        title={`Remove ${ch.name}?`}
        body="Notifications stop going to this chat channel. Your inbox and email aren't affected."
        confirmLabel="Remove channel"
        danger
        pending={remove.isPending}
        onConfirm={() => remove.mutate()}
      />
      <ErrorNote error={onOff.error ?? remove.error ?? test.error} />
    </div>
  );
}

/** Chat channels (08 §F8): Slack and Teams webhooks that get a short message for matching inbox items. */
export default function ChatChannels() {
  const [adding, setAdding] = useState(false);
  const channels = useQuery({ queryKey: KEY, queryFn: () => api<NotifyChannel[]>("/api/notify-channels") });
  const columns: Column<NotifyChannel>[] = [
    {
      key: "name",
      header: "Channel",
      cell: (ch) => (
        <>
          <strong>{ch.name}</strong>
          <div className="muted">
            {APP_LABEL[ch.kind]} · <code>{ch.url_hint}</code>
          </div>
        </>
      ),
      sortValue: (ch) => ch.name,
    },
    { key: "sends", header: "Sends", cell: sends },
    {
      key: "state",
      header: "State",
      cell: (ch) => (ch.enabled ? "On" : <span className="error-text">Off{ch.disabled_reason ? `: ${ch.disabled_reason}` : ""}</span>),
    },
    { key: "actions", header: <span className="visually-hidden">Actions</span>, cell: (ch) => <ChannelActions ch={ch} /> },
  ];
  return (
    <Card className="stack" aria-label="Chat channels">
      <h2>Slack and Teams</h2>
      <p className="muted">
        A short message with the title and a link back here, for the inbox items you choose. At most 50 a day unless your admin changed it.
      </p>
      <DataTable
        label="Chat channels"
        columns={columns}
        rows={channels.data}
        rowKey={(ch) => ch.id}
        rowLabel={(ch) => ch.name}
        loading={channels.isLoading}
        empty="No chat channels yet."
      />
      <ErrorNote error={channels.error} />
      {adding ? (
        <AddChannel onDone={() => setAdding(false)} />
      ) : (
        <div>
          <Button onClick={() => setAdding(true)}>Add channel</Button>
        </div>
      )}
    </Card>
  );
}
