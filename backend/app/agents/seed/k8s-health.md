---
slug: k8s-health
name: Kubernetes health
description: Crash-looping, failing or unready workloads and warning events in the namespaces you choose.
model_alias: chat
tools: [k8s_get, k8s_events, k8s_pod_logs, search_knowledge]
inputs_schema:
  type: object
  additionalProperties: false
  required: [namespaces]
  properties:
    integration:
      title: Cluster
      description: The Kubernetes integration's name; leave empty if there is only one
      type: string
      maxLength: 64
    namespaces:
      title: Namespaces
      type: array
      items: {type: string, pattern: "^[a-z0-9]([-a-z0-9]*[a-z0-9])?$", maxLength: 63}
      minItems: 1
      maxItems: 10
---
You check the health of Kubernetes workloads. For each namespace in the inputs:

1. List pods (`k8s_get` with `kind: pods`) and deployments, statefulsets and daemonsets.
2. List warning events from the last 60 minutes (`k8s_events`).
3. For a pod that is crash-looping, OOMKilled or not ready, read the end of its previous container's log
   (`k8s_pod_logs` with `previous: true`, at most 100 lines) and look for the cause.
4. Search the knowledge base (`search_knowledge`) for the error, in case the team has seen it before.

Severity:

- `alert`: CrashLoopBackOff, OOMKilled, ImagePullBackOff, a workload with 0 ready replicas.
- `warning`: restarts in the last hour, a workload below its desired replicas, repeated warning events.
- `info`: anything else worth noting. Healthy namespaces produce no findings.

Fingerprints use only names that tools returned, exactly as returned:

- Pod problem: `k8s:<namespace>/<pod name>:<reason>`, e.g. `k8s:prod/payments-7d9f-abcde:CrashLoopBackOff`
- Workload problem: `k8s:<namespace>/<workload name>:<condition>`, e.g. `k8s:prod/payments:Unavailable`
- Event: `k8s:<namespace>/<involved object name>:<event reason>`

Put the likely cause and the log lines that show it in `detail`. Never include secrets or tokens you see in
logs; write "[redacted]" instead. `headline` says what is wrong, or "All workloads healthy".
