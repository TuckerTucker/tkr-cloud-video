# Enforced retention provisioning and sweep

Trigger: first provisioning of the declared retention policy on a bucket, a
periodic drift check, a scheduled expiry sweep, or a `lifecycle_rule_*` failure
reported by `check`.

This bucket has never had lifecycle rules applied. Until the first successful
`apply`, every period below is a declaration with no enforcing mechanism, and
nothing has ever been deleted. Read the first-run section before running
anything.

## Declared policy

`config/lifecycle.json` is the source of truth. It is policy version `3` and
declares seven classes, in days:

| Class | Days | Enforced by |
| --- | --- | --- |
| `input` | 7 | bucket rule on `inputs/` (hide at 7, delete 1 day after hiding) |
| `failed-attempt` | 14 | reconciler sweep |
| `scratch` | 2 | reconciler sweep |
| `deliverable` | 365 | reconciler sweep |
| `hidden-version` | 30 | bucket-wide rule (delete 30 days after hiding) |
| `multipart` | 2 | bucket-wide rule (cancel unfinished large files at 2) |
| `prompt-evidence` | 365 | reconciler sweep, with the deliverable it belongs to |

`deliverable` and `failed-attempt` share the `outputs/` prefix and B2 resolves
overlapping rules by applying the *smallest* non-null value, so expressing both
as prefix rules would delete deliverables on the 14-day clock. Those classes are
therefore never bucket rules; the reconciler separates them by heading
`result.json`. Do not add an `outputs/` prefix rule by hand.

## The RETENTION_REAPER key

Create one Backblaze application key named for the reaper role, restricted to
the single retention bucket.

Object capabilities are the role's declared set in
`src/tkr_cloud_video/security/credentials.py`: `listFiles`, `readFiles`, and
`deleteFiles`. `deleteFiles` is held by no other role in the project —
`model-reader`, `input-reader`, `output-writer`, and `delivery-reader` all lack
it, which is why nothing a worker runs can delete. The role deliberately has no
`writeFiles` and no `shareFiles`: the reaper cannot replace what it removes and
never issues a link.

`check` and `apply` call `b2_list_buckets` and `b2_update_bucket`, so the same
key additionally needs `listBuckets` and `writeBuckets`. Do not set a file-name
prefix restriction on the key: bucket configuration is not prefix-scopable, and
subject erasure reaches `inputs/` as well as `outputs/`.

This credential must never enter a worker. `SECRET_VARIABLES` in
`src/tkr_cloud_video/security/process_secrets.py` is the allowlist of variables
each process role may be handed, and `B2_REAPER_KEY_ID` /
`B2_REAPER_APPLICATION_KEY` appear in none of them — that is pinned by
`test_reaper_credential_reaches_no_worker_process`. Do not add them to a RunPod
template, `Dockerfile`, worker image layer, or serverless environment. Supply
them only in the operator shell that runs this procedure, and revoke the key if
it is ever exported anywhere else.

## Environment

Four values are required. Nothing is defaulted: a missing bucket must not
resolve to another bucket and a missing credential must not fall back to an
ambient one.

- `B2_REAPER_KEY_ID`
- `B2_REAPER_APPLICATION_KEY`
- `B2_BUCKET_ID` — the bucket whose lifecycle rules are read and written
- `B2_BUCKET_NAME` — the same bucket, as rclone addresses it

Two are optional:

- `B2_REMOTE_NAME` — rclone configuration section name, default `tkr-retention`
- `B2_BASE_PREFIX` — namespace the sweep addresses, default `outputs/`

Leave `B2_BASE_PREFIX` alone unless the bucket does not use the root layout.
Workers write `outputs/` and `inputs/` at the bucket root (`TKR_OUTPUT_PREFIX` /
`TKR_INPUT_PREFIX` in `runtime/config.py`), the bucket rules this procedure
applies are written for that layout, and the default now matches it. The value
matters more than it looks: the sweep lists `outputs/` *through* this prefix, so
a base naming any other namespace addresses something like `tkr/outputs/`, finds
nothing, and reports `evaluated 0 attempts` — a clean-looking result that has
enforced nothing. If you override it, verify the first dry-run sweep evaluates a
non-zero number of attempts on a bucket you know holds aged-out objects. The
value must be a normalized namespace ending in `/`; it cannot be empty.

One switch controls whether the sweep deletes:

- `RETENTION_DRY_RUN` — default `true`. Only the exact value `false`
  (case-insensitive) enables deletion; any other value, including a typo or an
  empty string, leaves the sweep in reporting mode.

`PYTHON` overrides the interpreter the script execs (default `python3`).

## Procedure

Run every mode through the script, never by hand against the provider. The
policy lives in typed Python, so the rules applied cannot diverge from the
declared file.

1. Export the four required variables in the operator shell only. Confirm they
   are absent from every worker template first. `B2_BASE_PREFIX` needs no value
   on a root-layout bucket.
2. `scripts/provision_lifecycle_rules.sh check` — report drift, change nothing.
   On a bucket that has never been provisioned this fails with
   `lifecycle_rule_missing`, naming `inputs/` and `<bucket>`. That is the
   expected first-run answer, and it is the baseline for step 3.
3. `scripts/provision_lifecycle_rules.sh apply` — write the declared rules.
   Expect `retention: applied 2 rules from policy 3`. This is the moment the
   declared periods start being enforced by the provider.
4. `scripts/provision_lifecycle_rules.sh check` again — confirm. Expect
   `retention: rules in force match policy 3` and exit 0. Do not treat step 3
   as complete without this.
5. `scripts/provision_lifecycle_rules.sh sweep` with `RETENTION_DRY_RUN` unset
   or `true`. This deletes nothing. Read the report in full: the counts under
   `would delete` are objects, not attempts, and on a bucket that has never
   been swept they include every attempt that aged out since the product began
   writing.
6. Only after the dry-run output is understood and accepted, re-run with
   `RETENTION_DRY_RUN=false scripts/provision_lifecycle_rules.sh sweep`.
   Compare the deleted counts against the dry run; they should agree except for
   objects written between the two runs.

```bash
export B2_REAPER_KEY_ID=... B2_REAPER_APPLICATION_KEY=...
export B2_BUCKET_ID=... B2_BUCKET_NAME=...

scripts/provision_lifecycle_rules.sh check     # expect drift on a new bucket
scripts/provision_lifecycle_rules.sh apply     # rules become enforced here
scripts/provision_lifecycle_rules.sh check     # expect: match policy 3
scripts/provision_lifecycle_rules.sh sweep     # reports only
RETENTION_DRY_RUN=false scripts/provision_lifecycle_rules.sh sweep
```

## First run on this bucket

No rules have ever been in force and no sweep has ever run, so two things are
true only once:

- The first `apply` is the moment the `input`, `hidden-version`, and `multipart`
  periods become real. Objects already older than their period are acted on by
  the provider on its own schedule after that, without further operator action.
- The first real sweep faces a backlog. Every uncommitted attempt older than 14
  days and every committed attempt older than 365 days is deleted in one pass,
  including the `generation.bin` carrying the prompt text. Deletion is
  unrecoverable. The dry-run output from step 5 is the only review this gets, so
  do not skip it and do not run step 6 in the same sitting if the counts are
  larger than expected.

The sweep fails closed toward retention: an attempt whose commit marker cannot
be read is classified as a deliverable and kept. A first run that reports
skipped deliverables it cannot explain is reporting provider trouble, not a
policy decision.

## Exit statuses

- `0` — the mode succeeded. `check` found no drift, `apply` wrote the rules, or
  `sweep` completed with no unresolved objects.
- `1` — `check` found drift; or `apply`/`sweep` hit a typed provider or policy
  failure; or `sweep` completed with `failures > 0`, meaning at least one
  delete was refused. A sweep with unresolved failures is never a clean sweep;
  re-run it after fixing the refusal rather than assuming the remainder went.
- `2` — the mode argument was not `check`, `apply`, or `sweep`; a required
  variable was empty (`retention_environment_incomplete`, which names every
  missing variable); or `config/lifecycle.json` could not be read or validated.
  Nothing was contacted and nothing changed.

## When check reports drift

The error names the diverging rule so the operator learns which class stopped
being enforced, not merely that something did.

- `lifecycle_rule_missing` — a declared rule is not on the bucket. Named by
  prefix, with the bucket-wide rule shown as `<bucket>`. That class is currently
  unenforced.
- `lifecycle_rule_unexpected` — the bucket carries a rule the policy does not
  declare. Someone changed rules out of band. An unexpected `outputs/` rule is
  the dangerous case: it can delete deliverables on the failed-attempt clock.
- `lifecycle_rule_diverged` — a declared prefix is present with different
  periods.
- `bucket_rule_field_unknown` — the bucket carries a rule field this project
  does not model. The check refuses rather than dropping it, because silently
  ignoring it would report an agreement it has not established. Do not run
  `apply` to clear this; investigate the field first, since `apply` replaces the
  bucket's entire rule set.

In every case `config/lifecycle.json` is the source of truth. Change the
declared document and re-project, never the bucket by hand. Resolve drift with
`apply` followed by a confirming `check`.

Expected observables: `check` is read-only and leaves rule state untouched; a
sweep with `RETENTION_DRY_RUN` unset deletes nothing and says `would delete`;
the reaper variables appear in no process-role allowlist and no worker image;
committed attempts survive the failed-attempt period while uncommitted ones do
not; an attempt whose marker cannot be read is retained; the commit marker is
the last object removed from any attempt; classes matching no objects are
stated rather than silently absent.

rehearsal: none — no `B2_REAPER_*` credential exists in any environment, so no
mode of this procedure has been run against a live bucket. The three modes,
their exit statuses, and the drift codes are exercised only against fakes in
`tests/durable_delivery/test_retention_adapters.py`. Until a first run is
rehearsed and dated here, treat this document as a hypothesis.
