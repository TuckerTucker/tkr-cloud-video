# Operator console

A loopback web page for submitting a generation and playing back what it
committed. It exists because the two things the Serverless endpoint cannot do
for a caller are the two things a caller most needs: refuse a bad request
cheaply, and hand back a video. The handler returns references and never media
bytes, and a run's status reports that the worker answered, not that anything
was generated.

```bash
scripts/console.sh                # http://127.0.0.1:8765
scripts/console.sh --port 9100
```

## What it does

**It runs the worker's intake before submitting.** The page performs
`parse_generation_request` then `resolve_request_prompt`, in that order, which
is exactly what `RunPodHandler.validate` performs. A frame count off the
temporal grid, a canvas over the model's ceiling, or a closed-vocabulary term in
a structured prompt is refused in milliseconds. Submitted blind, the same
request pays a full model hydration first — about seven minutes of fixed
overhead by the inference in `docs/audits/2026-08-15-generation-latency.md`.

Agreement between the console's refusal and the handler's is a property that
would otherwise drift silently, so it is pinned rather than assumed.

> verify: `uv run pytest tests/console/test_intake_preflight.py -k agrees`

**It delivers a committed video.** On a committed result the console
reauthorizes and issues one signed link, for the video artifact only, capped by
`SignedLinkService` at one hour. The link is a response-only value: it is not
logged, does not enter an error context, and is not persisted.

> verify: `uv run pytest tests/console/test_signed_delivery.py`

**Its form is served from the registry, not restated.** Canvas bounds, the
canvas multiple, the temporal grid, the trained range, and the closed
vocabularies are read from the pinned trained-envelope and grammar tables, so a
newly registered model set reaches the page by being registered.

> verify: `uv run pytest tests/console/test_intake_preflight.py -k form_constraints`

**It shows the wire text.** A structured prompt is rendered, not supplied, so
the page shows back the exact text that would reach the workflow. Without it a
caller composing a structured prompt has no way to see what it composed to.

## Configuration

| Variable | Meaning |
|---|---|
| `TKR_CONSOLE_ENDPOINT_ID` | the Serverless endpoint to submit to |
| `TKR_PRINCIPAL_ID` | **must equal the endpoint's own principal** |
| `TKR_B2_BUCKET_NAME` | the delivery bucket |
| `TKR_CONSOLE_SIGNED_LINK_TTL_SECONDS` | requested playback lifetime, capped at 3600 |
| `TKR_CONSOLE_BIND_PORT` | loopback port; `0` asks the operating system to choose |

The three secrets are read from the environment and resolved by
`scripts/console.sh` from the same vault entries the other scripts use:
`RUNPOD_API_KEY`, `B2_DELIVERY_KEY_ID`, `B2_DELIVERY_APPLICATION_KEY`.

`TKR_PRINCIPAL_ID` is the one setting whose mismatch is invisible until a
generation succeeds. A job's identity is `sha256("<principal>:<request hash>")`,
so a console configured with a different principal computes keys for objects the
worker never wrote, and every generation reads as never committed. The console
compares the worker's reported job id against the one it derived and says so
when they differ.

> verify: `uv run pytest tests/console -k identity`

## What it cannot do

It composes the access policy, the signing service, and the private result
service, and none of the write side. No uploader, committer, reconciler, or
erasure path exists anywhere in the package, so it cannot write to or delete
from the delivery bucket. That is a property of its source rather than of its
configuration, which is what makes it checkable.

> verify: `uv run pytest tests/console -k write_or_delete`

It binds loopback and refuses any other address in its settings rather than in
the launch command. It authenticates nothing, because on loopback the operating
system has already answered who the caller is; it checks the `Host` header so a
page on another origin cannot reach it by pointing a name at 127.0.0.1. It keeps
no access log, because a request line names the job and would sit beside the
response that issued a link.

> verify: `uv run pytest tests/console/test_console_server.py -k rebound`

Its record of submitted jobs is in memory, and authorization is derived from it:
a job id the console did not derive resolves exactly as an absent one. Restarting
forgets which jobs were submitted. The alternative is a new on-disk store of job
identities and request hashes with no retention clock, which is the gap
`.claude/rules/compliance-triage.md` already records against the fields this
product does persist.

## Scope

Text-to-video only. The image-conditioned modes are reachable through the
request contract but are not offered by the page: an upload field turns on
`job.input_reference`, which the triage record identifies as the most sensitive
field in the product and as having gone cross-border under ADR-002 without the
privacy impact assessment that provision requires. Making that path easy is not
a UI decision.

## Dependencies

None beyond the standard library. `pydantic`, `runpod` and `structlog` remain
the whole runtime dependency set, and the deployed image's graph is unchanged. A
web framework would have widened the worker image's dependency closure for a
single-operator local tool, and the console holds a RunPod bearer token and a
delivery credential, so its dependency surface is part of its security posture.
Signature Version 4 presigning is computed from `hmac` and `hashlib`, and its
output is pinned against a vector verified byte for byte against `botocore`.

> verify: `uv run pytest tests/engineering_foundation/test_package_contract.py`

## What the console reports

| What is shown | What it means |
|---|---|
| "The endpoint has not resumed yet" | the config plane accepted a change the run plane has not; resubmit shortly |
| A refusal before submission | the request is invalid; the defect names the field and the rule |
| `COMPLETED` with a refusal beneath it | the worker answered but rejected the request; nothing was generated |
| "The worker's job id differs from the one this console derived" | `TKR_PRINCIPAL_ID` is not the endpoint's |
| Committed stays `in-progress` after a successful run | the marker is not readable; check the delivery credential and the bucket |
