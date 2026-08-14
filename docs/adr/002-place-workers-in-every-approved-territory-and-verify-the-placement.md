# ADR-002: Place workers in every approved territory and verify the placement

**Date:** 2026-08-14
**Status:** accepted

## Context

ADR-001 established that the license permits AU, CA, IN, IS, JP and NO. The approval record that followed covered CA and IS. Deployment covered neither addition: `deploy_runpod_endpoint.sh` had named CA-MTL-1, CA-MTL-2 and CA-MTL-3 since the endpoint was first created, so Iceland was approved and never used. The grant and the configuration had drifted apart, and nothing reported that they had.

The Canadian placement was also costing capacity. `set_endpoint_gpus.sh` had already added Hopper and Ampere behind Blackwell "so the endpoint can actually place a worker while no Canadian Blackwell is in stock" — a fallback onto an architecture the NVFP4 text encoder does not target natively, accepted because Canada alone could not supply the one it does.

A per-data-centre capacity survey was run against the provider before this decision, because the account-wide query the repository already had is misleading: B200 reports stock account-wide and is available in none of the permitted regions. The survey found:

- Blackwell, the architecture the text encoder targets, exists in **CA-MTL-3, EUR-IS-1 and EUR-IS-2 only**, all thin.
- **IN and JP** carry H100 and H200 — throughput, on the fallback architecture.
- **NO and AU** carry nothing usable. EUR-NO-1 is empty, EUR-NO-2 lists H100 with no free capacity, and OC-AU-1 offers only a 48 GB card, below what a 42.5 GB model set plus activations needs.

So the expansion that helps the NVFP4 path is Iceland, which was already approved. AU, IN, JP and NO were reviewed as a set with it.

## Decision

Approve all six license-permitted territories in one record, and configure placement to every registered region of those territories.

The approval covering AU, CA, IN, IS, JP and NO supersedes the CA/IS record. AU, CA, IN and JP rest on no contested reading. IS and NO are licensable only through ADR-001's provisional reading of the Union exclusion and fall out together if the licensor answers that the EEA is in scope; the three-month expiry is what stops that reading from standing indefinitely.

NO and AU are approved despite supplying no capacity today. Legal coverage is the cheap half of this decision and capacity moves; the record expires before that judgement can go stale unnoticed. What is not accepted is doing this silently, so the record states plainly that no purpose beyond widening placement capacity is documented for AU, IN, JP or NO.

CA-MTL-4 joins the three Montreal regions already used. It is registered as Canada, so it carries the same territory, residency story and review as its neighbours; omitting it narrowed capacity without narrowing exposure.

Verify placement rather than assume it. Three checks now exist where there were none:

1. **Before the write.** `set_endpoint_datacenters.sh` refuses to send any region whose territory the ratified approval does not name, so a non-compliant placement is never configured on a live endpoint even briefly.
2. **After the write.** Both the repair script and the create path read the placement back over GraphQL — REST omits `dataCenterIds` from its responses — and require the observed regions to equal the regions sent. A provider that accepts the write and stores nothing is an error, not a success.
3. **In the test suite.** `tests/security_foundation/test_deployed_placement.py` maps every region any script configures through the territory registry and asserts it falls inside the ratified grant.

## Consequences

Placement widens from 3 regions to 14 across six territories. Blackwell supply roughly triples, from CA-MTL-3 alone to CA-MTL-3, EUR-IS-1 and EUR-IS-2, which is the result that matters for the NVFP4 path.

The approval record is now the single lever. `runpod_graphql.sh` reads territories from it, so ratifying six territories is simultaneously the legal act and the operational one. The test suite is the only independent check remaining, and it is the reason the pin exists.

An empty placement no longer passes as approved. `review_placement` returns approved for an empty observation — nothing observed is nothing excluded — which meant the exact failure mode ADR-001 records, the provider dropping the placement field, would have exited zero. The verification script now refuses an unrestricted placement on its own terms.

The grant lapses on its own when the reading narrows. IS and NO are held on the understanding that licensing drops them when the licensor answers on the EEA, and that only happens by itself if narrowing the exclusion set breaks something: the record is a static file, and the placement check reads its territories without asking whether the licence still permits them. A test now asserts the ratified record names no excluded territory, so moving the EEA states into the exclusion set fails the suite and forces the record to narrow, which narrows placement on its next run. `scripts/validate_license_approval.sh` makes the same check but is not part of `scripts/check.sh`, so it would not have caught this unprompted.

Compute placement and data residency are now visibly separate. Storage stays in `ca-east-006` regardless of where a worker runs, so inputs and outputs cross borders in transit to and from IS, NO, IN, JP and AU workers. This record settles the model license only. The privacy and data-residency question is untouched and unrecorded; no compliance triage record exists in this repository.

Two gaps stay open and are not closed here. `ReleaseAssuranceGate` still has no runtime caller, so nothing at request time refuses a job whose territory falls outside the grant. And the territory registry omits 13 regions the provider offers, including EUR-IS-5 in Iceland, which carries H200 stock; unregistered regions are denied and reported, so the omission costs capacity rather than safety, but a registry-driven expansion silently skips them.
