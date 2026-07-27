# Live mode: real repo, real logs, automatic movies

Live mode is the loop IncidentLens was built for: your services write logs,
something breaks in production, and by the time you open your laptop there is
a narrated 3D replay of what happened waiting for you — plus the briefing and
the raw analysis JSON.

```text
your services ──write──▶ log files ──tail──▶ burst detector ──▶ analysis engine
                                                                     │
                          incidentlens-videos/INC-....mp4  ◀──render─┘
                          incidentlens-videos/INC-....briefing.md
                          incidentlens-videos/INC-....analysis.json
```

No agents and no code changes in your services: a log file is the whole
integration surface. If a service logs to stdout, redirect it:

```bash
python main.py 2>&1 | tee logs/hary-ai.log
```

## 1. Discover the architecture

From your repository root:

```bash
incidentlens discover .
```

This scans the checkout and writes two files:

- `incidentlens.arch.json` — the proposed service graph. Sources: docker-compose
  services and `depends_on`; top-level directories carrying a build manifest
  (`package.json`, `pyproject.toml`, `pom.xml`, `build.gradle`, `go.mod`, one
  nesting level included); cross-references between services found in config
  files; and external `http(s)` endpoints in settings, which become external
  gateway nodes (a shared LLM gateway, for example).
- `incidentlens.config.json` — log-source stubs per service plus watch settings.

**Edit both.** The scan is a proposal, not a verdict: fix dependency directions
it guessed wrong, delete services you don't run, and point each log entry at
the real file (globs allowed).

### Service internals (the nuts and bolts)

For each Python service, `discover` also reads the code with ``ast`` (nothing
is imported or executed) and proposes the service's *internal* pipeline:

- LangGraph ``add_edge("a", "b")`` / ``add_conditional_edges(...)`` string
  wiring becomes the stage graph, node by node;
- FastAPI/Flask route decorators become the entry stage;
- ``app.add_middleware(...)`` chains run before the entry, in execution order;
- modules with client-ish names (llm, client, model, provider, transport)
  imported by several stages become fan-in stages — the last hop before an
  external dependency, e.g. ``llm-client``.

Each stage lists the dotted module prefixes its log lines are written under
(``hary.models.llm_factory`` → ``llm-client``), which is how the analysis
attributes real telemetry to stages. The result lands in each service's
``internals`` block inside ``incidentlens.arch.json`` — edit it like the rest,
or write one by hand for services the scanner can't read (Java, Node):

```json
"internals": {
  "entry": "rate-limit",
  "stages": [
    {"name": "rate-limit", "modules": ["middleware.rate_limit"]},
    {"name": "chat-endpoint", "modules": ["hary.transport.routes", "main"]},
    {"name": "llm-client", "modules": ["hary.models.llm_factory"]}
  ],
  "edges": [["rate-limit", "chat-endpoint"], ["chat-endpoint", "llm-client"]]
}
```

When an incident's origin service has internals, the analysis adds an
``internal_trace`` — the request's path, where it died, and each stage's
status (``ok`` with evidence, ``inferred``, ``failed``, ``not-reached``,
``dormant``) — and the video dives into the service to show it.

## 2. Watch

```bash
incidentlens watch --config incidentlens.config.json
```

The watcher tails every configured file (rotation-safe, incremental), parses
each new line into canonical telemetry, and keeps a rolling buffer. When
`error_threshold` error-level lines arrive within `window_seconds` (wall
clock, so replayed logs trigger too), it waits `settle_seconds` for the burst
to finish, runs the same deterministic engine the web UI uses over the recent
buffer, and renders the cinematic MP4. Then it cools down for
`cooldown_seconds` so a sustained outage produces one movie, not fifty.

Everything can also be passed inline without a config file:

```bash
incidentlens watch \
  --logs hary-ai=logs/hary-ai.log \
  --logs hary-bff=logs/hary-bff.log \
  --arch incidentlens.arch.json \
  --threshold 3 --window 90 \
  --voice offline --profile high
```

## 3. Or analyze after the fact

Already have the log from an incident? One shot:

```bash
incidentlens analyze --logs hary-ai=incident.log --arch incidentlens.arch.json
```

Same analysis, same movie, no daemon.

## Log formats understood

Per line, tried in order:

| Format | Example |
| --- | --- |
| JSON lines | `{"timestamp": "...", "level": "error", "message": "..."}` |
| Python logging default | `2026-07-17 10:23:45,123 - pkg.module - ERROR - msg` |
| Spring Boot | `2026-07-17 10:23:45.123 ERROR 42 --- [thread] c.a.Cls : msg` |
| ISO prefix | `2026-07-17T10:23:45Z ERROR msg`, `[2026-07-17 10:23:45] [error] msg` |
| Level only | `ERROR: msg` (inherits the last timestamp seen in the file) |

Anything else survives as INFO detail attached to the previous timestamp, so
multi-line tracebacks stay with their error instead of vanishing.

## What triggers a video

An **error burst**: `error_threshold` lines at ERROR/CRITICAL/FATAL within
`window_seconds` of arrival. INFO/WARN lines never trigger on their own —
they become the baseline and context the analysis reasons over. If the engine
finds no incident in the buffer (it refuses to invent one), the watcher logs
that and keeps watching.

## The evidence rule still holds

Live mode changes where telemetry comes from, not what the engine is allowed
to say. A cause is `inferred` with a confidence, never asserted; services that
sent no telemetry are listed as gaps (your gateway's silence is a finding,
not a footnote); and every claim in the video, the briefing and the JSON
carries the IDs of the log lines that support it.
