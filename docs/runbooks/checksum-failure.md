# Artifact checksum incident

Trigger: checksum failures breach the configured consecutive evaluation
threshold.

1. Hold the affected release and stop new hydration attempts for its manifest.
2. Locate the release, manifest, and artifact digest using allowlisted event
   fields. Do not copy provider payloads or credentials into incident notes.
3. Compare the immutable object size, SHA-256 metadata, and version against the
   approved manifest and publication evidence.
4. Remove only the digest's partial local file. Never promote or relabel the
   failed bytes and never bypass verification.
5. If provenance is uncertain, revoke the publishing credential and publish
   corrected content under a new immutable release identity.
6. Re-run cold hydration and workflow validation before releasing the hold.

Expected observables: readiness remains false; no final cache blob exists for
the failed digest; partial state is absent after cleanup; the replacement
release has distinct immutable identity and passing digest evidence.

rehearsal: 2026-08-10 (failure-injection checksum path exercised locally)
