# Release publication

The reviewed MiniMax H3 catalog and API-format text-to-video workflow live in
`release-assets/minimax-h3-t2v/`. Preparing a manifest is read-only and requires
an explicit operator-controlled licence approval identity:

```bash
uv run tkr-cloud-video release prepare \
  --catalog release-assets/minimax-h3-t2v/catalog.json \
  --license-approval-id <approval-id> \
  --output dist/minimax-h3-t2v.model-set.json
```

Publication streams the large revision- and digest-pinned model files directly
to B2 and publishes the manifest last. It requires a temporary offline key with
`listBuckets`, `listFiles`, `readFiles`, and `writeFiles` access restricted to
the release bucket and scoped to `models/`. `listBuckets` is required by
rclone to resolve the explicitly configured bucket; the key must not be given
access to any other bucket. Supply it only through
`B2_MODEL_PUBLISHER_KEY_ID` and
`B2_MODEL_PUBLISHER_APPLICATION_KEY`. Revoke that key after publication. The
runtime model-reader credential remains read-only.

Build `Dockerfile.publisher` for release publication. This deliberately small,
unprivileged image excludes CUDA, PyTorch, ComfyUI, and every model artifact:

```bash
docker buildx build --platform linux/amd64 --load \
  -f Dockerfile.publisher -t tkr-cloud-video-publisher:local .
```

Run that image on temporary Canadian compute so the artifact path is Hugging
Face to the publisher and then directly to Canadian B2. Terminate the compute
and revoke the prefix-scoped publisher key immediately after the manifest is
committed. Do not relay model bytes through a developer workstation.
