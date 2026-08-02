# YouTube upload

Set visibility to **Public**. Not Unlisted — the rules require the video to be
publicly visible.

---

## Title

```
IncidentLens — verifiable incident reconstruction on Backblaze B2 + Genblaze
```

*Alternative, if you prefer leading with the problem rather than the stack:*

```
IncidentLens — find the method that broke production, from the logs you already have
```

---

## Description

```
IncidentLens reads a Python service's source tree and its log files, reconstructs
a production failure down to the module it was logged in and a candidate function
inside it, and renders it as a narrated replay where every claim cites a log line.

Built for the Backblaze Generative Media Hackathon.

It labels every conclusion confirmed, inferred or unknown — and refuses to name a
cause it cannot evidence. Integration surface is a log file: no agents, no
instrumentation, no OpenTelemetry requirement.

── Try it ──────────────────────────────────────────
Live app        https://incidentlens.onrender.com
Incident library https://incidentlens.onrender.com/gallery
Source          https://github.com/Abhinav0905/Incidentlens

Paste your own log lines on the home page — no account and no API key needed.

── How the sponsor tech is used ───────────────────
Backblaze B2 is the library the application reads from, not somewhere it wrote
once. Each incident is a named bundle — replay, poster, analysis, briefing, call
graph and provenance manifests — with queryable object metadata and a JSONL
catalog the gallery loads at page load. The provenance manifests are held under
Object Lock. Because the bucket is public, the hosted service holds no B2
credentials at all.

Genblaze orchestrates the narration synthesis and records verifiable provenance:
14 text-to-speech steps per replay, each carrying its provider, model and a
SHA-256 of the audio produced, plus an ingest step recording the finished MP4.
The manifest is embedded inside the MP4 itself, so it can be extracted from a
downloaded file and checked.

Scope, stated plainly: this is a single-provider Genblaze pipeline (openai-tts),
not multi-provider orchestration. The narration script is written by gpt-4o
through the OpenAI SDK directly, because in genblaze-openai 0.3.3 — the version
this was built against — chat() is a module function rather than a provider.

── Models used ────────────────────────────────────
gpt-4o             writes the narration script     (OpenAI SDK, not via Genblaze)
gpt-4o-mini-tts    speaks each narration beat      (via Genblaze OpenAITTSProvider)
gpt-4o-mini        turns a visitor's description into telemetry
Full list: https://github.com/Abhinav0905/Incidentlens/blob/main/PROVIDERS.md

The incident reconstruction itself uses no model at all. Origin, propagation,
blast radius, confidence and every evidence citation are computed by a
deterministic engine. The language model writes prose about the analysis; it
never produces the analysis.

── Notes ──────────────────────────────────────────
The demo scenarios are synthetic fixtures, labelled as such in the app. A
candidate function is a static inference from an AST call graph, not a runtime
stack frame, and the interface says so on screen.

Three defects found while building this were reported upstream with
reproductions, and all three were fixed by Backblaze within 24 hours:

#223 estimate_cost() always returned None    -> PR #230, genblaze-openai 0.3.4
#224 Pipeline.step() accepted a non-provider -> PR #231, genblaze-core 0.3.8
#225 Mp4Handler.extract() mislabelled a type -> PR #231, genblaze-core 0.3.8

https://github.com/backblaze-labs/genblaze/issues/223
https://github.com/backblaze-labs/genblaze/issues/224
https://github.com/backblaze-labs/genblaze/issues/225
```

---

## Chapters

Paste these into the description **as the last block**, with `0:00` first —
YouTube then renders a chapter strip on the scrubber. This matters more than
usual here: a judge who only has three minutes can jump straight to the segment
they are scoring.

Replace the times with your actual ones (watch the video once and note them):

```
0:00 The problem: a wall of logs at 2am
0:14 What IncidentLens does
0:32 Reconstructing from raw log files
1:02 The replay — a failure no dashboard catches
1:38 Genblaze provenance, verified from inside the MP4
2:06 Backblaze B2 as the incident library
2:30 When the telemetry cannot settle it
```

---

## Settings

- **Visibility:** Public
- **Audience:** "No, it's not made for kids"
- **Category:** Science & Technology
- **Tags:** `incident response`, `SRE`, `observability`, `Backblaze B2`,
  `Genblaze`, `provenance`, `static analysis`, `Python`
- **Language:** English

Once it is live, open the link in a private window to confirm it plays without
a sign-in, then paste the URL into Devpost.
