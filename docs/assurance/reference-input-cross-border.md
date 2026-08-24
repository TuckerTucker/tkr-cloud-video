# Cross-border processing of reference images — 2026-08-23

**Date:** 2026-08-23
**Scope:** The intake, storage, cross-border reading and disposal of caller-supplied reference
images — `job.input_reference` — for the `image-to-video` and `reference-to-video` modes. Covers the
object-store path (`inputs/` prefix in `ca-east-006`), the worker-side staging path
(`src/tkr_cloud_video/jobs/input_staging.py`), the credential and process boundaries that bound
them, and the retention and erasure mechanisms that reach them. Does **not** cover prompt text,
output video, audio or voice references, or public/commercial deployment use.
**Reviewed at:** commit `37b48ac`, working tree clean. Policy document `config/lifecycle.json`
version `3`. Licence approval `release-assets/minimax-h3-t2v/license-approval.json`, approval id
`minimax-h3-t2v-int8-20260809-au-ca-in-is-jp-no`, expiring 2026-11-14.
**Occasion:** CQLR c. P-39.1, s 17, recorded as item 2 of `.claude/rules/compliance-triage.md`. The
provision requires an assessment weighing sensitivity, purpose and protection measures *before*
personal information is communicated outside Québec. ADR-002 placed workers across six territories
on 2026-08-14 without one. This record is the assessment, conducted before any reference image has
been staged for a worker read.
**Status:** Drafted and unaccepted. The verdict below is a proposal until the acceptance block is
signed. Until it is signed, the gate this record carries has not lifted.

## 1. What is being assessed, and what has not yet happened

No reference image has ever been staged for a worker read. Nothing in the product can write an
object under `inputs/`: the only input-facing credential role is `INPUT_READER`, whose capabilities
are exactly `listFiles` and `readFiles` (`src/tkr_cloud_video/security/credentials.py`,
`ROLE_CAPABILITIES`), and the request contract takes an object key that must already exist
(`src/tkr_cloud_video/jobs/contracts.py`, `InputReference`). The staging path is written and
composed; the way in is not.

That is the whole reason this assessment is being written now rather than after. The finding in the
triage record is latent for reference images and would become live the moment an upload surface
exists. This record precedes the surface.

**What is being assessed is therefore prospective processing**, described from the code that would
carry it, not from operational evidence of it having occurred.

## 2. The personal information: what it is, and why it is collected

A reference image is a caller-supplied still or short clip that conditions generation. It may depict
an identifiable individual, and nothing in the product constrains it from doing so: the accepted
media types are `image/jpeg`, `image/png` and `video/mp4`, and the only checks applied are size,
digest and decodability (`src/tkr_cloud_video/jobs/input_staging.py`).

**This is the most sensitive field the product holds, and it is more sensitive than the triage
record's framing conveys.** The image itself is a likeness. The *output* is a moving likeness
derived from it — the same face, animated, at a duration and canvas the caller chose. A reference
image is not an input that is consumed and discarded; it is an input whose recognisable content
survives into a durable artifact that outlives it. Between one and four may be supplied per request
(`src/tkr_cloud_video/jobs/contracts.py`, `references: tuple[InputReference, ...]` with
`min_length=1, max_length=4`).

**Purpose.** To perform the generation the caller requested, and nothing else. The image is read
once per attempt, bound into the workflow graph as a node input, and not retained by any process
after the attempt's workspace is removed. It is not used for training, evaluation, benchmarking or
any secondary purpose; no code path in this repository reads an object under `inputs/` for any
purpose other than staging one attempt.

**Who the individual is.** The caller is a single operator on a loopback-bound console
(`src/tkr_cloud_video/console/settings.py` refuses any bind address that is not `127.0.0.1`, `::1`
or `localhost`). The individual depicted in a reference image is therefore, in the general case, a
**third party who is not the caller and has no relationship with this system.** Nothing in the
product asks for, records or verifies that person's consent. There is no consent field anywhere in
the request contract, the job evidence or the object store. This is stated here rather than in the
residual-risk section as well, because it is the fact that most changes the character of the
processing: the subject of the most sensitive field is the one party with no interface to the
system.

## 3. Where the image is read

Storage is fixed in Canada; compute is not. The object lives in one reviewed Canadian region and is
read across the border in transit by whichever worker the provider places the attempt on.

**Territories — six.** `AU`, `CA`, `IN`, `IS`, `JP`, `NO`. Sourced from ADR-002 ("Approve all six
license-permitted territories in one record") and confirmed against the `territories` array of the
ratified approval at `release-assets/minimax-h3-t2v/license-approval.json`.

**Regions.** ADR-002's decision is to "configure placement to every registered region of those
territories", which the registry at `src/tkr_cloud_video/security/territories.py`
(`DATA_CENTRE_TERRITORIES`) puts at 14 regions. The deployment scripts in this repository configure
**10** of those 14:

| Territory | Configured today | Registered but not configured |
|---|---|---|
| Canada (CA) | CA-MTL-1, CA-MTL-2, CA-MTL-3 | CA-MTL-4 |
| Iceland (IS) | EUR-IS-1, EUR-IS-2, EUR-IS-3 | EUR-IS-4 |
| Norway (NO) | EUR-NO-1 | EUR-NO-2 |
| India (IN) | AP-IN-1 | AP-IN-2 |
| Japan (JP) | AP-JP-1 | — |
| Australia (AU) | OC-AU-1 | — |

Taken from `scripts/deploy_runpod_endpoint.sh` and `scripts/set_endpoint_datacenters.sh`, which both
name the same 10 and both record why the other four are absent: the provider's REST `PATCH` schema
pins `dataCenterIds` to a narrower enum that rejects them with a 400. The omission is a provider
constraint, not a decision, and nothing in the licence or the registry excludes those four. **The
assessment is written against all 14, because the four absent regions add no territory and could be
added without any decision being taken.**

**Nine of the ten configured regions are outside Québec, and seven are outside Canada.** The three
Montreal regions are inside Québec; every other placement is a communication outside it. That is the
processing this record exists to weigh.

**Purpose per territory, as documented.** ADR-002 and the approval record are explicit: CA holds the
storage bucket, the worker identity and the reviewed publication path; IS is the only licensed
source of the Blackwell architecture the NVFP4 text encoder targets. For **AU, IN, JP and NO, no
purpose beyond widening placement capacity is documented**, and ADR-002 states plainly that none is.
Under a provision that weighs purpose, four of six territories are held for capacity headroom rather
than for a stated need — and ADR-002's own survey found that NO and AU "carry nothing usable" today.

**IS and NO are held on a provisional reading.** Both are licensable only through ADR-001's reading
of "the European Union" as Union membership rather than EEA geography. If the licensor answers that
the EEA is in scope, both fall out. That is a licence question, not a privacy one, but it means two
of the six territories in this assessment's scope may not be in scope for long.

## 4. Protection measures

The split below is the point of this section. A measure is **enforced** only if a named file in this
repository refuses the unsafe case; everything else is **asserted**.

### 4.1 Enforced — provable in code

| # | Measure | Enforced by |
|---|---|---|
| E-1 | The object never leaves a reviewed Canadian region at rest. Endpoint and region are validated as a *pair* — scheme must be `https`, region must start `ca-`, hostname must be exactly `s3.{region}.backblazeb2.com`, and no path, params, query or fragment may redirect a signed request. | `src/tkr_cloud_video/core/storage.py`, `validate_canadian_b2_endpoint`. Called at construction in `src/tkr_cloud_video/adapters/rclone.py:69`, `src/tkr_cloud_video/console/presign.py:81`, `src/tkr_cloud_video/console/settings.py:98` — so no client in the product can be pointed elsewhere by configuration. |
| E-2 | The credential that reads inputs can only read them. `INPUT_READER` holds exactly `listFiles` and `readFiles`; no role in the product holds `writeFiles` on the inputs prefix. A scope whose capabilities differ from its role's exact set is refused at construction, as is any prefix that is not one normalized namespace. | `src/tkr_cloud_video/security/credentials.py`, `ROLE_CAPABILITIES` and `CredentialScope.__post_init__`. |
| E-3 | The process that actually sees the image pixels holds no object-store credential at all. `ProcessRole.COMFYUI` maps to an empty secret allowlist, and handing any process a secret outside its role's allowlist raises `SecretExposureError`. | `src/tkr_cloud_video/security/process_secrets.py`, `SECRET_VARIABLES` and `ProcessEnvironmentBuilder.build`. |
| E-4 | An unauthorised reference is refused **before** any download, and the refusal is indistinguishable from absence. | `src/tkr_cloud_video/jobs/input_staging.py`, `InputStager.stage` — `authorized()` gates the download; failure raises `input_unauthorized`. |
| E-5 | Staging is bounded and verified: size checked against both the reported and the actual byte count, caller-supplied digest verified when present, media type restricted to a three-item allowlist, promotion atomic via `os.replace`, and the partial file unlinked in a `finally` on every path. | `src/tkr_cloud_video/jobs/input_staging.py`. Byte cap `maximum_input_bytes` defaults to 250 MB in `src/tkr_cloud_video/runtime/config.py`. |
| E-6 | The local copy on the worker is written into a `0o700` directory named only from server-owned identity, and the whole attempt workspace is removed in a `finally` regardless of outcome. | `src/tkr_cloud_video/jobs/workspace.py` (`allocate`, `cleanup`); called at `src/tkr_cloud_video/application.py:90` and `:177`. |
| E-7 | At most four references per request, and the object key is validated as an object-store key independently of any filesystem path. | `src/tkr_cloud_video/jobs/contracts.py`, `InputReference.validate_key` and the `max_length=4` bound. |
| E-8 | A retention period for inputs exists, is total, and cannot silently default to unbounded — a retained class with no configured period fails at construction rather than persisting forever. Declared as 7 days (`config/lifecycle.json`, version 3) and projected onto a bucket rule that hides at 7 days and deletes 1 day after, so the object is unreachable on the declared day rather than merely superseded on it. | `src/tkr_cloud_video/delivery/lifecycle.py` (`LifecyclePolicy.__post_init__` totality check); `src/tkr_cloud_video/delivery/lifecycle_rules.py` (`LifecycleRuleSet.project`, `INPUT_PREFIX`). |
| E-9 | Drift between the declared rules and the rules in force is detectable and names the prefix that stopped being enforced. Exposed as `lifecycle check` / `apply` / `sweep`. | `src/tkr_cloud_video/delivery/lifecycle_rules.py`, `detect_drift`; `src/tkr_cloud_video/operations/retention_command.py`; `src/tkr_cloud_video/cli.py:90`. |
| E-10 | Only the control plane can delete. `RETENTION_REAPER` is the sole holder of `deleteFiles`, appears in no worker process-role allowlist, and the delete-capable store is optional in composition — a process without it is structurally incapable of deleting rather than trusted not to. | `src/tkr_cloud_video/security/credentials.py`; `src/tkr_cloud_video/durable_delivery/composition.py`. |
| E-11 | Erasure of a presented record reaches the supplied input objects, and reports honestly what it could not reach. Unauthorised and absent share one outcome; a store failure raises rather than reporting an empty holding. | `src/tkr_cloud_video/delivery/subject_requests.py`, `SubjectRequestService.erase` / `describe`. |
| E-12 | Deployment placement is bound to the ratified territory grant before the write, read back after it, and asserted in the test suite; an unregistered region is denied and reported rather than guessed. | `src/tkr_cloud_video/security/territories.py`; `scripts/set_endpoint_datacenters.sh`; `tests/security_foundation/test_deployed_placement.py`. Deploy-time and CI-time only — see A-6. |

### 4.2 Asserted — not enforced by anything in this repository

- **A-1 — That the provider's workers retain no copy.** E-6 proves this product deletes its own
  copy from the worker's filesystem. It proves nothing about the container image layer, the host
  disk, the provider's snapshots, or what survives a container's teardown. Nothing in this
  repository can observe that, and no provider attestation covering it exists here.
- **A-2 — That the GPU provider is under any contractual protection obligation.** This is triage
  item 3, and it is unchanged: no term of the provider arrangement has been assessed against any
  comparable-protection requirement. There is no data-processing agreement, no vendor assessment,
  and no record of one having been sought, anywhere in this repository.
- **A-3 — That the six territories afford comparable protection.** No adequacy, equivalence or
  comparability finding exists. ADR-002 says so in its own words: "The privacy and data-residency
  question is untouched and unrecorded." The territory set was chosen by *licence* permission and
  GPU supply, not by protection.
- **A-4 — That the declared retention rules are actually in force on the live bucket.** E-8 and E-9
  make the rules declarable and drift detectable; neither proves the current state of
  `ca-east-006`. `lifecycle check` has not been run against the live bucket as part of this
  assessment, and its output is not recorded here.
- **A-5 — That the depicted individual consented.** Nothing asks, records or verifies. See §2.
- **A-6 — That a job is refused at request time if its worker's territory falls outside the grant.**
  `ReleaseAssuranceGate` checks territory, but it still has **no runtime caller** — it is composed in
  `src/tkr_cloud_video/security_foundation/composition.py:53` and invoked only by tests. This is the
  first of the two gaps ADR-002 declined to close, and it is still open. The deploy-time and
  CI-time checks in E-12 are what stands in its place.
- **A-7 — That transit is protected beyond TLS to the object store.** E-1 forces `https` on the
  endpoint. The security of the leg between the provider's host and the ComfyUI process, and of the
  provider's own internal network, is assumed.

### 4.3 Two findings that qualify the measures above

**F-1 — The reference's key and digest outlive the reference itself, by a factor of 52.** The
committed job evidence at `outputs/<job>/<attempt>/generation.json` embeds
`request.model_dump(mode="json")` (`src/tkr_cloud_video/application.py`), which for these two modes
contains the `object_key` and `sha256` of every reference supplied. That evidence is retained under
`prompt-evidence`/`deliverable`, both 365 days in `config/lifecycle.json`, while the image object
itself expires at 7. For 358 days after the image is gone there remains a durable record naming
exactly which object was used and its content digest — enough to confirm a suspected image was
supplied to a given job. This is not a leak of the likeness, but it is personal information about
the processing that survives the processing, and no measure above bounds it.

**F-2 — An access request will not disclose that an input reference is held.**
`SubjectRequestService.describe` hard-codes `held = [VIDEO_FIELD, PROMPT_FIELD]`
(`src/tkr_cloud_video/delivery/subject_requests.py`). `INPUT_FIELD` is defined in that module and
used only by the erasure path's `_field_for`. Because inputs do not live under the `outputs/`
prefix the describe path lists, a subject asking what is held is told about the video and the prompt
and not about their image. Erasure *does* reach input objects — but only those the requester
names, since the job identity cannot enumerate them. So the product can delete an input it will not
admit to holding.

## 5. Verdict

**Qualified pass, conditional.** Reference-image intake may proceed for the six territories named in
§3, for internal single-operator deployment use, on the two conditions below and subject to the
residual risks in §6 being accepted by name.

The reasoning: the sensitivity is high and the subject is a third party, but the measures that
matter most for a cross-border read are the ones that are actually enforced. The image stays in a
Canadian bucket that configuration cannot move (E-1); the credential that reaches it cannot write or
delete it (E-2, E-10); the process that renders it holds no credential at all (E-3); the local copy
is removed on every path (E-6); and there is a real, total, drift-checkable 7-day expiry (E-8, E-9)
rather than the indefinite retention the triage record describes. Against that, the protections
that would bear on *the territories themselves* — a provider obligation, a comparability finding, a
runtime territory check — are all asserted or absent. The read crosses the border under measures
this product controls and none the provider owes.

**This is not a clean pass, and it should not be renewed as one.**

Two conditions precede the first staged image:

1. **C-1.** `tkr-cloud-video lifecycle check` is run against `ca-east-006` and its output recorded,
   converting A-4 from an assertion into evidence. A 7-day input period that is declared but not in
   force is the exact shape of the mistake `src/tkr_cloud_video/delivery/lifecycle.py` documents in
   its own module docstring.
2. **C-2.** F-2 is closed, or accepted by name below. A product that cannot tell a subject it holds
   their image should not begin holding more of them silently.

**Scope discipline.** This record covers reference images. It does not cover prompt text, which
already crosses these borders and is governed by triage item 1; it does not cover output video
(item 3); and it does not cover audio or voice references, which are a different kind of material
and are the subject of a separate assessment. Reading this verdict as reaching any of them would be
reading it wider than it was conducted.

## 6. Residual risks, and who accepts them

Each of these is a real exposure that this record does **not** eliminate. They are listed so that
accepting them is a decision someone makes, not a silence.

| # | Residual risk | Source |
|---|---|---|
| R-1 | A reference image depicting an identifiable third party is read by workers in Iceland, Norway, India, Japan and Australia under no assessed contractual protection obligation on the provider. | A-2, A-3 |
| R-2 | No consent from the depicted individual is sought, recorded or verifiable, and the output is a moving likeness of them. | A-5, §2 |
| R-3 | Four of the six territories (AU, IN, JP, NO) are held for capacity headroom with no documented purpose, and two of them supply no usable capacity today. Under a provision that weighs purpose, they are exposure without a stated need. | §3, ADR-002 |
| R-4 | The 7-day input expiry is declared and projectable but unverified against the live bucket at the date of this record. | A-4, C-1 |
| R-5 | The reference's object key and content digest persist in job evidence for 365 days after the image itself is deleted at 7. | F-1 |
| R-6 | A subject access request does not disclose that an input reference is held, and erasure reaches only the input keys the requester can name. | F-2 |
| R-7 | No runtime check refuses a job whose worker territory falls outside the grant; deploy-time scripts and CI tests are the only barrier, and a manual provider-side placement change would not be caught until the next run of either. | A-6 |
| R-8 | Nothing in this repository can establish what the provider retains of the image after an attempt ends. | A-1 |
| R-9 | IS and NO rest on a provisional licence reading. If the licensor answers that the EEA is in scope, two of the six assessed territories become unlicensed, and this record's territory scope is wrong from that date. | §3, ADR-001 |

**Acceptance.**

> The risks R-1 through R-9 above are accepted, in the terms stated, for internal single-operator
> deployment use, until this record is superseded or until the ratified licence approval expires on
> 2026-11-14, whichever is sooner.
>
> Accepted by: `____________________________` *(name and role — to be completed by the accountable
> operator; this record is not in force until this line is filled in)*
>
> Date accepted: `____________________`

The verdict in §5 is a proposal until the line above carries a name. The evidence in this record was
gathered; the acceptance is signed. An unsigned record has not lifted the gate, and the dependent
intake slice stays blocked.

## 7. Renewal

**This record is append-only.** It is never edited to reflect a change in what it reviewed. Renewal
is a **new dated file** in `docs/assurance/`, and this one is kept: what was accepted, and when, is
the record.

Renew when any of the following changes:

- The territory set or the configured region list — including adding CA-MTL-4, EUR-IS-4, EUR-NO-2 or
  AP-IN-2 when the provider's REST enum catches up, and including IS and NO falling out on a
  licensor answer.
- Any measure in §4 moves between enforced and asserted, in either direction — closing A-6 or C-2
  renews this record as surely as losing E-1 would.
- The declared input retention period in `config/lifecycle.json`, or the policy version.
- The deployment use widens beyond internal single-operator, or the console stops binding loopback.
- The scope widens to material this record did not consider — audio or voice references most
  obviously, which are their own assessment and are not reached by this one.
- The ratified licence approval expires (2026-11-14) or is replaced.

**Staleness is a gate, not a note.** A record whose reviewed subject has changed has not been
renewed by being read.
