# Security boundaries

Runtime storage access is split into four independently revocable Backblaze B2
standard application keys. Each key is restricted to one bucket and one object
name prefix.

| Role | Required capabilities | Process boundary |
|---|---|---|
| model-reader | `listFiles`, `readFiles` | Hydrator only |
| input-reader | `listFiles`, `readFiles` | Job input staging only |
| output-writer | `listFiles`, `readFiles`, `writeFiles` | Durable uploader only |
| delivery-reader | `listFiles`, `readFiles`, `shareFiles` | Authorized delivery service only |

The administrative key used to create and revoke these keys is never installed
on a worker. ComfyUI receives no object-storage key. Replacement credentials are
created and probed against allowed and forbidden operations before the prior key
is revoked. Provider secret values are referenced through RunPod secrets and are
never written into images, workflows, manifests, or rclone configuration.

Release promotion separately requires a complete provenance manifest and a
current approval bound to its model set, manifest digest, deployment use, and
territory. Corrections create new immutable evidence rather than mutating an
approved release.
