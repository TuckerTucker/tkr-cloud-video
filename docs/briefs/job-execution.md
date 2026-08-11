# Job execution contract

The version 1 request is a strict discriminated union for text-to-video,
image-to-video, and reference-to-video operations. Unknown fields and unsafe
object references fail before workspace allocation, input transfer, or GPU use.
Canonical JSON produces a deterministic request hash.

Idempotency is scoped by authenticated principal and key. Identical concurrent
requests converge on one server-generated job identity; a different canonical
payload conflicts. Attempt workspaces use only server-owned identifiers.

Private inputs are authorized before bounded download, inspected and hashed in
a partial path, and atomically promoted. An approved binding map may alter only
declared inputs in a digest-verified API workflow. Prompt polling is bounded and
timeouts cancel the exact prompt. Result discovery uses prompt history paths,
never an output-directory scan; exactly one contained, ffprobe-validated video
becomes upload-ready with size, SHA-256, workflow, model-set, and request
identities.
