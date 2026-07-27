from __future__ import annotations

import json
from importlib.resources import files

try:  # Python 3.11+
    from importlib.resources.abc import Traversable
except ImportError:  # pragma: no cover - Python 3.10
    from importlib.abc import Traversable

from incidentlens.connectors.base import TelemetryConnector
from incidentlens.domain.models import ArchitectureGraph, ScenarioInfo, TelemetryEvent

DEFAULT_SCENARIO = "checkout-secret-rotation"


def _scenarios_root() -> Traversable:
    return files("incidentlens.data").joinpath("scenarios")


def available_scenarios() -> list[ScenarioInfo]:
    """List the synthetic scenarios bundled with the package."""
    scenarios: list[ScenarioInfo] = []
    for entry in _scenarios_root().iterdir():
        meta = entry.joinpath("scenario.json")
        if meta.is_file():
            raw = json.loads(meta.read_text(encoding="utf-8"))
            scenarios.append(ScenarioInfo.model_validate(raw))
    return sorted(scenarios, key=lambda item: item.name)


class SyntheticConnector(TelemetryConnector):
    """Reads a bundled scenario from package data.

    Real connectors (Datadog, CloudWatch, OpenTelemetry, ...) implement the same
    two methods against live telemetry.
    """

    def __init__(self, scenario: str = DEFAULT_SCENARIO) -> None:
        if scenario == "default":
            scenario = DEFAULT_SCENARIO
        root = _scenarios_root().joinpath(scenario)
        if not root.joinpath("scenario.json").is_file():
            known = ", ".join(item.name for item in available_scenarios())
            raise KeyError(f"Unknown scenario '{scenario}'. Available: {known}")
        self.scenario = scenario
        self._root = root

    def _read_json(self, name: str) -> object:
        return json.loads(self._root.joinpath(name).read_text(encoding="utf-8"))

    def info(self) -> ScenarioInfo:
        raw = self._read_json("scenario.json")
        return ScenarioInfo.model_validate(raw)

    def fetch_events(self) -> list[TelemetryEvent]:
        raw = self._read_json("events.json")
        assert isinstance(raw, list)
        return [TelemetryEvent.model_validate(item) for item in raw]

    def fetch_architecture(self) -> ArchitectureGraph:
        raw = self._read_json("architecture.json")
        assert isinstance(raw, dict)
        return ArchitectureGraph.model_validate(raw)

    def fetch_code_graphs(self) -> dict:
        """Optional bundled code network (codegraph.json) — {} when absent."""
        from incidentlens.domain.models import CodeGraph

        meta = self._root.joinpath("codegraph.json")
        if not meta.is_file():
            return {}
        raw = json.loads(meta.read_text(encoding="utf-8"))
        return {
            name: CodeGraph.model_validate(data)
            for name, data in raw.get("services", {}).items()
        }
