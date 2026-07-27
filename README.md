# IncidentLens

Evidence-backed incident reconstruction for distributed systems.

IncidentLens reads architecture metadata, logs, metrics and deployment events, then rebuilds what happened during an incident: which service failed first, what changed right before it, how the failure spread through the dependency graph, and what it did to customers. The result is a cinematic 3D replay of the incident — a continuous camera shot over your architecture, narrated beat by beat — plus a briefing an on-call engineer can act on. Point it at a repository and its log files and it does this for real incidents, automatically, as they happen.

> When production fails at 2:00 AM, IncidentLens reconstructs the incident before the on-call engineer opens their laptop.

Presenting the project at a demo or hackathon? Use the
[under-three-minute runbook](HACKATHON_RUNBOOK.md).

## The rule this project is built around

**A guessed root cause is never presented as fact.**

Production incidents are messy. Logs are incomplete, clocks drift, several things break at once. So every conclusion IncidentLens produces carries a status and a confidence score, and every claim links back to the evidence IDs that support it:

- `confirmed` — direct telemetry shows it happened
- `inferred` — the evidence points this way, but a human should verify
- `unknown` — a real possibility the available telemetry cannot settle

The analysis also lists what it *couldn't* see: services that reported no telemetry, missing audit logs, absent change history. Knowing what the system doesn't know is half the point.

## Quick start

Runs on macOS, Linux, or Windows via WSL2. Core IncidentLens and Studio
support Python 3.10+; the optional Genblaze integration requires Python 3.11+.

```bash
git clone https://github.com/Abhinav0905/Incidentlens.git
cd Incidentlens

python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

incidentlens serve
```

Open http://127.0.0.1:8000, pick a scenario, hit **Reconstruct incident**. Or use Docker:

```bash
docker compose up --build
```

## What you get

For each incident the engine emits a single `IncidentAnalysis` document:

- a timeline from healthy state through failure to (when observed) recovery
- ranked hypotheses with status, confidence and supporting evidence IDs
- the propagation chain: which service degraded which, and by what mechanism
- customer impact, stated from direct evidence only
- missing evidence — the gaps a responder should close first
- recommended checks and remediation, ordered by priority, each with a stated risk
- engineer briefing and executive summary
- a replay script that drives the animated architecture view in the UI

The UI plays the incident like a recording: nodes shift from healthy to warning to critical as the timeline advances, propagation edges light up as the failure crosses them, and a caption ties each frame to its evidence.

## Bundled scenarios

Seven synthetic incidents ship with the repo. They exist so you can see the engine
reason over genuinely different failures — same code, no scenario-specific branches.

### One application, four failures

Four of them run against the *same* service graph and the *same* 16-module code graph:
`hary-platform`, an AI assistant. Learn the architecture once, then watch a different
part of it fail each time. The point is that the analysis is not tuned to a failure —
only the telemetry changes.

| Scenario | What breaks | Where the engine lands | Depth |
| --- | --- | --- | --- |
| `rate-limit-exhaustion` | Redis connection pool exhausts under a traffic surge; the limiter fails closed and every request is rejected with `429` before it reaches the graph | `middleware.rate_limit` | front door |
| `pii-guardrail-crash` | An unusual unicode sequence crashes PII redaction. Well-formed requests keep working, so only *part* of the traffic breaks | `hary.guardrails.pii._get_presidio` | mid-pipeline |
| `gateway-auth-rejection` | A freshly configured virtual key is rejected `401` on both auth schemes; the circuit breaker opens and the assistant panel goes dark | `hary.models.llm_factory.get_llm_for_tier` | LLM client |
| `agentic-retry-exhaustion` | A prompt-pack change makes the agent misread a tool-call payload as retryable. It burns its retry budget, salvages a truncated answer, and returns **HTTP 200** | `hary.graph.nodes.agent.AgentNode.__call__` | agentic node |

The last one is the interesting case: nothing 5xxs and every availability dashboard
stays green. HTTP success holds at 99.6% while partial-answer salvages climb from 0.3%
to 44.8%, p95 latency triples, and thumbs-down goes 3/hr to 41/hr. A silent quality
regression is the failure a log tail cannot show you.

`rate-limit-exhaustion` resolves to a module but not a function, because
`middleware.rate_limit` has no symbols in the bundled code graph. That is deliberate:
the tool degrades to module level rather than inventing a stack frame it cannot support.

### Three other architectures

To show the analysis is not tuned to one system: `cache-stampede` (a cold cache floods
the index tier until the database saturates, then recovers on its own — no deployment
involved), `checkout-secret-rotation` (a deployment repoints a database secret and order
workers back up), and `queue-poison-message` (a schema change produces one un-decodable
record and the consumer crash-loops).

Run any of them. The outputs differ because the telemetry differs — that's the
demonstration.

## Watch your own system live

The live loop needs no agents and no code changes — a log file is the whole integration surface:

```bash
pip install -e ".[studio]"              # plus ffmpeg

incidentlens discover .                  # scan the repo -> architecture proposal + config
incidentlens watch --config incidentlens.config.json
```

`discover` derives the service graph from your checkout (docker-compose, service directories, config cross-references, external gateways found in settings — both files are editable proposals). It also scans each Python service's *insides* with `ast` — LangGraph node wiring, FastAPI entrypoints, middleware chains, shared LLM/client modules — so the video can later dive into the failing service and walk the request through its internal stages. `watch` then tails the configured log files; when a burst of error-level lines arrives, it reconstructs the incident from the recent telemetry and renders the movie plus a briefing, automatically. `incidentlens analyze --logs svc=incident.log` does the same once, after the fact. Details: [docs/LIVE.md](docs/LIVE.md).

A runnable end-to-end example lives in [demo/model-id-typo/](demo/model-id-typo/): a one-character typo in `FAST_TIER_MODEL_ID` (`modelhst` for `vendor`) that fails every LLM call. Nothing about the incident is hard-coded — `incidentlens analyze --config demo/model-id-typo/incidentlens.config.json` reads the raw logs, reconstructs it, attributes the logged failure to `hary.models.llm_factory`, and uses the static call graph to identify `get_llm_for_tier` as a candidate locus. Swap the log files for your service's real logs and it behaves the same.

## Studio: the incident as a movie

`incidentlens[studio]` renders an incident as a narrated MP4 — one continuous 3D shot, not a slideshow. A perspective camera glides across the architecture to whatever the narration is talking about; services are slabs on a floor grid that lift, recolor and pulse as their state changes; request traffic streams as particles along the dependency edges and turns hot and reverses when the failure crosses them; shockwaves ripple out of state changes; the hot parts bloom. Everything is rendered in pure Python and piped to ffmpeg at 1080p30 — no browser, no GPU — and it is deterministic: the visuals can't show anything the analysis didn't find. The narration is the generative part, with an LLM writing the story and a text-to-speech voice reading it; a cause is spoken as what the evidence points to, not as fact.

**The dive.** When the failing service's internal pipeline is known (see the scanner below), the movie doesn't stop at the service boundary. At the failure beat the camera follows the request stage by stage — logged or inferred traversal in teal, dormant context dim, and the logged failing stage red. When `incidentlens.codegraph.json` is present, it then opens the complete package-grouped module blueprint, establishes every module and edge, glides into the failed package, and overlays the incident: the log-attributed module is red, structural dependency risk is amber, and static-only context remains dim. The final level opens the focused function blueprint with methods nested inside classes and classes inside modules. It lands on the candidate function in amber and explicitly labels it as static inference rather than a confirmed runtime frame. Without a repository code graph, the earlier compact dependency and caller/callee views remain the fallback.

## The code as a network

The third level of depth. `incidentlens graph` walks a repository with pure `ast` (nothing imported, nothing executed) and renders every service's *code* as a dependency network — one self-contained HTML file, no CDN:

```bash
incidentlens graph . --out code-graph.html
```

The scan is now two graphs in one, built in a single pass:

- **Module network** — every internal module is a node; imports and *resolved calls* (through import aliases, symbols included) are the edges. Each module also carries its **fan-in / fan-out**, its **blast radius** (how many modules transitively depend on it), and whether it sits in an import **cycle**.
- **Call graph** — the `module.Class.method` / `module.function` layer. Nodes are functions, methods and classes; edges are resolved calls between them, best-effort in the spirit of `pyan`: `from x import f` origins, module-alias calls, `self.method()`, single-assignment local typing (`x = Thing(); x.foo()`), constructor calls, and **dynamic imports** (`importlib.import_module("a.b")`, `__import__`) that static scanning otherwise misses.

Click any node in the interactive view and the panel answers the 2 AM questions directly: **who calls this** and **what does this call** — `hary.guardrails.pii ← input_guardrail (scan)`, `hary.models.llm_factory.get_llm ← agent.AgentNode.__call__`. Toggle **module ⁄ symbol** to drill from files into functions; nodes are sized by blast radius, cycle members ring dashed-amber, and colour follows functional role (endpoint · client · config · middleware · graph-node · logic). Search, zoom, drag; switch between services. Pass `--analysis INC-….analysis.json` and the incident lands on the map: traversed modules ring teal, a module named by linked error logs is red, and a statically selected function candidate is dashed amber.

### Mermaid, hierarchical and colour-coded

`--mermaid` also emits a [Mermaid](https://mermaid.js.org) diagram (`.mmd` source plus a one-click viewer HTML) — paste it into a PR, a runbook, GitHub, or the Mermaid Chart connector:

```bash
incidentlens graph . --mermaid --level module                       # package-grouped overview
incidentlens graph . --mermaid --level symbol --focus hary.models.llm_factory
```

At `--level symbol` the diagram is genuinely hierarchical — **methods nest inside class subgraphs, classes inside module subgraphs** — with `classDef` colours per role and coupled clusters flagged. Because a real service's full call graph is too dense to read whole, symbol level scopes to a `--focus` (a module or `module.Class.method`) and its immediate callers and callees. `analyze` and `watch` emit both the interactive HTML and a failure-focused `.mmd` next to the video when `discover` has written `incidentlens.codegraph.json`.

```bash
pip install -e ".[studio]"              # plus ffmpeg
incidentlens studio gateway-auth-rejection --out incident.mp4
```

With `OPENAI_API_KEY` configured, the default `auto` voice uses OpenAI's calm
neural TTS; without a key it falls back locally. When the failing service has
a call graph, the narration names the candidate **function**, who calls it,
its structural blast radius, and any cycle it's trapped in — without claiming
that static attribution is a runtime stack frame. Add
`--intro-video PATH` to prepend a short Sora/Veo/Runway bumper without handing
the technical diagrams to a generative model. For voice controls, long-form
composition, Genblaze provenance, and Backblaze B2 publishing, see
[docs/STUDIO.md](docs/STUDIO.md).

### Genblaze media provenance

Install the additive Genblaze integration on Python 3.11 or newer:

```bash
pip install -e ".[studio,genblaze]"          # plus ffmpeg

export OPENAI_API_KEY=...
export INCIDENTLENS_GENBLAZE_VOICE=coral

incidentlens studio gateway-auth-rejection \
  --voice genblaze \
  --publish-genblaze \
  --out incident.mp4
```

This runs every narration beat through one Genblaze
`OpenAITTSProvider` pipeline, classifies prompt visibility as private, and
binds the generated WAVs to SHA-256 declarations. The classification is
provenance metadata, not encryption or redaction: an exported canonical
narration manifest still contains the spoken narration text, so protect it
like any incident artifact. Genblaze 0.3.x currently accepts `coral` but not
the newer `marin` or `cedar` voice names. Direct `--voice openai` remains the
default route for `marin` and compatible custom speech endpoints.

`--publish-genblaze` leaves `incident.mp4` byte-for-byte unchanged and writes
`incident.genblaze.json` beside it. With `--voice genblaze`, it also writes a
narration manifest. That narration manifest is canonically verifiable, but its
beat-level `file://` URLs refer to temporary WAVs removed after rendering; it
is not later fetch-verifiable unless those audio files are persisted
separately.

To publish through Genblaze's B2 sink:

```bash
export B2_BUCKET=incidentlens-demo
export B2_REGION=us-west-004
export B2_KEY_ID=...
export B2_APP_KEY=...

incidentlens studio gateway-auth-rejection \
  --voice genblaze \
  --upload-genblaze-b2 \
  --out incident.mp4
```

This uploads the original MP4 and canonical manifest with hierarchical keys,
then embeds that manifest into the local MP4. The embedded local copy is
therefore byte-different from the original object whose SHA is stored in B2.
`B2_APPLICATION_KEY` and a standard `B2_ENDPOINT_URL` remain accepted as
legacy aliases. The old direct `--upload-b2` command is retained separately
and still returns a time-limited presigned URL.

## How the engine reasons

The default engine (`engines/deterministic.py`) is rule-based and fully documented in-source. No ML, no external calls, reproducible output for identical input. The pipeline:

1. **Signal classification.** Each telemetry event is labeled baseline, change, failure, warning or recovery, using log levels and metric behavior.
2. **Metric anomaly detection.** A metric series is anomalous when it rises to ≥3x its own in-series baseline (or falls to ≤1/3), with single-point heuristics for error counts, saturation ≥95%, backlogs and extreme latency. A series that returns near baseline after an anomaly marks a recovery.
3. **Origin identification.** Failure signals are merged per service within a 90-second window; the earliest failing service is the origin.
4. **Change correlation.** Changes are scored against the origin failure: base 0.45, +0.25 if the failure follows within 5 minutes (+0.15 within 15), +0.10 for same-service, +0.10 for keyword affinity (credential, capacity or config terms appearing in both change and failure), capped at 0.95. Below 0.55 the change is reported as coincident, not causal.
5. **Propagation mapping.** The dependency graph is walked from the origin. A degraded service that depends on an already-degraded service becomes an upstream-dependency step; a degraded service that an already-degraded service depends on becomes a downstream-pressure step. Mechanisms (timeouts, load, backlog) come from keyword rules over the linking evidence.
6. **Hypothesis assembly.** Root cause and propagation are `inferred` (propagation capped at 0.88). Customer impact is `confirmed` only when a user-facing service shows direct failure evidence. Questions the telemetry can't answer are emitted as `unknown` with confidence 0.0.
7. **Provenance validation.** Every evidence ID referenced by any conclusion must exist in the evidence set, or the engine raises. No dangling citations.

Thresholds are constants at the top of the module, with comments explaining each choice. Disagree with one? That's a welcome pull request.

## API

```http
GET  /api/v1/health
GET  /api/v1/scenarios                    # list bundled scenarios
GET  /api/v1/scenarios/{name}             # architecture + raw events for one scenario
POST /api/v1/incidents/analyze            # {"scenario": "checkout-secret-rotation"}
```

`analyze` returns the full `IncidentAnalysis` as JSON. Unknown scenarios return 404. Telemetry with no failure signals returns 422 — no incident is invented where none is observed.

## Architecture

```text
Telemetry sources (logs, metrics, deployments, architecture metadata)
        │
        ▼
Connector interface  ──►  canonical event model
        │
        ▼
Analysis engine (signals → anomalies → origin → change correlation
                 → propagation → hypotheses → provenance check)
        │
        ▼
IncidentAnalysis  ──►  replay UI · briefings · action plan · evidence record
```

```text
src/incidentlens/
├── api.py               FastAPI routes
├── cli.py               `incidentlens serve`
├── domain/              models + errors
├── connectors/          telemetry integrations (synthetic included)
├── engines/             analysis engines (deterministic included)
├── services/            orchestration
├── data/scenarios/      bundled incidents (scenario.json, architecture.json, events.json)
└── static/              replay UI
```

## Writing a connector

Connectors adapt a telemetry source to the canonical event model:

```python
from incidentlens.connectors.base import TelemetryConnector
from incidentlens.domain.models import ArchitectureGraph, TelemetryEvent

class MyConnector(TelemetryConnector):
    def fetch_events(self) -> list[TelemetryEvent]: ...
    def fetch_architecture(self) -> ArchitectureGraph: ...
```

Wanted: OpenTelemetry, Datadog, CloudWatch, Grafana Loki, Elastic, Kubernetes events, GitHub Actions deployments. A new synthetic scenario is also a great first contribution — three JSON files in `data/scenarios/<name>/`, no Python required.

## Limitations

Honest list, current release:

- **Synthetic data only.** All seven scenarios are hand-built. Real connectors are the top roadmap item.
- **Clock skew is not corrected.** Event ordering trusts source timestamps. Real systems need skew tolerance before cross-source ordering can be trusted.
- **Heuristic thresholds.** The 3x anomaly ratio and 5/15-minute change windows are sensible defaults, not learned values.
- **Single-origin assumption.** Concurrent independent failures are ranked by earliest onset; true multi-origin incidents aren't modeled yet.
- **No auth, single tenant.** This is a local analysis tool right now, not a hosted service.

## Roadmap

1. OpenTelemetry connector (real telemetry in, same analysis out)
2. Motion between states in Studio videos (currently hard cuts between beats)
3. Clock-skew tolerance in event ordering
4. Replay export (shareable incident recordings)
5. Postmortem draft generation from the analysis document
6. Policy-gated action approval

## Development

```bash
make install   # editable install with dev deps
make lint      # ruff + mypy (strict)
make test      # pytest
make run       # serve on 127.0.0.1:8000
```

CI runs the same three checks on every push.

## Contributing · Security · License

See [CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md). Apache-2.0.
