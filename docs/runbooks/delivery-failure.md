# Durable delivery incident

Trigger: upload verification or commit-marker errors breach policy.

1. Hold delivery for the affected job attempt; consumers must continue treating
   the result as incomplete while `result.json` is absent.
2. Use job, attempt, and correlation identifiers to list only the immutable
   attempt prefix. Never synchronize the complete ComfyUI output directory.
3. Verify every required media, metadata, and workflow object's remote size,
   SHA-256 digest, and provider version against local validated evidence.
4. Retry the same attempt only when existing objects match exactly. Otherwise
   create a new attempt identity; never mix evidence between attempts.
5. Publish `result.json` only through the committer after all required objects
   verify. Never create or edit a marker manually.
6. Resolve and download the private result through the authorized delivery path
   before clearing the hold.

Expected observables: the marker is written last or remains absent; mismatched
objects return a typed conflict; signed access has bounded TTL; no media bytes,
credentials, or signed tokens appear in application logs.

rehearsal: 2026-08-10 (upload and commit failure paths exercised locally)
