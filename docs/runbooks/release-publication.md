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
`listFiles`, `readFiles`, and `writeFiles` access scoped to `models/`, supplied
only through `B2_MODEL_PUBLISHER_KEY_ID` and
`B2_MODEL_PUBLISHER_APPLICATION_KEY`. Revoke that key after publication. The
runtime model-reader credential remains read-only.
