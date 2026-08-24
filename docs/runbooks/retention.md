# Enforced retention provisioning and sweep

Trigger: first provisioning of the declared retention policy on a bucket, a
periodic drift check, a scheduled expiry sweep, or a `lifecycle_rule_*` failure
reported by `check`.

Bucket rules were first provisioned on 2026-08-23: `input`, `hidden-version`
and `multipart` are enforced by the provider, and a confirming `check` reports
`rules in force match policy 3`. No sweep has run, so `failed-attempt`,
`scratch`, `deliverable` and `prompt-evidence` remain declarations with no
enforcing mechanism and nothing has ever been deleted. Read the first-sweep
section before running step 6.

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

## The two control-plane keys

This procedure needs two Backblaze application keys, not one. The provider
refuses `writeBuckets` on a key restricted to a bucket — its capability
reference says of that option, and of `deleteBuckets` alone among the
capabilities, "This option is not allowed for app keys that are restricted to a
bucket." So a key able to rewrite this bucket's lifecycle rules cannot also be
confined to it, and a key confined to it cannot rewrite them.

Splitting them is the stronger arrangement regardless. The lifecycle key holds
no file capability, so it cannot read or delete a single object in any bucket.
The reaper holds no bucket capability, so it cannot widen the rules that bound
it. Neither can become the other.

Neither key can be created in the web console, which offers only Read Only,
Write Only, and Read and Write — "Read and Write" would grant `writeFiles` and
`shareFiles`, which the reaper role deliberately excludes. Use the B2 CLI, which
names capabilities individually.

`scripts/create_retention_keys.sh` runs both creations and stores all four
values in the vault. It reads the master key into the environment the B2 CLI
consults — never onto a command line — points the account cache at a temporary
file it removes on exit, and echoes no key. Pass `--rotate` to replace keys that
are already stored; without it, it refuses rather than issuing a second pair.

Run it from a terminal, where it prompts:

```bash
scripts/create_retention_keys.sh
```

It prompts on `/dev/tty` rather than stdin, so a wrapper that redirects stdin
does not turn the prompt into a silent exit. Where there is no terminal at all —
an editor's command runner, a `!` escape, CI — name a two-line file instead,
which keeps the credential out of shell history. The script deletes the file
however it exits:

```bash
printf '%s\n%s\n' '<keyID>' '<applicationKey>' > /tmp/b2-master
chmod 600 /tmp/b2-master
scripts/create_retention_keys.sh --credentials-file /tmp/b2-master
```

It restricts the reaper to whatever `TKR_B2_BUCKET_NAME` holds rather than to a
literal, so the key cannot be scoped to a bucket the sweep does not address. The
two creations it performs are:

```bash
b2 key create --bucket <bucket> \
    retention-reaper listBuckets,listFiles,readFiles,deleteFiles
b2 key create retention-rules listBuckets,writeBuckets
```

Each prints its key once and never again, which is why the script stores them
rather than showing them.

**`retention-reaper`** — bucket-restricted, used by `sweep`. Its object
capabilities are the role's declared set in
`src/tkr_cloud_video/security/credentials.py`: `listFiles`, `readFiles`, and
`deleteFiles`. `deleteFiles` is held by no other role in the project —
`model-reader`, `input-reader`, `output-writer`, and `delivery-reader` all lack
it, which is why nothing a worker runs can delete. The role deliberately has no
`writeFiles` and no `shareFiles`: the reaper cannot replace what it removes and
never issues a link. `listBuckets` is added because the B2 tooling requires it
to authorize at all; it is allowed on a bucket-restricted key, which must then
name its bucket in the request, and `b2_lifecycle.py` does. Do not set a
file-name prefix restriction: bucket configuration is not prefix-scopable, and
subject erasure reaches `inputs/` as well as `outputs/`.

**`retention-rules`** — account-wide, used by `check` and `apply`. Account-wide
is not a widening: with no `readFiles`, `writeFiles` or `deleteFiles` it cannot
touch an object anywhere in the account. It can only read and rewrite bucket
configuration, which is precisely `b2_list_buckets` and `b2_update_bucket`. Add
`--duration 3600` if you would rather it expire after the run; drift repair
later then needs a fresh one.

Both pairs live in the project vault, which is where
`provision_lifecycle_rules.sh` reads them. The creation script writes them; to
set one by hand:

```bash
tkr op secrets.set_secret --vaultId project:tkr-cloud-video \
    --name B2_REAPER_KEY_ID --value <reaper key id>
```

The vault opens from the credential chain (OS keychain, then
`TKR_VAULT_PROJECT_TKR_CLOUD_VIDEO_PASSWORD`, then a `0600` password file), so
no secret needs exporting to run any mode below.

Neither credential may enter a worker. `SECRET_VARIABLES` in
`src/tkr_cloud_video/security/process_secrets.py` is the allowlist of variables
each process role may be handed, and none of the four appear in any of them —
pinned by `test_reaper_credential_reaches_no_worker_process` and
`test_lifecycle_credential_reaches_no_worker_process`. The lifecycle key is on
that side of the boundary despite deleting nothing: a worker able to rewrite
retention rules could extend its own retention. Do not add them to a RunPod
template, `Dockerfile`, worker image layer, or serverless environment. The vault
is read by the operator's own process at run time and never by a worker, which
is what keeps that boundary true while the credentials live somewhere durable.
Revoke a key if it is ever exported anywhere else.

## Environment

Each mode requires three values and no more. They are read from the project
vault when not already exported. Nothing is defaulted: a missing bucket must not
resolve to another bucket and a missing credential must not fall back to an
ambient one.

`check` and `apply` — bucket configuration, on the lifecycle credential:

| Variable | Vault name | |
| --- | --- | --- |
| `B2_LIFECYCLE_KEY_ID` | `B2_LIFECYCLE_KEY_ID` | |
| `B2_LIFECYCLE_APPLICATION_KEY` | `B2_LIFECYCLE_APPLICATION_KEY` | |
| `B2_BUCKET_ID` | `TKR_B2_BUCKET_ID` | the bucket whose lifecycle rules are read and written |

`sweep` — object expiry, on the reaper credential:

| Variable | Vault name | |
| --- | --- | --- |
| `B2_REAPER_KEY_ID` | `B2_REAPER_KEY_ID` | |
| `B2_REAPER_APPLICATION_KEY` | `B2_REAPER_APPLICATION_KEY` | |
| `B2_BUCKET_NAME` | `TKR_B2_BUCKET_NAME` | the same bucket, as rclone addresses it |

A mode requires only its own pair. Running `sweep` does not ask for the
lifecycle key and `check` does not ask for the reaper — an operator holding one
key can run its mode without holding the other, which is what makes the split a
boundary rather than a naming convention. `MODE_REQUIREMENTS` in
`operations/retention_command.py` is the single declaration of this, read by
both the script and the typed layer.

The bucket name is `TKR_B2_BUCKET_NAME` in the vault because that is what
`deploy_runpod_endpoint.sh` and `verify_evidence.sh` already call it. One bucket
must not acquire a second vault name that can disagree with the first.

An explicit export wins over the vault, which is what makes a one-off run
against a different bucket possible without editing the vault. That override is
also how the wrong bucket gets addressed, so the script reports the resolved
source of every value and names the bucket before any mode runs:

```
retention: B2_LIFECYCLE_KEY_ID from vault (B2_LIFECYCLE_KEY_ID)
retention: B2_BUCKET_ID from environment
retention: addressing bucket <id or name> (check)
```

Read those lines. A credential's source is reported; its value never is.

If a value resolves from neither source the script exits 2 naming every
unresolved variable the mode needs, rather than the first one it happened to
check.

Two are optional:

- `B2_REMOTE_NAME` — rclone configuration section name, default `tkr-retention`
- `B2_BASE_PREFIX` — namespace the sweep addresses, default `outputs/`
- `B2_RCLONE_EXECUTABLE` — absolute path to rclone for the sweep. The typed
  default is the path the worker image pins, which does not exist on an
  operator machine; the script resolves the operator's own from PATH and
  reports which binary it will run. Set this only to pin a specific one.

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

1. Confirm the six values are in the vault (`tkr op secrets.list_secrets
   --vaultId project:tkr-cloud-video`) and that neither credential pair appears
   in any worker template. Export nothing unless you are deliberately overriding
   a value for this run. `B2_BASE_PREFIX` needs no value on a root-layout
   bucket.
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
# nothing to export: every value resolves from project:tkr-cloud-video
scripts/provision_lifecycle_rules.sh check     # expect drift on a new bucket
scripts/provision_lifecycle_rules.sh apply     # rules become enforced here
scripts/provision_lifecycle_rules.sh check     # expect: match policy 3
scripts/provision_lifecycle_rules.sh sweep     # reports only
RETENTION_DRY_RUN=false scripts/provision_lifecycle_rules.sh sweep
```

The script execs `${PYTHON:-python3}`, which is not this project's virtualenv.
Run it with `PYTHON=.venv/bin/python` or with the environment activated.

## First sweep on this bucket

The first `apply` landed on 2026-08-23 against an inventory that gave its rules
nothing to act on: `inputs/` held zero objects, there were no unfinished large
files, and the bucket was twelve days old so no version could have been hidden
for thirty. That is why provisioning deleted nothing, and it is not a property
to assume on the next bucket — establish the inventory before `apply`, because
objects already older than their period are acted on by the provider on its own
schedule afterwards, without further operator action.

The first real sweep has not run. It faces whatever backlog exists: every
uncommitted attempt older than 14 days and every committed attempt older than
365 days is deleted in one pass, including the `generation.bin` carrying the
prompt text. Deletion is unrecoverable. The dry-run output from step 5 is the
only review this gets, so do not skip it and do not run step 6 in the same
sitting if the counts are larger than expected.

On this bucket, as of the provisioning date, that backlog is bounded by the
bucket's own age. It was created 2026-08-11, so nothing in it can be older than
the `failed-attempt` period of 14 days, let alone the 365-day `deliverable` and
`prompt-evidence` periods. Only `scratch` (2 days) can have aged out. A dry run
reporting deliverable or failed-attempt candidates on this bucket is reporting
clock skew or the wrong bucket, not a policy decision.

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
  value resolved from neither the environment nor the vault (the script names
  every unresolved variable, and the typed layer behind it raises
  `retention_environment_incomplete` for the same condition); or
  `config/lifecycle.json` could not be read or validated. Nothing was contacted
  and nothing changed.

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
every run names its bucket and the source of each resolved value while printing
no credential; a sweep runs without the lifecycle credential and a check without
the reaper; neither credential pair appears in a process-role allowlist or a
worker image;
committed attempts survive the failed-attempt period while uncommitted ones do
not; an attempt whose marker cannot be read is retained; the commit marker is
the last object removed from any attempt; classes matching no objects are
stated rather than silently absent.

rehearsal: 2026-08-23 — `check`, `apply`, a confirming `check`, and a dry-run
`sweep` were run against `tkr-cloud-video-aba33dd61d3e` on the credentials this
document describes. `check` on an unprovisioned bucket returned
`lifecycle_rule_missing` naming `<bucket>,inputs/` at exit 1; `apply` reported
`applied 2 rules from policy 3`; the confirming `check` reported `rules in force
match policy 3` at exit 0. The dry-run sweep reported `evaluated 11 attempts,
would delete 0 objects, 0 unresolved`, with `failed-attempt`, `prompt-evidence`
and `scratch` matching no objects — consistent with a bucket twelve days old
whose shortest reconciler period is two days. A non-zero attempt count is the
evidence that the base prefix addresses the namespace the workers write to.

The run found three defects this document had asserted away. The drift codes
carry the offending prefix in their error context and the operator entrypoint
printed only the code and message, so the refusal was correct and anonymous. The
sweep resolved rclone at the absolute path the worker image pins, which does not
exist off-image, so the first sweep failed on a machine the procedure is written
for. And that failure surfaced as a raw `OSError` traceback rather than the
coded exit the status contract promises, because the executor classified
timeouts, oversized output and non-zero returns but not a missing binary. All
three are fixed and pinned in `tests/durable_delivery/test_retention_adapters.py`.

Step 6 has still never run. On this bucket the dry run says it would delete
nothing, so running it would prove the deleting path only in the sense that it
completes — it would not prove that a delete is issued. Treat the deleting path
as unrehearsed until a sweep with something to remove is recorded here.
