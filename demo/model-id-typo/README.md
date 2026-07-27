# Demo · a one-character model-id typo takes Hary down

A real, **dynamic** incident — nothing about it is hard-coded. Someone set

```
FAST_TIER_MODEL_ID=us.modelhst.fast-tier-4-5-20251001-v1:0
                          ^^ "modelhst" — the "h" is missing
```

Every fast-tier LLM call now fails with a ModelProvider `ValidationException` (invalid
model identifier), the circuit breaker opens, the BFF times out, and the
assistant panel goes dark. IncidentLens reads the raw logs, reconstructs the
incident, traces it to the failing **function**, and narrates it over a replay.

This is the `analyze`/`watch` path, not a bundled scenario: the engine reasons
over the log files live. Point the config at your service's real logs instead of
these and it behaves identically.

## Run it

```bash
cd incidentlens && pip install -e ".[studio,graph]"    # ffmpeg needed for the video
incidentlens analyze --config demo/model-id-typo/incidentlens.config.json
# or watch continuously (renders when the error burst trips):
incidentlens watch   --config demo/model-id-typo/incidentlens.config.json

# name the model that narrates the failure:
incidentlens analyze --config demo/model-id-typo/incidentlens.config.json \
  --narration llm --narration-model claude-opus-4-8        # or gpt-5.1 (+ --narration-provider openai)
```

## What it reconstructs (verified, no video needed)

```
origin service : hary-ai
failing stage  : llm-client
failing module : hary.models.llm_factory
failing SYMBOL : hary.models.llm_factory.get_llm_for_tier   (called by _resolve_llm)
blast radius   : 6 modules
propagation    : hary-ai → hary-bff → hary-frontend
root cause     : change on hary-ai, ~90% (config change 3 min before first failure)
```

## What's in `out/` (generated without ffmpeg, so you can inspect the depth)

- `*.briefing.md` — the on-call briefing
- `*.analysis.json` — the full evidence-backed analysis
- `*.code-graph.html` — the interactive graph; toggle **symbols**, and the failing
  function `get_llm_for_tier` burns red
- `*.code-graph.mmd` — the same as a Mermaid diagram (failing function flagged)

The `.mp4` is the only thing that needs ffmpeg — run the command above locally to
get it. The narration names the failing function, its caller, and its blast radius.

## Files

- `logs/hary-ai.log` — Python-`logging` format; logger names map to pipeline stages
- `logs/hary-bff.log` — Spring/Java format (the BFF timeout)
- `logs/hary-frontend.log` — ISO-prefixed (the 502 to users)
- `incidentlens.arch.json` — the service graph + hary-ai's internal pipeline
- `incidentlens.codegraph.json` — the code graph, **with the function-level symbol layer**
