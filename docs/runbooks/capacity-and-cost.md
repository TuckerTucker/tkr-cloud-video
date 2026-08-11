# Capacity and cost incident

Trigger: disk preflight rejection, cache-efficiency regression, unexpected
storage growth, or a provider-cost anomaly.

1. Hold new admissions for the affected release and region; do not terminate
   workers with leased cache blobs.
2. Capture cold and warm benchmark records for the same manifest digest,
   workload, and region.
3. Confirm warm `hydration.bytes` is zero. If it is not, inspect cache digest
   verification and eviction evidence before changing capacity.
4. Compare measured storage GB-month, egress GB, and request classes with the
   dated executable pricing inputs in `operations/cost_model.py`.
5. Evict only unselected, unleased blobs under the configured high/low water
   marks. Do not delete retained deliverables.
6. Resume admissions only after disk preflight and the cold/warm comparison
   pass. Record a new pricing audit before changing providers.

Expected observables: admission state is held or active; manifest digest and
region match across both runs; cache hit/miss counts, hydration bytes, storage,
egress, request counts, and calculated USD cost are present; protected blobs
remain intact.

rehearsal: 2026-08-10 (automated policy paths exercised locally; provider
capacity actions require environment acceptance)
