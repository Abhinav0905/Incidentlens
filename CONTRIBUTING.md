# Contributing

Thanks for considering a contribution to IncidentLens.

## Good first contributions

- add a telemetry connector
- add a new synthetic incident scenario (three JSON files in `src/incidentlens/data/scenarios/<name>/` — scenario.json, architecture.json, events.json — no Python needed)
- improve timeline reconstruction
- add export formats
- improve accessibility
- add tests
- improve documentation

## Local setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## Before opening a pull request

```bash
ruff check .
mypy src
pytest --cov=incidentlens
```

## Pull request guidelines

- keep changes focused
- add tests for new behavior
- document new public interfaces
- avoid vendor-specific logic in the domain layer
- never commit secrets, customer logs or production data
- label inferred conclusions clearly

## Commit style

Examples:

```text
feat: add OpenTelemetry connector
fix: preserve event ordering across sources
docs: add local deployment guide
test: cover missing-evidence detection
```
