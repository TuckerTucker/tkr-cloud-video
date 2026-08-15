# Generation latency and its measurement — 2026-08-15

**Bar.** Every phase of a generation is attributable from committed evidence, and fixed
overhead is not a material fraction of wall clock.

The product fails the first half of that bar outright: nothing records how long any phase
took, so the split below is *inferred from two runs* rather than measured. It fails the
second half by roughly 28%. Findings are grouped as **A** (what the runs measured, and the
gap that made inference necessary) and **B** (configuration that spends the time).

Measured against the first two structured-prompt generations to complete end to end, both
on `feat/retention-reconciler` at `84f24a2`, endpoint `176tpna3ogl94t`.

## The two runs

| | Run A | Run B |
|---|---|---|
| Request | `a93d0c19-…-u1` | `46cae39c-…-u1` |
| Job | `job-2ea577062383db85fd880d4adc8030c4` | `job-33fdb1d372a3eff8675914227da67f0c` |
| Frames | 73 | 243 |
| Canvas | 1344×768 @ 24 fps | 1344×768 @ 24 fps |
| `delayTime` | 7,471 ms | 11,032 ms |
| `executionTime` | **739,856 ms** (12 m 20 s) | **1,498,242 ms** (24 m 58 s) |
| Worker | `2c69xe1ic6w73m` | `u687zngv5hletu` |
| Worker state | cold | cold |

Both workers were cold, so both paid a full model hydration. Neither run was warm, which is
why the fixed term below cannot be isolated from these two runs alone.

## A — what the runs measured

**A-01 · Sampling costs ~4,461 ms per frame; fixed overhead is ~414 s.** **[V]**
Solving the two runs as a line through (73, 739856) and (243, 1498242) gives 4,461.1 ms per
frame and a 414,196 ms (6 m 54 s) constant. For Run B that is 1,084,046 ms of sampling
(18.1 min) against 414,196 ms of overhead — **28% of wall clock spent not sampling**.
Location: the runs above.

**A-02 · The line cannot validate itself.** **[V]**
A two-point fit reproduces both points exactly by construction, so agreement with the
observed totals is arithmetic, not evidence. The split is a hypothesis with one free
parameter per observation. It is stated here so it is not mistaken for a measurement.
Location: A-01.

**A-03 · The only independent corroboration is an eyeball reading.** **[R]**
The operator observed the worker's disk at ~21 GB roughly 4½ minutes into Run A. Against a
42.5 GB model set that implies ~78 MB/s and a full hydration near 7–9 minutes, which is
consistent with A-01's 6 m 54 s constant. Reported during the run, not instrumented, and
consistent with a range rather than a value.
Location: worker `2c69xe1ic6w73m`, Run A.

**A-04 · No phase timing is recorded anywhere.** **[V]**
`generation.bin` carries `media`, `model_set_id`, `prompt_provenance` and the request, and
no elapsed time. `grep` for `elapsed|duration_ms|started_at|timing|perf` across
`src/tkr_cloud_video/jobs/` and `src/tkr_cloud_video/adapters/comfy.py` returns nothing.
Hydration, model load, sampling, VAE decode, encode and commit are therefore
indistinguishable in the committed record, and every one of them wants a different fix.
This is the finding that makes A-01 necessary and A-02 unavoidable.
Location: `src/tkr_cloud_video/jobs/`, `src/tkr_cloud_video/adapters/comfy.py`.

**A-05 · Per-step cost is ~54 s at 243 frames.** **[V]**
1,084,046 ms of inferred sampling over the workflow's 20 steps. Inherits A-02's caveat.
Location: `docs/audits/2026-08-15-generation-latency.md` A-01, workflow node `9`.

**A-06 · Run A sat below the model's trained frame range.** **[V]**
The trained range is roughly 124–362 frames; Run A used 73. Run B at 243 is the first
generation inside the envelope, so it is the better of the two samples for any timing claim
and the only one whose quality is comparable to intended use.
Location: `src/tkr_cloud_video/jobs/contracts.py`, and the finding recorded as
`find-xfjI7pbm`.

## B — configuration that spends the time

Read from the live endpoint and the workflow committed with Run B.

**B-01 · No network volume is attached, so every cold worker pulls 42.5 GB.** **[V]**
`networkVolumeId` is empty. The model set is hydrated from B2 into container disk on every
cold start, which is the bulk of A-01's fixed term. A mounted volume removes nearly all of
it and cannot affect output, since the bytes are identical and digest-pinned.
Location: endpoint `176tpna3ogl94t`, `networkVolumeId`.

**B-02 · `flashboot` is disabled.** **[V]**
`flashboot: False`. Snapshot restore targets exactly this cold-start shape.
Location: endpoint `176tpna3ogl94t`, `flashboot`.

**B-03 · Every run is cold by configuration.** **[V]**
`workersMin: 0` with `idleTimeout: 300`. Consecutive runs more than five minutes apart each
pay the full fixed cost. Raising either trades idle GPU spend for latency; it is a cost
decision, not a correctness one.
Location: endpoint `176tpna3ogl94t`, `workersMin`, `idleTimeout`.

**B-04 · Sampling is linear in steps, and steps are 20.** **[V]**
Workflow node `9` (`BasicScheduler`) declares `steps: 20`, `scheduler: simple`,
`denoise: 1.0`. Sampling time is proportional to steps, so 20 → 12 removes ~40% of
A-01's sampling term — about 7 minutes at 243 frames. **This is the only lever here that
trades output quality**, so it needs an A/B against a fixed seed rather than a decision on
paper.
Location: committed `workflow.json`, node `9`.

**B-05 · The sampler's evaluations per step are unmeasured.** **[V] that it is unmeasured**
Node `17` selects `res_multistep`. Higher-order samplers commonly evaluate the model more
than once per step, so the effective cost may be a multiple of B-04's step count — but the
NFE for this sampler in the pinned ComfyUI 0.30.2 has not been checked, and no claim about
it is made here. If it is greater than one, a first-order sampler at equal step count is a
larger win than B-04 at equal quality risk. Worth measuring before changing.
Location: committed `workflow.json`, node `17`.

**B-06 · The text encoder is a 32B model loaded per cold start.** **[V]**
Node `13` loads `qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors`. It runs once per job, so it
contributes to the fixed term rather than the per-frame term, and it is part of what B-01
and B-02 address.
Location: committed `workflow.json`, node `13`.

**B-07 · The 45-minute execution cap leaves less headroom than it appears.** **[V]**
`executionTimeoutMs: 2700000`. At A-01's rates a cold run at the envelope maximum of 362
frames lands near 34 minutes, inside the cap but with under a third of margin. Anything
that lengthens sampling — more steps, a higher-NFE sampler, a larger canvas — narrows it
further.
Location: endpoint `176tpna3ogl94t`, `executionTimeoutMs`.

## Worker and endpoint state as measured

| | |
|---|---|
| GPU | H100 SXM, `gpuCount: 1` |
| Permitted types | RTX PRO 6000 Blackwell Server, H100 80GB HBM3, A100 80GB PCIe |
| Container disk | 120 GB |
| Scaler | `QUEUE_DELAY`, value 4 |
| Workers | `workersMin: 0`, `workersMax: 2` (temporarily raised from 1 by the operator during this pass) |

## The measurement that would close A-02

Resubmitting Run A's 73-frame request against a **warm** worker isolates the fixed term
directly: A-01 predicts ~5.4 minutes warm against 12 m 20 s cold. A result near 5.4 minutes
confirms the split and justifies B-01 and B-02 on measurement; a materially different one
falsifies A-01 and the per-frame rate has to be re-derived. It costs roughly a third of a
full run and is the cheapest way to convert this audit's central claim from inference into
evidence.

Instrumenting A-04 is the durable version of the same thing, and makes every future run a
data point rather than requiring a dedicated experiment.

## Provenance and maintenance

Authored 2026-08-15 against `84f24a2` on `feat/retention-reconciler`.

| Volatile fact | Re-verify with |
|---|---|
| Endpoint scaling, `flashboot`, `networkVolumeId`, execution cap | `curl -H "Authorization: Bearer $KEY" https://rest.runpod.io/v1/endpoints/176tpna3ogl94t` |
| Worker GPU model | RunPod GraphQL `myself { endpoints { pods { machine { gpuDisplayName } } } }` |
| Run timings | `curl -H "Authorization: Bearer $KEY" https://api.runpod.ai/v2/176tpna3ogl94t/status/<request-id>` |
| Workflow steps and sampler | `scripts/fetch_result.sh` then read `workflow.json` in the attempt package |
| Trained frame range | `src/tkr_cloud_video/jobs/contracts.py`, model-set entry `max_frames` and grid |
| Absence of phase timing | `grep -rn "elapsed\|duration_ms\|started_at" src/tkr_cloud_video/` |
