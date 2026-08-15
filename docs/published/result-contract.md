# Durable result commit

Every retry writes beneath the immutable `outputs/<job>/<attempt>/` namespace.
The uploader transfers the exact video, pinned workflow, and generation metadata
then independently heads every object to compare byte count, SHA-256, and
provider version. Only the final committer may create `result.json`, and it
re-verifies the complete evidence set immediately before doing so. Identical
marker retries are idempotent; divergent bytes are a terminal conflict.

Consumers treat `result.json` as the sole completion signal. Authorization is
checked before job state or metadata is exposed, and unauthorized jobs map to
the same response as absent jobs. A separate delivery-read/share credential
issues a link for only the committed video with a maximum one-hour TTL; URLs
are response-only values excluded from telemetry.

Retention is explicit by object class, and enforced by two mechanisms because
one cannot reach every class. Inputs expire on a bucket lifecycle rule, as do
hidden versions and abandoned multipart uploads. Committed deliverables and
failed attempts share the `outputs/<job>/<attempt>/` namespace and cannot be
separated by a prefix rule, so a reconciler distinguishes them by the presence
of `result.json` and expires each on its own period. An attempt whose committed
state cannot be established is retained, never deleted.

Prompt text is held inside the committed generation metadata and is destroyed
with the attempt that carried it, on the deliverable period. It has no shorter
independent clock: the prompt is the reproducibility record, and while the
video exists the prompt is still required for the purpose it was collected for.
