# ADR-003: Keep Backblaze B2 as the runtime origin for model artifacts

**Date:** 2026-08-14
**Status:** accepted

## Context

ADR-002 widened placement from three Canadian regions to fourteen across six territories. Workers in AU, IN and JP now cold-start a 42.5 GB model set out of ca-east-006, and the distance raised the question of whether to drop the Canadian cache entirely and hydrate straight from Hugging Face on every cold start, removing the egress line item and shortening the path to the bytes.

Cost turned out not to be the constraint. The model set is 42,470,585,471 bytes across four blobs. B2 gives free egress up to three times average monthly storage and then charges $0.01/GB, so a hundred cold starts a month costs roughly $41, and the stored bytes themselves earn about 127 GB of the allowance that delivered video currently rides on for free. Removing the cache removes the charge and the allowance together. Either way the figure is tens of dollars against GPU rental measured in thousands.

The question worth answering was therefore latency, and behind it, territory.

## Decision

Keep Backblaze B2 as the runtime origin for model artifacts. Hugging Face stays what it already is: a publication-time source, reached once per model set under operator review through `put_url`, and confined by validators to the approved host and a revision-pinned resolve URL.

Two findings decide it.

Hugging Face serves Xet data from an S3 bucket in us-east-1 behind CloudFront. Storage Regions, the feature that would place those bytes closer to a worker, apply to the repositories an organisation owns. Comfy-Org/MiniMax-H3 is not ours, and Asia-Pacific is not offered on any tier yet. There is no configuration available to us that puts a controllable origin near AU, IN or JP. The remaining benefit reduces to whether a CloudFront edge happens to be holding a 21 GB checkpoint between our cold starts, which we can neither pre-warm, pin, nor observe, and which Hugging Face's own architecture write-up concedes may increase first-hop distance for some clients.

The licence settles the rest. The grant forbids reproducing or distributing the works outside the Applicable Territory, and the United States is a named exclusion. Hydrating on every cold start would make us the party causing continuous reproduction of the work from a US origin across a cache with edges in every excluded territory, in perpetuity, on infrastructure we do not control. ADR-001 read one ambiguous term in the licence and recorded the reading as provisional; this would require a second and harder reading about a third party's cache behaviour. The approval record already names CA as holding the reviewed publication path, and that is the property being traded away.

## Consequences

The manifest keeps digest-addressed object keys, the licence approval keeps its binding to manifest digest 6f74cd5f, and CredentialScope keeps its one-bucket-one-prefix invariant. No new bearer credential enters the worker: a Hugging Face token has no prefix restriction and no capability decomposition, so it would have sat outside the credential model as a documented exception rather than inside it.

Long-haul hydration remains a real cost and is now owed measurement rather than argument. Two things follow. The hydration path's own bounds need correcting first, because a 300-second executor default currently deadlines a 21 GB download that cannot finish inside it. After that, pulling the 605 MB audio VAE from OC-AU-1, AP-JP-1 and AP-IN-2 against both origins, repeated across several hours, yields throughput at distance and the edge-cache hit rate at once. The B2-versus-R2 comparison that capabilities.yaml already requires is the structural answer, and it reaches both goals -- zero egress and edge termination -- without moving the trust anchor off storage we own.

What would reopen this: a written territory authorization from the licensor, which the licence invites and which would replace the reasoning above rather than argue with it. What would not reopen it: Hugging Face shipping Asia-Pacific storage regions, since repository placement remains the owner's setting and the repository is not ours. Mirroring into an org-owned Hugging Face repository is also not a route, because it reintroduces the publication step this was meant to delete while leaving the US origin and the global edge cache exactly where they are.
