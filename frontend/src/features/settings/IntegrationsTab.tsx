import { useEffect, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, PlugZap } from "lucide-react";
import { api, type Credential, type CredentialTest, type Integration } from "../../api";
import {
  Banner,
  Button,
  Card,
  ConfirmDialog,
  EmptyState,
  ErrorNote,
  SkeletonRows,
  StatusPill,
  TextArea,
  TextField,
  formatDate,
  formatRelative,
  useToast,
} from "../../components/ui";

const STATE_LABEL: Record<Credential["status"], string> = {
  not_set: "Not set",
  unverified: "Not tested",
  ok: "Verified",
  invalid: "Rejected",
  unreadable: "Can't be read",
};

/** Shared with Home and the Add repository dialog: an integration is usable once every part is saved. */
export function missingCredentials(i: Integration): string[] {
  return i.credentials.filter((c) => c.status === "not_set").map((c) => c.label);
}

function CredentialField({ integration, c, focused }: { integration: Integration; c: Credential; focused: boolean }) {
  const qc = useQueryClient();
  const toast = useToast();
  const input = useRef<HTMLInputElement & HTMLTextAreaElement>(null);
  const [value, setValue] = useState("");
  const [clearing, setClearing] = useState(false);
  useEffect(() => {
    if (focused) {
      input.current?.scrollIntoView({ block: "center" });
      input.current?.focus();
    }
  }, [focused]);
  const url = `/api/integrations/${integration.id}/credentials/${c.kind}`;
  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["integrations"] });
    qc.invalidateQueries({ queryKey: ["home"] });
  };
  const save = useMutation({
    mutationFn: () => api(url, { method: "PUT", json: { value } }),
    onSuccess: () => {
      setValue("");
      refresh();
      toast({ title: `${integration.name} ${c.label.toLowerCase()} saved`, tone: "success", description: "Test it to make sure it works." });
    },
  });
  const clear = useMutation({
    mutationFn: () => api(url, { method: "DELETE" }),
    onSuccess: () => {
      setClearing(false);
      refresh();
    },
  });
  const isKubeconfig = c.kind === "kubeconfig";
  const label = `${integration.name} ${c.label.toLowerCase()}`;
  const hint =
    c.status === "unreadable"
      ? "Can't be read any more. Enter it again."
      : c.updated_at && c.status !== "not_set"
        ? `Saved ${formatDate(c.updated_at)}.`
        : undefined;
  const common = {
    label,
    value,
    placeholder: c.status === "not_set" ? "Enter…" : "Replace…",
    hint,
    autoComplete: "off",
  };
  return (
    <form
      className="inline-form"
      onSubmit={(e) => {
        e.preventDefault();
        save.mutate();
      }}
    >
      {isKubeconfig ? (
        <TextArea
          {...common}
          ref={input}
          rows={6}
          inputClassName="mono"
          spellCheck={false}
          onChange={(e) => setValue(e.target.value)}
        />
      ) : (
        <TextField
          {...common}
          ref={input}
          type={c.kind === "username" ? "text" : "password"}
          onChange={(e) => setValue(e.target.value)}
        />
      )}
      <StatusPill status={c.status === "ok" ? "ok" : c.status} label={STATE_LABEL[c.status]} />
      <Button type="submit" variant="primary" disabled={!value} pending={save.isPending}>
        Save
      </Button>
      {c.status !== "not_set" && (
        <Button variant="danger" onClick={() => setClearing(true)}>
          Clear
        </Button>
      )}
      <ErrorNote error={save.error || clear.error} />
      <ConfirmDialog
        open={clearing}
        onOpenChange={setClearing}
        title={`Clear your ${label}?`}
        body={`Anything that uses ${integration.name} for you, such as indexing your repositories or schedules, stops working until you enter it again.`}
        confirmLabel="Clear"
        danger
        pending={clear.isPending}
        onConfirm={() => clear.mutate()}
      />
    </form>
  );
}

function IntegrationCard({ integration, focus }: { integration: Integration; focus: string | null }) {
  const qc = useQueryClient();
  const [result, setResult] = useState<CredentialTest | null>(null);
  const test = useMutation({
    mutationFn: () => api<CredentialTest>(`/api/integrations/${integration.id}/test`, { method: "POST" }),
    onSuccess: (r) => {
      setResult(r);
      qc.invalidateQueries({ queryKey: ["integrations"] });
      qc.invalidateQueries({ queryKey: ["home"] });
    },
  });
  const [focusId, focusKind] = (focus ?? "").split(":");
  const here = focusId === String(integration.id);
  const missing = missingCredentials(integration);
  const verified = integration.credentials.find((c) => c.verified_at && c.status === "ok")?.verified_at;
  const rejected = integration.credentials.find((c) => c.status === "invalid" && c.last_error);
  const id = `integration-${integration.id}`;
  return (
    <li>
      <Card as="section" aria-labelledby={id} className={`stack${here ? " focused" : ""}`}>
        <div className="row">
          <h2 id={id} className="grow">
            {integration.name}
          </h2>
          <span className="muted small mono">{integration.base_url}</span>
        </div>
        <p className="muted small">{integration.help}</p>
        {integration.credentials.map((c, n) => (
          <CredentialField
            key={c.kind}
            integration={integration}
            c={c}
            focused={here && (focusKind ? focusKind === c.kind : n === 0)}
          />
        ))}
        {rejected && !result && <Banner tone="danger">{rejected.last_error}</Banner>}
        {result && <Banner tone={result.ok ? "success" : "danger"}>{result.message}</Banner>}
        <div className="row">
          <Button
            icon={<PlugZap size={16} aria-hidden />}
            pending={test.isPending}
            disabled={integration.kind !== "feed" && missing.length > 0}
            onClick={() => test.mutate()}
          >
            Test
          </Button>
          {verified && <span className="muted small">Verified {formatRelative(verified)}</span>}
          {missing.length > 0 && integration.kind !== "feed" && (
            <span className="muted small">Save {missing.join(" and ").toLowerCase()} first.</span>
          )}
        </div>
        <ErrorNote error={test.error} />
      </Card>
    </li>
  );
}

/** Settings → Integrations (07 §4.12, 02 §1): one card per integration an admin added. */
export default function IntegrationsTab() {
  const [params] = useSearchParams();
  const focus = params.get("focus");
  const back = params.get("back");
  const integrations = useQuery({ queryKey: ["integrations"], queryFn: () => api<Integration[]>("/api/integrations") });
  const safeBack = back && back.startsWith("/") && !back.startsWith("//") ? back : null;
  return (
    <div className="stack">
      {safeBack && (
        <div>
          <Link to={safeBack} className="btn btn-secondary">
            <ArrowLeft size={16} aria-hidden />
            <span className="btn-label">{safeBack.startsWith("/sources/repos") ? "Back to Add repository" : "Back"}</span>
          </Link>
        </div>
      )}
      <p className="muted lede">
        Your own credentials for the systems your admin connected. They're encrypted, never shown again after saving,
        and used only for what you ask MyWorkBuddy to do.
      </p>
      <ErrorNote error={integrations.error} />
      {integrations.isLoading && <SkeletonRows />}
      {integrations.data && !integrations.data.length && (
        <EmptyState>
          No integrations yet. An admin adds GitLab, Gerrit, Jira and the others in Admin → Integrations; tokens you
          saved before then move here when they do.
        </EmptyState>
      )}
      <ul className="stack plain-list" aria-label="Integrations">
        {integrations.data?.map((i) => <IntegrationCard key={i.id} integration={i} focus={focus} />)}
      </ul>
    </div>
  );
}
