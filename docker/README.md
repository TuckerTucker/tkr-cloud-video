# Worker image inputs

The worker uses the digest-pinned CUDA 12.8.1 runtime and uv 0.9.26 images,
checks out ComfyUI v0.30.2 by its full commit (`dec5d945…`), installs this
application from `uv.lock`, and bundles rclone plus ffprobe. MiniMax H3 support
is provided by the pinned ComfyUI core release. Models are hydrated at runtime;
they are never baked into an image layer.

The release pipeline records the resulting OCI digest, Python lock digest,
ComfyUI revision, tool versions, schemas, license review, vulnerability scan,
and secret scan in the worker release manifest. Runtime credentials are injected
only at launch and are absent from build arguments and layers.
