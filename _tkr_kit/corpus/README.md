# Compliance corpus payloads

The Koji compliance corpus is a derived, disposable index. These payloads are the
durable artifact: the index is rebuilt from them, never the other way round.

Rebuild:

    tkr op governance.corpus_load --input "$(cat _tkr_kit/corpus/compliance-corpus-2026-07-17.json)" --json

## Provenance

`compliance-corpus-2026-07-17.json` was built for, and is shared with, the compass_app
project (`_planning/_localization/corpus/`). It carries verbatim provisions with each
instrument's official citation, source URL and consolidation date:

| Instrument | Citation | Consolidated | Source |
|---|---|---|---|
| pipeda | S.C. 2000, c. 5 | 2026-05-26 | laws-lois.justice.gc.ca |
| pipeda-breach-regs | SOR/2018-64 | 2026-05-26 | laws-lois.justice.gc.ca |
| qc-p39-1 | CQLR c. P-39.1 | 2026-04-01 | legisquebec.gouv.qc.ca |
| qc-c11 | CQLR c. C-11 | 2026-04-01 | legisquebec.gouv.qc.ca |

Both sets are unofficial consolidations, reproduced under the Reproduction of Federal
Law Order SI/97-5 (federal) and as internal reference copies (Québec). They are not
official versions and carry no authority of their own.

## Known coverage gap

There is no EEA instrument here. ADR-002 placed workers in Iceland and Norway, and no
GDPR provision exists in this corpus to ground that placement. `.claude/rules/compliance-triage.md`
records this as an open gap rather than citing around it. Loading a GDPR payload is what
closes it — do not paraphrase the instrument from memory to fill the hole.

## Fields excluded from the personal-information surface

Held here rather than in the triage record's "Not applicable" table: `parseComplianceRecord`
maps every parsed outcome through `outcomeToPolicy`, which demands all four detail lines, so a
populated row there makes the record unreadable. The table must stay `| — | — | — |`.

| Item | Rationale |
|---|---|
| `job.seed`, `width`, `height`, `frames`, `fps`, `workflow_id`, `model_set_id` | Generation parameters and pinned asset identifiers. They describe the model and the canvas, not an identifiable individual. |
| Object metadata and provenance evidence | Digests, sizes, provider evidence, manifest bindings. They describe artifacts rather than people. Retained deliberately as immutable release evidence, which is a separate obligation from personal-information retention. |

## Open gaps

No provision in the loaded corpus governs these. A record written over them is incomplete,
and the honest response is to load the missing instrument — not to cite around it.

1. **No EEA instrument is loaded.** ADR-002 placed workers in Iceland and Norway, both EEA
   states. This corpus carries PIPEDA, its breach regulations and two Québec instruments; it
   holds no GDPR, and queries for "gdpr", "European Union", "biometric" and "special category"
   all return nothing. Whether EEA processing engages the GDPR through the EEA Agreement, and
   whether an uploaded face is special-category data under it, is unanswered.

2. **PIPEDA applicability is unresolved.** Part 1 applies to personal information handled "in
   the course of commercial activities" (S.C. 2000, c. 5, s 4(1)(a), consolidated 2026-05-26).
   The ratified licence approval records deployment_use as *internal*. The triage record is
   written on the conservative assumption that the obligations apply; if the deployment becomes
   commercial, that assumption stops being conservative and becomes load-bearing.

3. **Québec applicability is assumed, not established.** P-39.1 is cited because the storage
   bucket and the original compute both sit in Québec. Whether this organisation is a "person
   carrying on an enterprise" under that Act has not been determined.

4. **Retention is unimplemented for every personal field.** `job.prompt`, `job.input_reference`
   and `job.output_video` all record "no automated deletion exists". That is one gap, not three,
   and it is the largest single distance between the record and the provisions it cites.
