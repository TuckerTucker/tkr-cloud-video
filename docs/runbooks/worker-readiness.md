# Worker readiness incident

Trigger: readiness remains below the release threshold or a worker transitions
to `failed`.

1. Hold new admissions for the affected worker and retain its last safe
   structured evidence. Do not request or log an environment dump.
2. Query the correlation ID and identify the terminal boot, hydration,
   readiness, generation, upload, or commit transition and typed error code.
3. Confirm disk preflight, manifest hydration, ComfyUI loopback health, workflow
   validation, and required model/node inventory in that order.
4. Drain accepted jobs within their cancellation/deadline contract. Never rely
   on a shutdown-wide output sync.
5. If readiness or error thresholds remain breached, apply the rollout result's
   pinned rollback release and verify recovery evidence.
6. Resume admissions only after the exact candidate passes readiness and its
   acceptance evidence remains complete.

Expected observables: readiness never becomes true before all gates pass;
admissions are held during diagnosis; accepted work reaches a typed terminal
state; rollback targets the recorded healthy release; recovery is correlated.

rehearsal: 2026-08-10 (readiness and rollout policy paths exercised locally)
