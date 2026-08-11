# tkr-cloud-video

`tkr-cloud-video` is a secure, reproducible execution platform for MiniMax H3
video workflows through ComfyUI on RunPod. Private object storage is the durable
source of truth; worker-local NVMe is a disposable, verified cache.

## Development

Python 3.11 or newer and [uv](https://docs.astral.sh/uv/) are required. Recreate
the reviewed dependency graph and run the complete local/CI contract with:

```bash
uv sync --frozen --all-groups
./scripts/check.sh
```

The diagnostic command is read-only, requires no cloud credentials, and makes
no network requests:

```bash
uv run python -m tkr_cloud_video doctor
uv run python -m tkr_cloud_video doctor --json
```

Application behavior is composed from injected provider-neutral interfaces.
Provider credentials must never be placed in project metadata, command-line
arguments, workflow files, or logs.

## Worker deployment

The image defaults to the fail-closed `serverless` command; `worker` runs the
same supervised runtime without the host adapter for Interactive Pod validation.
Build the RunPod deployment image for its AMD64 GPU target explicitly:

```bash
docker buildx build --platform linux/amd64 --load \
  -t tkr-cloud-video:local .
```

The ComfyUI dependency graph is version- and hash-locked in
`requirements/comfyui.lock`, including the CUDA 12.8 PyTorch backend. Regenerate
that lock only after reviewing a new pinned ComfyUI revision.

The runtime resolves the exact
digest-addressed manifest, probes its single B2 model namespace, verifies and
materializes every artifact, starts ComfyUI on loopback, validates the required
nodes and models, and only then becomes ready. `SIGTERM` drops readiness before
bounded child termination.

Required non-secret variables are `TKR_RELEASE_ID`, `TKR_WORKER_ID`,
`TKR_MODEL_SET_ID`, `TKR_MANIFEST_DIGEST`, and `TKR_B2_BUCKET_NAME`. RunPod must
inject model, input, and output key IDs/application keys as separate
`B2_MODEL_*`, `B2_INPUT_*`, and `B2_OUTPUT_*` secrets.
Optional `TKR_*_ROOT`, prefix, port, capacity-safety, and timeout variables use
the container defaults defined by `runtime/config.py`. ComfyUI receives a fresh
environment containing none of the B2 values.
