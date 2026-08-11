# Immutable artifact catalog

Model and workflow bytes are stored beneath digest-addressed keys. A manifest
binds every key to its SHA-256 digest, exact byte count, safe ComfyUI-relative
destination, dependencies, provenance digest, and license approval. Publication
uploads and remotely verifies all blobs before it creates the manifest object.

Workers receive both a manifest key and an independently pinned SHA-256 digest.
They verify the exact bytes before parsing, then confirm every referenced remote
object. A mutable `latest` name is therefore never a trust anchor.

Local NVMe uses `blobs/sha256/<first-two>/<digest>`. Downloads land in unique
`partials/` names, verify completely, and are atomically renamed. Warm-cache
hits are re-hashed and download zero bytes. Materialization uses containment-
checked managed symlinks; eviction removes only whole, unselected, unleased
blobs until the configured high-water target is met.
