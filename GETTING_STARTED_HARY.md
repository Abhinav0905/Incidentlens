# IncidentLens × Hary — reproduce the Gateway 401 and get the movie

This folder is a self-contained copy of IncidentLens 0.6.0. Nothing in the
Hary services was modified; the only integration surface is log files.

New in 0.6.0 — **the code as a network.** Double-click
`demo/hary-ai-code-graph.html`: that is the real hary_ai codebase (135
modules, 285 edges) as an interactive map, extracted with pure static
analysis. Click `hary.models.llm_factory` and the panel lists exactly who
calls it and what it uses; search "pii" or "guardrail" to find who depends on
those. `demo/demo-code-graph.html` is the same view for the demo incident,
with the traversed path ringed teal and the failing module burning red.
Regenerate anytime with `incidentlens graph .` from the Hary root — and when
you run `discover` + `watch`/`analyze`, every incident now gets its own
`INC-….code-graph.html` next to the video, plus a code annotation card in the
movie itself naming the failing file and its callers.

New in 0.5.0 — **the dive**: the video no longer stops at "hary-ai failed".
At the failure beat the camera goes inside hary-ai and walks the request
through its internal pipeline — rate-limit, chat endpoint, PII scan,
guardrail, context, query rewrite — each stage flashing healthy (with the log
lines that prove it), until the shared LLM client throws the 401 and erupts.
Branches the request never took stay dimmed. `incidentlens discover` extracts
these internals from any Python codebase automatically (it reads hary_ai's
LangGraph wiring in `hary/graph/builder.py` without executing anything), and
you can hand-edit them in `incidentlens.arch.json` for non-Python services.
Watch `demo/incidentlens-gateway-401-dive-demo.mp4`.

## 0. Install (one time, ~2 minutes)

```bash
cd incidentlens
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[studio]"
brew install ffmpeg             # espeak-ng is only needed for the robotic fallback
```

Python 3.10+ works.

## 1. Sanity check — your incident is already bundled

The exact failure from the ticket (hary-ai → Gateway, both auth schemes
rejected with 401, assistant panel down) ships as a scenario. Render it:

```bash
incidentlens studio gateway-auth-rejection --out demo.mp4
```

A pre-rendered copy is in `demo/incidentlens-gateway-401-demo.mp4` (silent
audio track — the sandbox this was built in has no TTS; the command above
gives you the narrated version).

## 2. Live repro against the real hary_ai

From the Hary repo root (the parent of this folder):

```bash
# 1) derive the service graph from the codebase, then look it over
incidentlens/.venv/bin/incidentlens discover .
#    -> incidentlens.arch.json      (editable service graph proposal)
#    -> incidentlens.config.json    (log paths + watch settings — edit these)

# 2) run hary_ai with its stdout captured to a file
mkdir -p logs
cd hary_ai/hary_ai_microservice
python main.py 2>&1 | tee ../../logs/hary-ai.log
```

Point the `hary-ai` entry in `incidentlens.config.json` at `logs/hary-ai.log`,
then in a second terminal:

```bash
incidentlens/.venv/bin/incidentlens watch --config incidentlens.config.json
```

Now reproduce the 401 (send a chat request through hary-ai with the virtual
key configured). Three ERROR lines within 90 seconds trip the watcher; it
waits for the burst to settle, reconstructs the incident, and writes into
`incidentlens-videos/`:

- `INC-....mp4` — the narrated 3D replay
- `INC-....briefing.md` — the on-call briefing
- `INC-....analysis.json` — the full evidence record

## 3. Or after the fact

Already have the log saved? No watcher needed:

```bash
incidentlens analyze --logs hary-ai=logs/hary-ai.log --arch incidentlens.arch.json
```

## Options worth knowing

- `--profile preview` renders in ~1/5 the time while you iterate; `high`
  (default) is 1080p30; `ultra` is 1440p.
- `--voice openai` + `OPENAI_API_KEY` for a calm neural voice. `auto` chooses
  OpenAI when the key is present, then falls back locally. Use
  `--intro-video PATH` for an optional short Sora/Veo/Runway bumper; the
  technical replay remains deterministic.
- `incidentlens serve` still runs the browser UI with the interactive replay.

## What the video will say about the 401

The engine reconstructs from evidence only. For this incident that means:
origin `hary-ai`; the credential config change minutes earlier as the likely
cause (~0.90, inferred — not asserted); propagation to `hary-bff` and the
frontend; and — because Gateway itself sends no telemetry — an explicit gap:
*"No telemetry received from: llm-gateway"*, with the first recommended
action being to compare the configured credential (and its auth scheme)
against what the gateway expects. Which is precisely the question you asked
IT in the ticket.

Housekeeping: a stray `pytest-cache-files-*` folder may exist here from the
build sandbox — safe to delete. Docs: `README.md`, `docs/LIVE.md`,
`docs/STUDIO.md`, `CHANGELOG.md`.
