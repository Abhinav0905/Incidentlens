# Changelog

## Unreleased

### Added

- OpenAI neural narration via `gpt-4o-mini-tts`, with a calm professional
  delivery prompt, configurable voice, speech-specific base URL, automatic
  provider selection, and explicit AI-voice disclosure in rendered films.
- Optional Genblaze 0.3.x integration for Python 3.11+: `--voice genblaze`
  batches every narration beat into one OpenAI TTS pipeline, marks prompts
  private, and binds each generated WAV to a SHA-256-backed manifest. The
  supported calm default is `coral`; direct `--voice openai` remains available
  for `marin`, `cedar`, and compatible custom speech endpoints.
- `--publish-genblaze` writes a hash-verified sidecar manifest without changing
  the rendered MP4. `--upload-genblaze-b2` persists the original MP4 and its
  canonical manifest through Genblaze's `ObjectStorageSink`, then embeds that
  manifest into the local copy. The legacy direct `--upload-b2` path remains
  available.
- Genblaze B2 configuration supports `B2_BUCKET`, `B2_REGION`, `B2_KEY_ID`, and
  `B2_APP_KEY`, plus the existing `B2_ENDPOINT_URL` and
  `B2_APPLICATION_KEY` aliases.
- `--intro-video PATH` for prepending a short Sora/Veo/Runway or
  motion-graphics bumper while keeping the technical replay deterministic.
- Consistent narration loudness and a more legible, branded cinematic HUD.

### Notes

- The narration manifest records verified hashes for beat-level WAVs, but its
  `file://` asset URLs point into the renderer's temporary working directory.
  Those files are removed after rendering, so this manifest is not
  fetch-verifiable later unless the audio assets are persisted separately.
- Genblaze's `private` prompt visibility is a provenance classification, not
  encryption or redaction. Exported narration manifests contain the spoken
  script and should be handled as sensitive incident artifacts.

### Fixed

- Explicit gateway `model_blocked` / virtual-key policy failures now produce a
  specific evidence-backed hypothesis and read-only model allow-list check.
- Pre-incident startup messages containing “warmed” no longer create a false
  recovery before the failure.
- Spoken narration no longer reads raw error dictionaries or long model IDs
  for model-policy and invalid-model failures.
- Symbol-level Mermaid diagrams declare every edge endpoint, including class
  constructors when class methods are visible.

## 0.6.0 - 2026-07-17

### Added

- **Deep code graph** (`connectors/code_graph.py`): the third level of depth. A pure-`ast` scan maps every internal module of a service, its imports, and — resolved through import aliases — which modules it actually calls and which symbols it calls on them (including attribute reads like `settings.gateway_base_url`). Answers "who calls the PII scanner?" and "what breaks if the LLM client breaks?" from static analysis alone; nothing is imported or executed. `discover` writes it to `incidentlens.codegraph.json`.
- **Interactive network explorer** (`incidentlens graph`, `studio/graphview.py`): one self-contained dark-theme HTML file — force-directed canvas network, zoom/pan, drag, search, per-service switcher, and a details panel listing *called by* / *calls* with symbols for any clicked module. With `--analysis`, the incident overlays onto the network: traversed modules ring teal, the failing module burns red. No CDN, no network access; attach it to the incident channel like the video.
- **Code context in the story**: the trace now carries the failing *module* (not just the stage) plus its callers and callees; the point-of-failure narration names them ("the code is llm factory, called by query rewrite, agent and small talk — one bad credential breaks every caller"); and the dive scene pins an annotation card to the failing slab: `hary/models/llm_factory.py`, `← called by …`, `→ uses …`.
- `analyze` and `watch` automatically load `incidentlens.codegraph.json` (written by `discover`), enrich the trace, and emit `<incident>.code-graph.html` with the incident overlay next to the video and briefing.
- Bundled `gateway-auth-rejection` scenario ships a code graph, so the demo shows all three levels: system → pipeline → code.

## 0.5.0 - 2026-07-17

### Added

- **The dive: internal nuts-and-bolts tracing.** When the origin service's internal pipeline is known, the movie no longer stops at the service boundary. At the failure beat the camera dives inside the origin service: its middleware, endpoints, graph nodes and shared clients appear as their own 3D scene, a pulse walks the request stage by stage — each traversed stage flashing teal with a `passed · logged` or `traversed · inferred` tag — and at the failing stage the pulse turns red and erupts, with everything downstream marked never-ran and off-path branches dormant. Then the camera surfaces back to the macro architecture for the propagation story.
- **Internal pipeline scanner** (`incidentlens discover`, static AST — nothing is imported or executed): LangGraph `add_edge`/`add_conditional_edges` string wiring becomes the stage graph; FastAPI/Flask route decorators become the entry stage; `add_middleware` chains run before it in execution order; modules with client-ish names imported by multiple stages become fan-in stages (e.g. a shared `llm_factory` becomes `llm-client`). Each stage carries the dotted module prefixes its log lines are written under. Written into `incidentlens.arch.json` as an editable proposal, with a hand-written escape hatch for non-Python services.
- **Request-path trace in the engine** (`InternalTrace` on the analysis): origin-service log lines are attributed to stages via logger-name/module-prefix matching; the failing stage is the first error-level line; statuses keep the evidence rule — `ok` only with direct telemetry, `inferred` for silent stages the path shape implies, `failed`, `not-reached`, `dormant`. Narration gains two deterministic beats (the healthy walk, then the point of failure) in both template and LLM modes.
- Bundled `gateway-auth-rejection` scenario now carries hary-ai's internal pipeline (rate-limit → chat-endpoint → pii-scanner → input-guardrail → conversation-context → query-rewriter → llm-client, with dormant router/agentic branches) and stage-attributed events, so the demo shows the 401 dying exactly where it died: the LLM client.

### Notes

- Services without internals render exactly as in 0.4.0 — the dive only happens when there is a pipeline to dive into and a failing stage attributable from evidence.

## 0.4.0 - 2026-07-17

### Added

- **Cinematic renderer** (`--style cinematic`, now the default): the video is one continuous 3D shot instead of per-beat stills. A perspective camera glides between the services the narration is talking about; nodes are extruded slabs on a floor grid that morph, lift and pulse through their state changes; request particles stream along dependency edges and reverse, turn hot and accelerate when a propagation edge ignites; failures ripple shockwaves across the floor; a bloom pass makes the hot elements glow. Rendered in pure Python (numpy + Pillow, DejaVu fonts bundled) and streamed straight into ffmpeg as raw RGB at 30 fps — no browser, no GPU, no new system dependencies. Deterministic: identical inputs render identical videos.
- Render profiles: `high` (1920x1080@30, supersampled), `preview` (1280x720@24, fast), `ultra` (2560x1440@30). Narration beats are placed on a single mixed audio track at their exact start times, so the voice stays in sync with the camera.
- **Live mode**: `incidentlens watch` tails real log files, detects a failure burst (N error-level lines within a sliding wall-clock window, debounced with a cooldown), runs the deterministic analysis over the recent telemetry and renders the movie automatically — plus a briefing.md and analysis.json next to it. `incidentlens analyze` is the same path as a one-shot over saved logs.
- **Log connector** (`connectors/logfile.py`): turns real log lines into canonical telemetry. Understands the Python `logging` default format (timestamp - logger - LEVEL - message), JSON lines, Spring Boot, bare ISO-prefixed lines and level-only lines; multi-line tracebacks stay attached to their error. Files are tailed incrementally and rotation-safe.
- **Repository discovery** (`incidentlens discover`): scans a checkout — docker-compose services, service directories with build manifests (one level of nesting included), cross-references in config files, and external `http(s)` gateways found in settings — and writes an editable `incidentlens.arch.json` plus a starter `incidentlens.config.json` for the watch command.
- Bundled scenario `gateway-auth-rejection`: an AI microservice pointed at a shared LLM gateway gets `401 Unauthorized` on every completion call after a virtual-key config change; the failure crosses the BFF and takes the assistant panel down. Notably, the gateway itself reports no telemetry — the analysis says so instead of guessing which credential is wrong.
- `classic` style keeps the original per-beat still renderer for web parity and constrained environments.

### Changed

- `requires-python` lowered to 3.10 (StrEnum/importlib compat shims).
- The credential recommended action no longer assumes the rejecting party is a datastore; it now covers API gateways and external services, including the auth scheme mismatch case.
- Version 0.4.0; studio extra now pulls numpy and Pillow.

## 0.3.0 - 2026-07-12

### Added

- **IncidentLens Studio** (`incidentlens[studio]`): render an incident as a narrated MP4. Deterministic visuals — the same dependency-graph replay the web UI shows, ported to a server-side SVG renderer and rasterized frame by frame — with a generative narration layer over the top. Every narrated line stays bound to the evidence in the analysis, and an inferred cause is spoken as what the evidence points to, not as fact.
- Two narration modes: `template` (deterministic lines built from the analysis; no network, the default and demo fallback) and `llm` (Claude writes the narration from the analysis JSON, constrained to the recorded evidence, defaulting to `claude-sonnet-5` and overridable via `INCIDENTLENS_NARRATION_MODEL`). The LLM path falls back to template narration on any error so a live demo never dead-ends.
- Three voices behind a one-method `Voice` protocol: `OfflineVoice` (espeak-ng, free), `ElevenLabsVoice` (neural, for the launch video), and `SilentVoice` (timed silence, for CI and muted renders).
- Optional Backblaze B2 upload via the S3-compatible API (`--upload-b2`), returning a presigned URL.
- `incidentlens studio <scenario>` CLI subcommand (`--out`, `--voice`, `--narration`, `--fps`, `--upload-b2`).
- Studio test suite: narration coverage and speakability, per-beat state folding, direction-insensitive propagation edges, layout ordering, and a guarded end-to-end render smoke test.
- `docs/STUDIO.md` and a README section covering install, the generative path, the Python API, and Backblaze setup.

### Notes

- Rendering needs ffmpeg; the offline voice needs espeak-ng. Both are documented and installed in CI so the render test runs there.

## 0.2.0 - 2026-07-12

### Changed

- Replaced the hardcoded demo engine with a real deterministic analysis pipeline: signal classification, in-series metric anomaly detection, origin identification, scored change correlation, dependency-graph propagation mapping, hypothesis assembly and provenance validation. Output is now computed from the telemetry it is given.
- Restructured bundled data into `data/scenarios/<name>/` (scenario.json, architecture.json, events.json) so new incidents can be added without code changes.
- Rebuilt the web UI around an animated architecture replay: dependency-graph layout, per-frame node states, propagation edge activation, transport controls (play, step, scrub, speed), keyboard support and a live caption tied to evidence IDs.
- Rewrote the API: scenario listing and detail endpoints, 404 on unknown scenarios, 422 when telemetry contains no incident.
- Rewrote the README to document how the engine reasons, including thresholds and the change-correlation confidence formula.

### Added

- Second bundled scenario `cache-stampede`: a no-deployment incident with load-driven propagation and an observed recovery, demonstrating the engine generalizes across failure types.
- `PropagationStep` model and a propagation view in the UI.
- Recovery detection and recovery-state recommended actions (verification, runbook, postmortem).
- `NoIncidentDetected` error for clean telemetry.
- Engine and API test suite covering root-cause identification, propagation, missing-evidence detection, cross-scenario distinctness, evidence-ID integrity and the no-incident path.

### Fixed

- Repository now passes its own CI (ruff, mypy strict, pytest) from a clean checkout.
- Interpolated strings in the UI are HTML-escaped.

## 0.1.0 - 2026-07-10

### Added

- FastAPI application
- synthetic checkout incident
- connector interface
- deterministic analysis engine
- evidence-backed timeline
- root-cause hypotheses
- engineer and executive briefings
- visual replay
- test suite
- Docker support
- GitHub Actions CI
