# IncidentLens — Genblaze + B2 submission runbook

The Devpost video must be **less than three minutes**, and judges are *not required
to watch beyond three minutes*. So target **2:45**, leaving 15 seconds of margin.

**The rule that decides your edit: nothing load-bearing after 2:30.** If a judge stops
at the line, they must already have seen the manifest verify, the B2 object, and the
closing claim. The replay excerpt is your only flexible segment — trim it, never the proof.

## One-line pitch

IncidentLens reads a Python service's source and its logs, reconstructs a production
failure down to the method that raised, and renders it as a narrated replay whose every
claim cites a log line — orchestrated through Genblaze and served from Backblaze B2 with
provenance you can verify.

## What to demo

The **hary-platform** series: one AI-assistant architecture, four failures, each landing
on a different part of the same 16-module code graph.

| Scenario | Lands on | Why it is in the demo |
| --- | --- | --- |
| `agentic-retry-exhaustion` | `hary.graph.nodes.agent.AgentNode.__call__` | **The hero.** Nothing 5xxs, dashboards stay green, quality silently rots |
| `gateway-auth-rejection` | `hary.models.llm_factory.get_llm_for_tier` | Classic hard outage, deep in the stack |
| `pii-guardrail-crash` | `hary.guardrails.pii._get_presidio` | Partial failure — only some traffic breaks |
| `demo/model-id-typo` | same platform, **from raw log files** | Proves the log path, and analyses in seconds on camera |

Lead with the silent one. Every competitor can show a service turning red; showing a
failure that *no dashboard catches* is the argument for why the tool should exist.

## The 2:45 storyboard

| Time | On screen | What you say |
| --- | --- | --- |
| 0:00–0:12 | A wall of raw log lines scrolling | "At 2am this is what you get. Somewhere in here a service is failing, and nothing tells you which function." |
| 0:12–0:30 | The hosted URL. The incident gallery, thumbnails streaming from B2 | "IncidentLens keeps every reconstructed incident as a media bundle in Backblaze B2 — video, analysis, provenance." |
| 0:30–1:00 | `incidentlens analyze --config demo/model-id-typo/incidentlens.config.json` running on **raw log files**. The briefing appears in seconds | "No agents, no instrumentation. It reads log files and the source tree. Seconds later: the origin service, the propagation chain, and the failing module." |
| 1:00–1:35 | The `agentic-retry-exhaustion` excerpt. Architecture → module → `AgentNode.__call__` in red | "This one returns HTTP 200. Success rate holds at 99.6%. But salvaged partial answers went from 0.3% to 44.8% — and it lands on the exact method." |
| 1:35–2:05 | The Genblaze manifest: 14 `openai-tts` steps, `gpt-4o-mini-tts`, SHA-256. Then extract the manifest **from inside the MP4** and verify | "Genblaze orchestrates every narration beat and records provider, model and hash. The manifest is embedded in the video — download it anywhere and it still verifies against the analysis that produced it." |
| 2:05–2:30 | The B2 bucket: hierarchical keys, the public URL opening in a signed-out window. A delete attempt returning **403** under Object Lock | "B2 is the library the product reads from, not a dead drop. And the manifest is immutable — six months from now you can still prove the replay was not edited." |
| 2:30–2:45 | The `unknown — confidence 0.00` row, and the missing-evidence list | "And when the telemetry cannot settle a question, it says so. That is the difference between a tool you trust at 2am and one you don't." |

## Rules for the edit

- **Under 3:00.** Target 2:45. If you run long, cut the replay excerpt from 35s to 25s —
  never the manifest or B2 segments.
- **No third-party trademarks or copyrighted music.** The fixtures are already scrubbed;
  keep vendor logos and branded UI out of frame, and use silent or licence-free audio
  under any montage.
- **Hosted on YouTube, Vimeo or Youku**, publicly visible, English.
- **Show the app working**, not just artefacts. At least one segment must be the live
  hosted URL, not a local terminal.
- Do not play a full replay. They run 2:46–3:15 each — longer than the entire budget.
  Judges can watch them in full on the B2 gallery.

## Commands used in the demo

```bash
# Live reconstruction from raw log files (fast — safe to run on camera)
incidentlens analyze --config demo/model-id-typo/incidentlens.config.json --narration template

# Full render + Genblaze narration + publish to B2 (slow — pre-render this)
incidentlens studio agentic-retry-exhaustion \
  --out demo/gallery/agentic-retry-exhaustion/replay.mp4 \
  --voice genblaze --narration llm \
  --narration-provider openai --narration-model gpt-4o \
  --profile high --upload-genblaze-b2

# Verify the manifest embedded inside the MP4
python -c "from genblaze_core.media import Mp4Handler; \
m = Mp4Handler().extract('demo/gallery/agentic-retry-exhaustion/replay.mp4'); \
print(m.verify(), m.canonical_hash)"
```

## Submission checklist

- [ ] Public GitHub repo with working setup instructions in the README
- [ ] Hosted application URL a judge can click (warm, not cold-starting)
- [ ] Demo video **< 3:00**, public on YouTube/Vimeo/Youku
- [ ] `PROVIDERS.md` pasted into the Devpost description
- [ ] Written explanation of how B2 and Genblaze are each used
- [ ] Feedback filed as Genblaze GitHub issues (optional, separate prize, 10 winners)
