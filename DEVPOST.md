# Devpost submission text

Copy-paste ready. Every claim here was checked against the running system; where
something is a limitation it is stated rather than omitted, because the judges
wrote the SDK and will open the JSON.

**Before publishing, confirm the two bracketed items at the bottom.**

---

## Tagline

**Provenance for the reasoning, not just the pixels — verifiable incident replays
for on-call engineers, with Backblaze B2 as the incident library.**

---

## What it does

At 2am a responder gets a wall of log lines and a service map. Neither says which
part of the code failed, what it took down, or which questions the telemetry
cannot answer.

IncidentLens reads a Python service's **source tree and its log files**, then
reconstructs the failure: the origin service, the propagation chain, the blast
radius, the customer impact, and — using a static call graph — the module the
failure was logged in plus a **candidate function** inside it. It renders that as
a narrated 1080p replay that zooms from the service map down to the candidate
method, and writes an engineer briefing alongside.

The discipline is the product. Every conclusion carries a status and a confidence:

- `confirmed` — direct telemetry shows it happened
- `inferred` — the evidence points this way, a human should verify
- `unknown` — a real possibility the telemetry cannot settle

The analysis also lists what it **could not see**. The engine raises rather than
emit a citation it cannot resolve, and a candidate function is labelled on screen
as `STATIC CANDIDATE · NO STACK FRAME`. It is never presented as a runtime frame.

Integration surface is a log file. No agents, no instrumentation, no OpenTelemetry
requirement.

**Try it:** paste your own log lines on the home page — no account, no key, no
rate limit. Or describe a failure in a sentence and let a model write the
telemetry for the engine to reconstruct.

---

## How Backblaze B2 is used

B2 is the library the application **reads from**, not a dead drop it once wrote to.

**Store.** Six 1080p narrated replays plus, for each incident, a poster frame, the
full analysis document, an engineer briefing, a Mermaid call graph, and two
Genblaze provenance manifests.

**Organise.** Two layers, deliberately:

```
Hary_Part1-Gateway-Auth-Rejection/     ← human-readable bundle, 7 objects
Hary_Part2-PII-Guardrail-Crash/
Hary_Part3-Agentic-Retry-Exhaustion/
Other-Architectures/…
incidents.jsonl                        ← the catalog the app reads
incidentlens/runs/…/manifest.json      ← Genblaze's content-addressed originals
```

Every object also carries queryable metadata — `incident-id`, `origin-service`,
`failing-module`, `failing-symbol`, `leading-confidence` — so the gallery builds
a listing from `head()` calls without downloading bodies. Lifecycle defaults are
applied.

**Serve.** `GET /api/v1/incidents` reads `incidents.jsonl` back out of B2;
`/gallery` streams video and posters **directly from the bucket** to the browser.
Because the bucket is public, the hosted service holds **no B2 credentials at
all** — reads are plain HTTPS over the standard library, no boto3 on the deployed
image.

**Object Lock.** The Genblaze manifests are written under GOVERNANCE retention, so
the provenance record cannot be quietly rewritten. Media is deliberately left
unlocked — a replay can be re-rendered, but the record of what produced it cannot
be silently replaced. Verified read-only: `head_object` reports the mode and
retain-until date, and a delete of that version returns HTTP 403.

---

## How Genblaze is used

Genblaze orchestrates the **speech synthesis** and records verifiable provenance.

Each replay produces two manifests:

- `narration.genblaze.json` — **14–17 `generate` steps**, provider `openai-tts`,
  model `gpt-4o-mini-tts`, modality `audio`, `prompt_visibility=private`, each
  step carrying a SHA-256 of the audio it produced.
- `provenance.genblaze.json` — a single `StepType.INGEST` step with
  `provider: null`, recording the finished MP4 (SHA-256, dimensions, duration,
  codec, tracks) plus a digest of the analysis document that produced it.

That `ingest` step is a deliberate choice: the video is **not** model-generated, so
it is recorded as provenance-tracked media rather than claimed as a generation.
Publishing goes through `ObjectStorageSink` with `KeyStrategy.HIERARCHICAL` and
`manifest_lock`, and the manifest is **embedded inside the MP4** — extract it from
a downloaded file and it still checks out.

**Scope, stated plainly.** This is a single-provider, single-modality Genblaze
pipeline: `openai-tts` for audio, plus the non-generative ingest step. It is not
multi-provider orchestration. The narration *script* is written by `gpt-4o`
through the OpenAI SDK **directly, not through Genblaze**, because in
`genblaze-openai` 0.3.3 — the version this was built and tested against —
`chat()` is a module function rather than a `BaseProvider`, so a text step could
not participate in a Pipeline.

---

## Upstream contributions

Three defects found while building this were reported with reproductions, and
**all three were fixed by Backblaze within 24 hours**:

| Issue | What it was | Fixed by |
| --- | --- | --- |
| [#223](https://github.com/backblaze-labs/genblaze/issues/223) | Every `ModelSpec` in `genblaze-openai` left `pricing=None`, so `estimate_cost()` could only ever return `None` for all 11 models | [PR #230](https://github.com/backblaze-labs/genblaze/pull/230) → `genblaze-openai` 0.3.4 |
| [#224](https://github.com/backblaze-labs/genblaze/issues/224) | `Pipeline.step()` silently accepted a non-provider and died later with `AttributeError`; no `BaseProvider` existed for `Modality.TEXT` | [PR #231](https://github.com/backblaze-labs/genblaze/pull/231) → `genblaze-core` 0.3.8 |
| [#225](https://github.com/backblaze-labs/genblaze/issues/225) | `Mp4Handler.extract()` reported a caller type error as `EmbeddingError`, i.e. as media corruption | [PR #231](https://github.com/backblaze-labs/genblaze/pull/231) → `genblaze-core` 0.3.8 |

Each was reproduced against the then-current published packages before filing,
with the root cause identified rather than just the symptom. This submission
remains on the versions it was tested against; the fixes are in the releases
above.

---

## AI providers and models

| Provider | Model | Role | Via Genblaze? |
| --- | --- | --- | --- |
| OpenAI | `gpt-4o` | Writes the narration script | **No** — direct OpenAI SDK |
| OpenAI | `gpt-4o-mini-tts` | Speaks each narration beat | **Yes** — `OpenAITTSProvider` |
| OpenAI | `gpt-4o-mini` | Sandbox: turns a visitor's description into telemetry | No |
| OpenAI | `sora-2-pro` | A 4-second decorative title clip | No — predates the integration; no Genblaze manifest |

Selectable but unused in the demo: Anthropic `claude-sonnet-5` / `claude-opus-4-8`
for narration, ElevenLabs, Piper, espeak-ng for voice. Full detail in
[`PROVIDERS.md`](PROVIDERS.md).

**What uses no model at all:** the incident reconstruction (origin, propagation,
blast radius, confidence, evidence citations), the static AST call graph, and the
3-D renderer. Identical input produces byte-identical pixels. The language model
writes prose *about* the analysis; it never produces the analysis.

---

## Honest limitations

- **The scenarios are synthetic.** Seven hand-built fixtures, labelled
  "Synthetic" in every description. They exercise real code paths; they are not
  real outages.
- **A candidate function is a static inference**, not a runtime stack frame, and
  the UI says so on screen. Where a module has no indexed symbols the tool
  degrades to module level rather than guessing.
- **Manifest verification checks the manifest.** `verify()` confirms the
  manifest's own integrity. The analysis digest is *recorded* in the manifest;
  comparing it against a given analysis document is a separate step.
- **The live log watcher is beta** — offsets are in memory, so a restart can
  re-fire a recent incident.
- **Rendering is offline.** A replay takes minutes of CPU, so the hosted service
  does not render; it serves pre-rendered media from B2 and reconstructs analyses
  live.

---

## Built during the hackathon

The reconstruction engine and renderer predate the submission period. Built
**during** it, specifically for B2 and Genblaze:

- The entire Genblaze integration: narration orchestration, both manifest kinds,
  in-MP4 embedding, and `manifest_lock`.
- The B2 incident library: bundle layout, object metadata, the `incidents.jsonl`
  catalog, lifecycle defaults, Object Lock, and the read-back API and gallery.
- The visitor sandbox (paste-your-own-logs and describe-an-incident).
- The one-platform, four-failure scenario series.
- Three defects reported upstream with reproductions — **all three fixed by
  Backblaze within 24 hours**, shipping in `genblaze-core` 0.3.8 and
  `genblaze-openai` 0.3.4. See "Upstream contributions" above.

---

## Links

- **Live app:** https://incidentlens.onrender.com
- **Incident library:** https://incidentlens.onrender.com/gallery
- **Repository:** https://github.com/Abhinav0905/Incidentlens
- **Catalog, straight from B2:**
  https://s3.us-east-005.backblazeb2.com/Hackproject/incidents.jsonl

---

## Confirm before publishing

1. **Retention period.** The "Object Lock" paragraph says the manifests are held
   under GOVERNANCE retention. Set a period that outlasts judging (11 August)
   before you publish this, or the claim will not reproduce.
2. **Prior-work dates.** Adjust "Built during the hackathon" to match your actual
   commit history if the boundary differs.
