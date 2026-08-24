# ADR-004: A model-set identity names the published set, not the checkpoint

**Date:** 2026-08-23
**Status:** accepted

## Context

Registering the reference model set forced a question the product had never answered in writing: what does a model_set_id identify? The published set is the evidence that the question was live. minimax-h3-t2v-int8-20260809 says text-to-video while the diffusion checkpoint it ships is minimax-h3-fl2va-int8, the keyframe first/last-frame checkpoint, and its graph reaches text-to-video by driving MiniMaxH3ImageToVideo with no image attached. So the mode segment of the published identity already named the mode the graph drives rather than the weights it loads. Nothing recorded that, and until the trained-envelope registry held more than one row, guessing wrong cost nothing. The registry is where it starts to be load-bearing: one identifier is one envelope row, and a request resolves its absent dimensions from the row its model set names.

## Decision

A model-set identity names the published set — weights, workflow graph and binding map together — not the checkpoint. The reference set is therefore minimax-h3-ref2v-int8-20260809: a distinct identity from minimax-h3-t2v-int8-20260809, sharing the 20260809 segment because that segment names the weights snapshot (Comfy-Org/MiniMax-H3@014cd40f), which the two sets genuinely share. The ref2v segment is what separates them. The reference row restates the fl2va checkpoint's trained numbers under its own identity and its own source string rather than inheriting them.

## Consequences

Naming the checkpoint instead would collapse t2v and ref2v onto one identifier, because their weights are byte-identical. One identifier is one envelope row, so the reference set would inherit the text-to-video row by construction and unreviewably — no second row would ever exist for a reviewer to look at. Preventing exactly that is why the registry keys on model-set identity, so this rule is what makes the registry do its job rather than a naming preference.

The rule also settles a question that would otherwise recur: the published t2v identity is not wrong and must not be renamed. It names the mode its graph drives, which is what this rule says an identity names. Renaming it is unavailable in any case — model_set_id is inside the manifest's canonical bytes and a reviewer license approval binds that digest, so correcting the name would invalidate a signed approval.

Every future model set is named under this rule, and each new published graph earns a new identity even when it loads weights already on disk. The cost is that identities multiply faster than checkpoints do, and each one needs its own envelope row reviewed rather than inherited. That cost is the point. Consumers should import REFERENCE_MODEL_SET_ID and TEXT_TO_VIDEO_MODEL_SET_ID from jobs/trained_envelope.py rather than restating either literal, so the catalog and the registry cannot drift apart; the doctor gate already fails on an unregistered model set, which is what caught a mismatched identity while this decision was being made.
