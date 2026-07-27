"""Full repository-derived blueprint scenes."""

from __future__ import annotations

import pytest

from incidentlens.connectors.code_graph import enrich_trace_with_code
from incidentlens.connectors.synthetic import SyntheticConnector
from incidentlens.engines.deterministic import DeterministicAnalysisEngine
from incidentlens.services.incidents import IncidentService
from incidentlens.studio.cinema.blueprint import (
    ModuleBlueprintScene,
    SymbolBlueprintScene,
    build_module_blueprint,
    build_symbol_blueprint,
)
from incidentlens.studio.narration import build_narration


def _scenario():
    connector = SyntheticConnector("gateway-auth-rejection")
    architecture = connector.fetch_architecture()
    analysis = IncidentService(
        connector=connector,
        engine=DeterministicAnalysisEngine(),
    ).analyze()
    graphs = connector.fetch_code_graphs()
    enrich_trace_with_code(analysis, graphs)
    return architecture, analysis, graphs["hary-ai"]


def test_module_blueprint_is_full_and_package_grouped() -> None:
    _architecture, analysis, graph = _scenario()
    layout = build_module_blueprint(graph, analysis)

    assert layout is not None
    assert set(layout.nodes) == {module.name for module in graph.modules}
    assert {"hary.graph", "hary.models", "hary.transport"} <= {
        group.label for group in layout.groups
    }
    assert layout.fail_key == "hary.models.llm_factory"
    assert layout.failure_confirmed
    assert len(layout.impact_nodes) == analysis.internal_trace.blast_radius


def test_symbol_blueprint_nests_methods_in_classes() -> None:
    _architecture, analysis, graph = _scenario()
    layout = build_symbol_blueprint(graph, analysis)

    assert layout is not None
    assert layout.fail_key == "hary.models.llm_factory.get_llm_for_tier"
    assert layout.fail_key in layout.nodes
    nested = {group.label for group in layout.groups if group.depth == 1}
    assert "class AgentNode" in nested
    assert "class TokenUsage" in nested
    assert any(node.label == "__call__()" for node in layout.nodes.values())
    assert not layout.failure_confirmed  # no runtime stack frame in this scenario


def test_movie_selects_blueprints_when_code_graph_is_available() -> None:
    pytest.importorskip("numpy")
    pytest.importorskip("PIL")
    from incidentlens.studio.cinema import Timeline, build_movie_scene
    from incidentlens.studio.cinema.engine import RenderSpec

    architecture, analysis, graph = _scenario()
    narration = build_narration(analysis, mode="template")
    timeline = Timeline(analysis, narration, [3.0] * len(narration.beats))
    movie = build_movie_scene(
        analysis,
        architecture,
        timeline,
        RenderSpec(640, 360, 12, 1.0),
        code_graph=graph,
    )

    assert len(movie.dives) == 3
    assert isinstance(movie.dives[1].scene, ModuleBlueprintScene)
    assert isinstance(movie.dives[2].scene, SymbolBlueprintScene)
    module_window = timeline.module_window()
    symbol_window = timeline.symbol_window()
    assert module_window is not None and symbol_window is not None
    module_frame = movie.frame(module_window[1] + 1.3)
    symbol_frame = movie.frame(symbol_window[1] + 1.3)
    assert module_frame.size == symbol_frame.size == (640, 360)
    assert module_frame.tobytes() != symbol_frame.tobytes()
