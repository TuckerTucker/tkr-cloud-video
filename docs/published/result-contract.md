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

Retention is explicit by object class. Inputs, failed attempts, scratch files,
hidden versions, and abandoned multipart uploads expire independently while
committed deliverables use their longer reviewed retention period.
