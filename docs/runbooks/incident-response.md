# Runbook — Incident Response

What to do when something breaks. Sized for a small team: one IC, one
scribe, one ops.

## Severity levels

| Sev | Trigger                                            | First response  |
|-----|----------------------------------------------------|-----------------|
| S1  | Trading halted; data loss; HITL queue stuck > 1h   | Page on-call    |
| S2  | Degraded UX; one component down; backlog growing   | Slack channel   |
| S3  | Nuisance alert; cosmetic issue                     | Backlog ticket  |

## Five-minute triage

1. **Confirm the alert** — open `/readyz`. Which check is failing?
2. **Check the dashboard** — Grafana → Horizon Overview.
3. **Tail logs** — `make web-logs` and `make worker-logs`.
4. **Decide** — restart vs. rollback vs. escalate.
5. **Communicate** — post to #horizon-ops every 15 min until resolved.

## Common scenarios

### "All trades failing with `live_error`"
Look at `traces.llm_call` rows where `mode = 'live_error'`. Likely
causes:
* OpenAI key revoked or rate-limited
* Network egress lost (NAT down)
* Wrong model name in env

Mitigation: set `USE_MOCK_LLM=1` and roll the env — the firm continues
on deterministic mocks while you investigate.

### "HITL backlog growing"
* Confirm `horizon_hitl_pending` rising in Grafana.
* Check who's the operator on call — see the policies doc.
* Bulk-resolve via the UI; if the queue is poisoned with duplicates,
  set `HITL_ONE_PER_TICKER=1` and bounce `worker`.

### "Web pod CrashLoopBackOff"
```bash
kubectl -n horizon-capital logs deploy/web -p
```
Almost always one of: DB connection refused, missing env var, bad
image tag. Roll back the deployment:
```bash
kubectl -n horizon-capital rollout undo deploy/web
```

### "Worker stuck (no scheduler ticks)"
Worker is single-replica (no leader election). If the pod is healthy
but loops aren't ticking, restart it:
```bash
kubectl -n horizon-capital rollout restart deploy/worker
```

## Postmortem template

After any S1/S2 incident, fill in:

```
# Incident: <one-line summary>
Date:    <YYYY-MM-DD>
Sev:     S1 | S2
Duration: <Start ISO> → <End ISO>   (MTTR: N min)
IC:      <name>
Authors: <names>

## What happened
Customer-facing summary.

## Timeline (UTC)
HH:MM — alert fired
HH:MM — first responder ack
HH:MM — root cause identified
HH:MM — mitigation applied
HH:MM — verified healthy

## Root cause
Specific change / failure mode. No blame.

## What worked
Detection, escalation, tooling that helped.

## What didn't
Gaps. Each gets an action item.

## Action items
- [ ] (owner) <follow-up>     (due: YYYY-MM-DD)
```
