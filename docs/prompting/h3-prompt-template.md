# MiniMax H3 prompt template

Fill-in templates for the structured prompt this platform accepts, plus the rules
and closed vocabularies each slot draws from.

- [`templates/base-mode.json`](templates/base-mode.json) — T2VA, I2VA, FL2VA, L2VA
- [`templates/reference-mode.json`](templates/reference-mode.json) — Ref2VA

Copy a template, replace every `<ANGLE_BRACKET>` slot, delete every `$comment*`
key, and submit the result as the `structured_prompt` field of a generation
request. The service composes, validates, renders, and digests it; you never
assemble wire text yourself.

```json
{
  "mode": "text-to-video",
  "workflow_id": "h3-t2v-1",
  "model_set_id": "h3-models-1",
  "seed": 42,
  "structured_prompt": { "…the filled template…" }
}
```

Supply **exactly one** of `prompt` (freeform string) or `structured_prompt`.
Sending both is refused rather than resolved by precedence.

## Which mode

| Mode | You supply | The prompt describes |
|---|---|---|
| `T2VA` | text only | the whole audiovisual timeline |
| `I2VA` | a first frame | the path forward from that frame |
| `FL2VA` | first and last frames | the continuous path between them |
| `L2VA` | a last frame | a plausible opening converging on it |
| `Ref2VA` | subjects, pictures, videos, audio | reuse relationships across the clip |

The keyframe alignment header is derived for you from the mode and duration —
there is no slot for it. `T2VA` and `Ref2VA` get none.

## The one rule that matters most

**Two kinds of string live in this document, and they are not interchangeable.**

| | Fields | Rule |
|---|---|---|
| **Caller text** | `dialogue[].text`, `on_screen_text[]` | Reproduced byte for byte, never translated. May **not** contain `<d>`, `</d>`, `<scenetrans>`, `<cutoff>`, a reference label, `[Shot N]`, a section name followed by `:`, or any control character — newlines included. A span that does is refused and no prompt is produced. |
| **Service prose** | `description`, `identity`, `definition`, `detail`, `summary.text`, the two sound sections | Written by you as the prompt author. May carry reference labels. Must not carry dialogue delimiters. |

Refusal, not escaping: escaping would alter words the grammar requires verbatim,
so a span that would act as structure is rejected instead of rewritten.

## Slot vocabularies

Every value below is a closed set. Anything outside it is rejected before GPU work.

### Visual style — `shots[0].style`, opening shot only

`Cinematic` · `live-action` · `2D-animated` · `3D CG` · `claymation` ·
`watercolor` · `vintage film`

Declared once on shot 1; restating it on a later shot is a defect.

### Camera motion — `camera.motion`

| Label | Renders as | Label | Renders as |
|---|---|---|---|
| `Zoom In` | zooms in | `Pedestal Up` | pedestals up |
| `Zoom Out` | zooms out | `Pedestal Down` | pedestals down |
| `Push In` | pushes in | `Arc Shot` | arcs around the subject |
| `Pull Out` | pulls out | `Tracking Shot` | tracks the subject |
| `Pan Left` | pans left | `Static Shot` | holds a static shot |
| `Pan Right` | pans right | `Shake Slightly` | shakes slightly |
| `Truck Left` | trucks left | `Shake Strongly` | shakes strongly |
| `Truck Right` | trucks right | `POV` | shows the subject's point of view |
| `Tilt Up` | tilts up | `Roll Clockwise` | rolls clockwise |
| `Tilt Down` | tilts down | `Roll Counterclockwise` | rolls counterclockwise |

### Camera modifiers — `camera.amplitude`, `camera.speed`

| Field | Values | Default |
|---|---|---|
| `amplitude` | `with small amplitude`, `with large amplitude` | medium — **pass `null`** |
| `speed` | `at slow speed`, `at fast speed` | normal — **pass `null`** |

Medium and normal have no wire form. Passing the literal string `"medium"` is a
defect, because the grammar expresses that value as absence.

### Cut phrase — `cut_phrase`, later shots

`the camera cuts to` · `the shot cuts to` · `the shot transitions to` ·
`the shot changes to` · `the shot switches to`

`cross-dissolve`, `fade`, and `wipe` exist as requested transitions and are used
only when the caller asks for one.

### Task types — `summary.task_types`, Ref2VA

`keyframe completion` · `reference generation` · `video editing` ·
`video continuation` · `audio reuse` · `audio reference`

Combine when several relationships genuinely apply; no repeats. Presence of a
video or audio asset does not by itself create a type — a reference video that
only supplies camera rhythm is `reference generation`.

### Retention markers — `retention_analysis[].marker`, Ref2VA

| Label kind | Markers |
|---|---|
| `<Subject N>`, `<Picture N>`, `<Video N>` | `fully_preserved`, `partially_preserved`, `attribute_transfer`, `weak_reference` |
| `<Audio N>` | `fully_copy`, `partially_copy`, `reference`, `weak_reference` |

### Absent values

`N/A` — the only way to say "no soundscape" or "no score". Omitting the section
is a defect; the token is how absence is stated.

`[unclear]` — for an unintelligible span of reference audio, instead of guessing.

## Shot timeline rules

- Shot numbers start at `1` and increase by one.
- Shot 1 carries **no** `cut_at_seconds`. Every later shot **requires** one.
- Cut times strictly increase and are strictly **less than** `duration_seconds`.
- A cut introduces new information about subject, space, state, viewpoint, or
  time. If only distance or a slight angle changes, use camera motion instead.
- Cut times render as `MM:SS.mmm`; you supply plain seconds.

## Speaker rules

- `speaker_id` is `S1`, `S2`, … assigned in order of **first vocalization**, with
  no gaps. Starting at `S2` is a defect.
- Compound ids (`S1,S2`) are for already-numbered speakers vocalizing together.
- A speaker keeps their id across shots. Characters who never vocalize get none.
- Speaker ids never appear in `retention_analysis`.
- `voiceover: true` renders the fixed off-screen phrase and states that the
  on-screen character's lips stay closed.

## Reference label rules — Ref2VA

- Labels are `<Subject N>`, `<Picture N>`, `<Video N>`, `<Audio N>`.
- `subject_definitions` is the **only** section that may introduce a label.
- Every defined label must be used; every used label must be defined. Both
  directions are checked, so a dangling label and an unused definition are each
  reported.
- `<Subject N>` is reusable visible content. `<Picture N>` standalone only when
  the image is itself a frame or composition anchor — if it merely defines a
  character, cite it inside that subject's definition. `<Video N>` is for
  whole-video relationships. `<Audio N>` is an audio signal.
- Video and audio labels are numbered independently; `<Video 1>` and `<Audio 2>`
  may come from one file.

## What the service tells you back

A rejection names the **field path and the rule**, never the offending text —
read your own payload for that. A `PromptValidationError` carries every defect at
once, so one round trip is enough to fix them all.

An acceptance reports which checks ran **and which were skipped** for your mode,
with the reason. A base-mode pass names the five full-reference checks it did not
exercise, so a green result never implies coverage it did not have.

## Worked example

A filled base-mode template that composes, validates, and renders. This exact
payload is exercised by `tests/prompt_authoring/test_templates.py`, so it cannot
drift from the code.

```json
{
  "mode": "T2VA",
  "duration_seconds": 8.0,
  "shots": [
    {
      "number": 1,
      "style": "Cinematic",
      "description": "A medium-wide shot frames a baker opening the shutters of a small street bakery before sunrise.",
      "camera": { "motion": "Push In", "amplitude": "with small amplitude", "speed": "at slow speed" },
      "dialogue": [
        {
          "speaker_id": "S1",
          "identity": "The middle-aged baker with a calm, slightly raspy voice",
          "language": "English",
          "text": "First batch of the morning."
        }
      ]
    },
    {
      "number": 2,
      "cut_at_seconds": 5.0,
      "cut_phrase": "the camera cuts to",
      "description": "a close-up of steam rising from the sliced bread on the wooden counter"
    }
  ],
  "overall_soundscape": "Wooden shutters scrape open over a quiet street as trays clink softly inside the bakery. The doorbell rings once, followed by light footsteps and the crisp sound of bread being sliced.",
  "non_diegetic_music": "A soft acoustic-guitar pattern at a moderate tempo, joined by sparse upright-bass notes and a gentle fade at the end."
}
```

Rendering that produces:

```text
integrated_multimodal_description: [Shot 1] Cinematic, A medium-wide shot frames a baker opening the shutters of a small street bakery before sunrise. The camera pushes in with small amplitude at slow speed. The middle-aged baker with a calm, slightly raspy voice (S1) says: <d>[English] First batch of the morning.</d> [Shot 2] At 00:05.000, the camera cuts to a close-up of steam rising from the sliced bread on the wooden counter

overall_soundscape: Wooden shutters scrape open over a quiet street as trays clink softly inside the bakery. The doorbell rings once, followed by light footsteps and the crisp sound of bread being sliced.

non_diegetic_music: A soft acoustic-guitar pattern at a moderate tempo, joined by sparse upright-bass notes and a gentle fade at the end.
```

## Provenance and maintenance

Authored 2026-08-14 against grammar revision `h3-2026-08`.

Every vocabulary in this document is extracted from
`src/tkr_cloud_video/prompting/grammar.py`, which is digest-pinned: editing a
table without restating `GRAMMAR_DIGEST` fails at import, and the loaded revision
is reported by `tkr-cloud-video doctor --json` under `prompt_grammar`.

| Volatile fact | Re-verify with |
|---|---|
| Grammar revision and digest | `uv run python -m tkr_cloud_video doctor --json` |
| Every vocabulary in this file | `uv run pytest tests/prompt_authoring/test_templates.py` |
| Template files parse and validate | `uv run pytest tests/prompt_authoring/test_templates.py` |
| Structured prompt field shape | [`schemas/h3-prompt.schema.json`](../../schemas/h3-prompt.schema.json) |

The upstream grammar of record is pinned in the plan at
`prompt-authoring.source_documents`. Its guide text is deliberately not vendored
here: it ships under the MiniMax H3 Community License, which
[ADR-001](../adr/001-read-the-license-exclusion-of-the-european-union-as-union-membership.md)
reads narrowly for territory.
