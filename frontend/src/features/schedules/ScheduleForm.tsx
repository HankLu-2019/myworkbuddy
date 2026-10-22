import { useEffect, useMemo, useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery } from "@tanstack/react-query";
import { FlaskConical } from "lucide-react";
import { ApiError, api, type Agent, type CronPreview, type NotifyMode, type RunDetail, type ScheduleIn } from "../../api";
import { useAuth } from "../../auth";
import { draftKey, readDraft, writeDraft } from "../../app/drafts";
import { UnsavedGuard } from "../../app/UnsavedGuard";
import {
  Banner,
  Button,
  Card,
  Drawer,
  ErrorNote,
  RadioGroup,
  Select,
  SkeletonRows,
  TextArea,
  TextField,
  Toggle,
  formatDate,
} from "../../components/ui";
import { InputsForm, toInputs, toTexts, type InputTexts, type InputsSchema } from "./InputsForm";
import { NOTIFY_OPTIONS, PRESETS, cronOf, presetOf, type Preset } from "./presets";
import { ACTIVE, RunResult } from "./RunResult";

export type FormState = {
  name: string;
  agent: string;
  inputs: InputTexts;
  cron: string;
  timezone: string;
  notify_mode: NotifyMode;
  notify_email: boolean;
  prompt: string;
  max_runtime_s: number;
};

export function toState(s: Partial<ScheduleIn> | undefined, timezone: string): FormState {
  return {
    name: s?.name ?? "",
    agent: s?.agent ?? "",
    inputs: toTexts(s?.inputs ?? {}),
    cron: s?.cron ?? "0 8 * * 1-5",
    timezone: s?.timezone ?? timezone,
    notify_mode: s?.notify_mode ?? "on_change",
    notify_email: s?.notify_email ?? true,
    prompt: s?.prompt ?? "",
    max_runtime_s: s?.max_runtime_s ?? 300,
  };
}

export function toBody(f: FormState, schema: InputsSchema): ScheduleIn {
  return {
    name: f.name.trim(),
    agent: f.agent,
    inputs: toInputs(schema, f.inputs),
    cron: f.cron.trim(),
    timezone: f.timezone,
    notify_mode: f.notify_mode,
    notify_email: f.notify_email,
    prompt: f.prompt,
    max_runtime_s: f.max_runtime_s,
    enabled: true,
  };
}

/** Field errors from a 422 (`field`, e.g. "inputs.jql") or 409 limit response. */
export function fieldErrors(error: unknown): Record<string, string> {
  if (!(error instanceof ApiError)) return {};
  const field = (error.body as { field?: string } | null)?.field;
  return field ? { [field]: error.message } : {};
}

function useDebounced<T>(value: T, ms = 400): T {
  const [v, setV] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setV(value), ms);
    return () => clearTimeout(t);
  }, [value, ms]);
  return v;
}

function localTimes(next: string[]): string {
  return next.slice(0, 3).map((iso) => formatDate(iso)).join(", ");
}

/**
 * The schedule form (07 §4.7): What · When · Notify · Instructions, a live preview of the next runs, and
 * "Run once now to test" before saving. Drafts survive a reload (07 §6.6).
 */
export function ScheduleForm({
  initial,
  draftId,
  submitLabel,
  onSubmit,
  pending,
  saved,
  error,
}: {
  initial?: Partial<ScheduleIn>;
  draftId: string;
  submitLabel: string;
  onSubmit: (body: ScheduleIn) => void;
  pending?: boolean;
  /** The save succeeded: drop the draft and stop guarding, so the caller can navigate away. */
  saved?: boolean;
  error?: unknown;
}) {
  const { user } = useAuth();
  const key = draftKey(user?.id, "schedule", draftId);
  const start = useMemo(() => toState(initial, user?.timezone ?? "UTC"), [initial, user?.timezone]);
  const [f, setF] = useState<FormState>(() => {
    try {
      const saved = readDraft(key);
      return saved ? { ...start, ...(JSON.parse(saved) as Partial<FormState>) } : start;
    } catch {
      return start;
    }
  });
  const [restored] = useState(() => !!readDraft(key));
  // The profile may arrive after the form: a new schedule defaults to the user's timezone.
  useEffect(() => {
    if (!initial?.timezone && user?.timezone && !restored) setF((s) => (s.timezone === "UTC" ? { ...s, timezone: user.timezone! } : s));
  }, [user?.timezone]);
  const [preset, setPreset] = useState<Preset>(() => presetOf(f.cron));
  const dirty = JSON.stringify(f) !== JSON.stringify(start);
  useEffect(() => writeDraft(key, dirty && !saved ? JSON.stringify(f) : ""), [key, f, dirty, saved]);
  const set = <K extends keyof FormState>(k: K, v: FormState[K]) => setF((s) => ({ ...s, [k]: v }));

  const agents = useQuery({ queryKey: ["agents"], queryFn: () => api<Agent[]>("/api/agents"), staleTime: 300_000 });
  const agent = agents.data?.find((a) => a.slug === f.agent);
  const schema = (agent?.inputs_schema ?? {}) as InputsSchema;
  useEffect(() => {
    if (!f.agent && agents.data?.length) set("agent", agents.data[0].slug);
  }, [agents.data, f.agent]);

  const debounced = useDebounced(JSON.stringify({ cron: f.cron, timezone: f.timezone }));
  const cronQuery = useMemo(() => JSON.parse(debounced) as { cron: string; timezone: string }, [debounced]);
  const preview = useQuery({
    queryKey: ["cron-preview", cronQuery],
    queryFn: () => api<CronPreview>("/api/schedules/preview", { method: "POST", json: cronQuery }),
    enabled: !!cronQuery.cron.trim(),
    staleTime: 60_000,
  });

  const [testRun, setTestRun] = useState<string | null>(null);
  const test = useMutation({
    mutationFn: () => api<{ run_id: string }>("/api/schedules/test", { method: "POST", json: toBody(f, schema) }),
    onSuccess: (r) => setTestRun(r.run_id),
  });
  const run = useQuery({
    queryKey: ["runs", testRun],
    queryFn: () => api<RunDetail>(`/api/runs/${testRun}`),
    enabled: !!testRun,
    refetchInterval: (q) => (ACTIVE.includes(q.state.data?.status ?? "queued") ? 2000 : false),
  });

  const errors = { ...fieldErrors(test.error), ...fieldErrors(error) };
  const cronError = errors.cron ?? (preview.data && !preview.data.valid ? preview.data.error : null);

  function submit(e: FormEvent) {
    e.preventDefault();
    onSubmit(toBody(f, schema));
  }

  if (agents.isLoading) return <SkeletonRows rows={6} />;
  return (
    <form className="stack schedule-form" onSubmit={submit} noValidate>
      <UnsavedGuard when={dirty && !pending && !saved} what="this schedule" />
      {restored && <Banner tone="info">Restored your unsaved changes.</Banner>}
      <ErrorNote error={agents.error} />

      <Card as="section" aria-labelledby="sf-what" className="stack">
        <h2 id="sf-what">What</h2>
        <TextField label="Name" value={f.name} onChange={(e) => set("name", e.target.value)} error={errors.name} required maxLength={120} />
        <Select
          label="Check type"
          value={f.agent}
          onChange={(e) => setF((s) => ({ ...s, agent: e.target.value, inputs: {} }))}
          options={(agents.data ?? []).map((a) => [a.slug, a.name] as const)}
          hint={agent?.description}
          error={errors.agent}
        />
        {agent && agent.missing.length > 0 && (
          <Banner tone="warning">
            Needs a working {agent.missing.join(", ")} credential; until then those tools are left out.{" "}
            <Link to="/settings/integrations">Set it up</Link>
          </Banner>
        )}
        {agent && <InputsForm schema={schema} value={f.inputs} onChange={(v) => set("inputs", v)} errors={errors} />}
        {errors.inputs && <p className="error-text">{errors.inputs}</p>}
      </Card>

      <Card as="section" aria-labelledby="sf-when" className="stack">
        <h2 id="sf-when">When</h2>
        <RadioGroup
          legend="Repeat"
          value={preset}
          options={PRESETS.map(([k, label]) => [k, label] as const)}
          onChange={(p) => {
            setPreset(p);
            set("cron", cronOf(p, f.cron));
          }}
        />
        {preset === "custom" && (
          <TextField
            label="Cron expression"
            hint="Five fields: minute hour day-of-month month day-of-week, e.g. 30 7 * * 1-5"
            value={f.cron}
            onChange={(e) => set("cron", e.target.value)}
            error={cronError}
            inputClassName="mono"
          />
        )}
        {preset !== "custom" && cronError && <p className="error-text">{cronError}</p>}
        <TextField label="Timezone" value={f.timezone} onChange={(e) => set("timezone", e.target.value)} error={errors.timezone} />
        <p className="muted" aria-live="polite">
          {preview.data?.valid
            ? `${preview.data.description}${preset === "custom" ? ` (${f.cron.trim()})` : ""}. Next: ${localTimes(preview.data.next)} (${f.timezone})`
            : preview.isFetching
              ? "Checking…"
              : ""}
        </p>
      </Card>

      <Card as="section" aria-labelledby="sf-notify" className="stack">
        <h2 id="sf-notify">Notify</h2>
        <RadioGroup legend="Tell me" value={f.notify_mode} options={NOTIFY_OPTIONS} onChange={(v) => set("notify_mode", v)} />
        <Toggle label="Also send an email" checked={f.notify_email} onChange={(v) => set("notify_email", v)} />
      </Card>

      <Card as="section" aria-labelledby="sf-instructions" className="stack">
        <h2 id="sf-instructions">Instructions</h2>
        <TextArea
          label="Extra instructions (optional)"
          hint="In your words, e.g. “Only crash loops and OOM kills matter.”"
          value={f.prompt}
          onChange={(e) => set("prompt", e.target.value)}
          rows={3}
          maxLength={4000}
          error={errors.prompt}
        />
      </Card>

      <ErrorNote error={Object.keys(fieldErrors(error)).length ? null : error} />
      <ErrorNote error={Object.keys(fieldErrors(test.error)).length ? null : test.error} />
      <div className="row row-end">
        <Button icon={<FlaskConical size={16} aria-hidden />} onClick={() => test.mutate()} pending={test.isPending}>
          Run once now to test
        </Button>
        <Button variant="primary" type="submit" pending={pending}>
          {submitLabel}
        </Button>
      </div>

      <Drawer open={!!testRun} onOpenChange={(o) => !o && setTestRun(null)} title="Test run">
        {run.data ? <RunResult run={run.data} /> : <SkeletonRows rows={3} />}
        <ErrorNote error={run.error} />
      </Drawer>
    </form>
  );
}
