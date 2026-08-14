# ADR-001: Read the license exclusion of the European Union as Union membership

**Date:** 2026-08-14
**Status:** accepted

## Context

The MiniMax H3 Community License Agreement (2026-08-02) grants a worldwide Applicable Territory excluding the European Union, the United Kingdom, the Republic of Korea and the United States of America, and forbids using, reproducing, modifying, distributing or displaying the works or their outputs outside that territory.

Iceland, Liechtenstein and Norway sit between the two readings. They are not Union members, but they take part in the single market through the EEA Agreement and adopt Union law marked EEA-relevant. Nothing in the agreement names them.

The question is not academic. Of the data centres the compute provider offers, only EUR-IS-1 in Iceland carries a Blackwell GPU that is also outside the named exclusions, and Blackwell is the architecture the model set's NVFP4 text encoder targets natively. Every Canadian data centre offers Hopper and Ampere only.

## Decision

Read "the European Union" as Union membership, which leaves Iceland, Liechtenstein and Norway outside the exclusion. Encode the reading as a named set in security/territories.py rather than as an absence from the excluded list, and pin it with a test, so reversing it is a territory decision that fails loudly rather than a refactor that widens where the model may be served.

The agreement names the United Kingdom separately, and the United Kingdom was a member until 2020. Its drafters therefore distinguish membership from geography rather than using the Union as a shorthand for Europe.

The reading is provisional and is not a legal opinion. The exclusions track regulatory exposure, and Union instruments of this kind are commonly EEA-relevant, which would reach these states through the EEA Agreement. The license invites contact for territory authorization; a written answer from the licensor replaces this reading rather than arguing with it.

## Consequences

Permitted territories become AU, CA, IN, IS, JP and NO. Iceland becomes reachable, and with it the only licensed route to native NVFP4.

The reading is not yet an approval. No deployment may rely on it until a license approval record binds a named reviewer, the model set, the manifest digest, the deployment use and an expiry to these territories, which the record-model-license-territory-approval story specifies. Until that record exists and the release gate has a runtime caller, placement is restricted by provider configuration alone, and this provider has already been observed dropping a placement field on create and altering worker bounds unprompted.

If the licensor answers that the EEA is excluded, the change is one constant and the placement check reports the narrowed set on its next run. If a deployment has run in Iceland by then, its committed evidence names the territory, so the exposure is bounded and auditable rather than unknown.
