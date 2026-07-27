"""Internal pipeline: code scanner, request-path trace, narration, dive act."""

from __future__ import annotations

import textwrap

import pytest

from incidentlens.connectors.internals_scan import scan_service_internals
from incidentlens.connectors.synthetic import SyntheticConnector
from incidentlens.domain.models import StageStatus
from incidentlens.engines.deterministic import DeterministicAnalysisEngine
from incidentlens.services.incidents import IncidentService
from incidentlens.studio.narration import build_narration


def _analysis():
    connector = SyntheticConnector("gateway-auth-rejection")
    analysis = IncidentService(
        connector=connector, engine=DeterministicAnalysisEngine()
    ).analyze()
    # the real pipeline enriches the trace with the code graph before rendering
    code_graphs = connector.fetch_code_graphs()
    if code_graphs:
        from incidentlens.connectors.code_graph import enrich_trace_with_code

        enrich_trace_with_code(analysis, code_graphs)
    return connector.fetch_architecture(), analysis


# ------------------------------------------------------------------ scanner


def test_scanner_reads_langgraph_fastapi_and_shared_clients(tmp_path) -> None:
    svc = tmp_path / "svc"
    (svc / "app" / "nodes").mkdir(parents=True)
    (svc / "app" / "__init__.py").write_text("")
    (svc / "app" / "nodes" / "__init__.py").write_text("")
    (svc / "main.py").write_text(textwrap.dedent("""
        from fastapi import FastAPI
        from app.middleware import RateLimitMiddleware
        app = FastAPI()
        app.add_middleware(RateLimitMiddleware)

        @app.post("/v1/chat")
        async def chat(body: dict):
            return {}
    """))
    (svc / "app" / "middleware.py").write_text("class RateLimitMiddleware:\n    pass\n")
    (svc / "app" / "builder.py").write_text(textwrap.dedent("""
        from langgraph.graph import END, START, StateGraph

        def build(graph):
            graph.add_edge(START, "guardrail")
            graph.add_edge("guardrail", "rewrite")
            graph.add_conditional_edges("rewrite", pick, {"agent": "agent", "done": END})
            graph.add_edge("agent", END)
    """))
    (svc / "app" / "nodes" / "guardrail.py").write_text("import app.llm_client\n")
    (svc / "app" / "nodes" / "rewrite.py").write_text("import app.llm_client\n")
    (svc / "app" / "nodes" / "agent.py").write_text("import app.llm_client\n")
    (svc / "app" / "llm_client.py").write_text("def call():\n    pass\n")

    internals = scan_service_internals(svc)
    assert internals is not None
    names = {s.name for s in internals.stages}
    assert {"guardrail", "rewrite", "agent", "chat-endpoint", "rate-limit"} <= names
    assert "llm-client" in names
    assert internals.entry == "rate-limit"
    assert ("rate-limit", "chat-endpoint") in internals.edges
    assert ("chat-endpoint", "guardrail") in internals.edges
    assert ("rewrite", "llm-client") in internals.edges
    # module mapping feeds log attribution later
    rewrite = next(s for s in internals.stages if s.name == "rewrite")
    assert rewrite.modules == ["app.nodes.rewrite"]


# -------------------------------------------------------------------- trace


def test_trace_follows_the_request_to_the_failing_stage() -> None:
    _, analysis = _analysis()
    trace = analysis.internal_trace
    assert trace is not None
    assert trace.service == "hary-ai"
    assert trace.failing_stage == "llm-client"
    assert trace.path[0] == "rate-limit"
    assert trace.path[-1] == "llm-client"

    status = {t.stage: t.status for t in trace.stages}
    assert status["pii-scanner"] == StageStatus.OK  # logged its pass
    assert status["conversation-context"] == StageStatus.INFERRED  # silent, on path
    assert status["llm-client"] == StageStatus.FAILED
    assert status["router"] == StageStatus.DORMANT  # branch not taken

    failed = next(t for t in trace.stages if t.stage == "llm-client")
    assert failed.evidence_ids  # provenance survives into the trace
    assert "401" in failed.detail


def test_trace_absent_when_origin_service_has_no_internals() -> None:
    # Every bundled scenario now carries internals on its origin service, so the
    # "no internal pipeline declared" branch is exercised directly.
    from datetime import datetime, timezone

    from incidentlens.domain.models import ArchitectureGraph, ServiceNode, TelemetryEvent
    from incidentlens.engines.internal_trace import trace_internals

    origin_ts = datetime(2026, 7, 10, 2, 4, 12, tzinfo=timezone.utc)
    architecture = ArchitectureGraph(
        system="opaque-platform",
        services=[ServiceNode(name="opaque-service", depends_on=[])],
    )
    events = [
        TelemetryEvent(
            id="log-001",
            source_type="log",
            source="opaque-service",
            timestamp=origin_ts,
            detail="Database authentication failed for role app_rw.",
            attributes={"level": "ERROR", "logger": "opaque.db.credentials"},
        )
    ]
    assert trace_internals(events, architecture, "opaque-service", origin_ts) is None


# ---------------------------------------------------------------- narration


def test_narration_gains_internal_beats_after_the_origin_failure() -> None:
    _, analysis = _analysis()
    narration = build_narration(analysis, mode="template")
    kinds = [b.kind for b in narration.beats]
    assert "internal_path" in kinds and "internal_fail" in kinds
    path_i = kinds.index("internal_path")
    assert kinds[path_i + 1] == "internal_fail"
    assert kinds[path_i - 1] == "event"  # right after the origin failure beat
    fail_beat = narration.beats[kinds.index("internal_fail")]
    assert "llm client" in fail_beat.text or "llm-client" in fail_beat.title


def test_narration_drills_from_stage_to_module_to_symbol() -> None:
    _, analysis = _analysis()
    narration = build_narration(analysis, mode="template")
    kinds = [b.kind for b in narration.beats]
    assert "module_path" in kinds and "module_fail" in kinds
    assert "symbol_path" in kinds and "symbol_fail" in kinds
    # One continuous hierarchy: service path -> module graph -> static symbol.
    assert kinds.index("module_path") == kinds.index("internal_fail") + 1
    assert kinds.index("module_fail") == kinds.index("module_path") + 1
    assert kinds.index("symbol_path") == kinds.index("module_fail") + 1
    assert kinds.index("symbol_fail") == kinds.index("symbol_path") + 1
    module_fail = narration.beats[kinds.index("module_fail")]
    path = narration.beats[kinds.index("symbol_path")]
    fail = narration.beats[kinds.index("symbol_fail")]
    assert "llm_factory" in module_fail.title
    assert "potential dependents" in module_fail.text
    # Names the static function candidate and explicitly avoids overclaiming.
    assert "get_llm_for_tier" in path.title
    assert "get llm" in path.text  # spoken form of the failing symbol
    assert "model id for tier" in path.text  # a callee, spoken
    assert "get_llm_for_tier" in fail.title
    assert "not a confirmed stack frame" in fail.text

    dive_words = sum(
        len(beat.text.split())
        for beat in narration.beats
        if beat.kind.startswith(("internal", "module", "symbol"))
    )
    assert dive_words <= 150  # preserve a comfortable sub-three-minute cut


# --------------------------------------------------------------- module view


def test_module_view_centers_failure_locus_and_static_neighbors() -> None:
    architecture, analysis = _analysis()
    from incidentlens.studio.cinema.dependencies import build_module_view

    trace = analysis.internal_trace
    assert trace is not None
    service = next(s for s in architecture.services if s.name == trace.service)
    view = build_module_view(trace, service.internals, analysis)
    assert view is not None
    # Structured logger provenance confirms the module even when the readable
    # stage detail does not contain a bracketed logger suffix.
    assert view.failure_confirmed
    assert view.meta[view.fail_node].module == "hary.models.llm_factory"
    assert len(view.callers) == 3
    assert len(view.dependencies) == 3
    modules = {meta.module for meta in view.meta.values()}
    assert {
        "hary.prompts.query_rewrite",
        "hary.graph.nodes.agent",
        "hary.graph.nodes.small_talk",
        "hary.models.llm_factory",
        "config.settings",
        "hary.auth.headers",
        "hary.transport.http",
    } == modules

    observed = {
        meta.module for meta in view.meta.values() if meta.observed
    }
    dormant = {
        meta.module for meta in view.meta.values() if meta.dormant
    }
    assert observed == {"hary.prompts.query_rewrite"}
    assert "hary.graph.nodes.small_talk" in dormant
    assert "hary.prompts.query_rewrite" not in dormant


def test_module_timeline_uses_red_only_for_log_confirmed_module() -> None:
    architecture, analysis = _analysis()
    from incidentlens.studio.cinema.dependencies import (
        ModuleTimeline,
        build_module_view,
    )

    trace = analysis.internal_trace
    assert trace is not None
    service = next(s for s in architecture.services if s.name == trace.service)
    view = build_module_view(trace, service.internals, analysis)
    assert view is not None
    timeline = ModuleTimeline(view, (0.0, 4.0, 8.0))

    assert timeline.node_state_at(view.fail_node, 0.0)[0] == "healthy"
    assert timeline.node_state_at(view.fail_node, 5.5)[0] == "critical"
    for node in view.callers:
        state = timeline.node_state_at(node, 5.5)[0]
        if view.meta[node].dormant:
            assert state == "dormant"
        else:
            assert state == "warning"
    for node in view.dependencies:
        assert timeline.node_state_at(node, 5.5)[0] == "healthy"

    unconfirmed_view = build_module_view(trace, service.internals)
    assert unconfirmed_view is not None and not unconfirmed_view.failure_confirmed
    unconfirmed_timeline = ModuleTimeline(unconfirmed_view, (0.0, 4.0, 8.0))
    assert (
        unconfirmed_timeline.node_state_at(unconfirmed_view.fail_node, 5.5)[0]
        == "warning"
    )


def test_module_view_collapses_reciprocal_neighbor_to_one_node() -> None:
    from incidentlens.domain.models import InternalTrace
    from incidentlens.studio.cinema.dependencies import build_module_view

    trace = InternalTrace(
        service="svc",
        stages=[],
        failing_stage="client",
        failing_module="app.client",
        failing_callers=["app.coupled"],
        failing_callees=["app.coupled", "app.config"],
    )
    view = build_module_view(trace)
    assert view is not None
    modules = [meta.module for meta in view.meta.values()]
    assert modules.count("app.coupled") == 1
    coupled = next(
        meta for meta in view.meta.values() if meta.module == "app.coupled"
    )
    assert coupled.role == "coupled"
    assert len(view.graph.services) == 3


# --------------------------------------------------------------- symbol view


def test_symbol_view_places_failing_symbol_with_callers_and_callees() -> None:
    _, analysis = _analysis()
    from incidentlens.studio.cinema.symbols import build_symbol_view

    view = build_symbol_view(analysis.internal_trace)
    assert view is not None
    assert view.fail_node == "get_llm_for_tier"
    assert view.meta[view.fail_node].is_failing
    assert view.callers == ["_resolve_llm"]
    assert set(view.callees) == {"model_id_for_tier", "_build_for_model"}
    names = {s.name for s in view.graph.services}
    # caller upstream, the two callees downstream
    assert {"_resolve_llm", "get_llm_for_tier", "model_id_for_tier", "_build_for_model"} <= names
    # the failing symbol depends on (calls) its callees
    fail_svc = next(s for s in view.graph.services if s.name == "get_llm_for_tier")
    assert "model_id_for_tier" in fail_svc.depends_on
    # the caller points into the failing symbol
    caller_svc = next(s for s in view.graph.services if s.name == "_resolve_llm")
    assert caller_svc.depends_on == ["get_llm_for_tier"]


def test_symbol_view_absent_without_call_graph_context() -> None:
    from incidentlens.domain.models import InternalTrace
    from incidentlens.studio.cinema.symbols import build_symbol_view

    bare = InternalTrace(service="x", stages=[], failing_stage="s")
    assert build_symbol_view(bare) is None


# ----------------------------------------------------------------- dive act


def test_movie_scene_dives_and_surfaces() -> None:
    pytest.importorskip("numpy")
    pytest.importorskip("PIL")
    from incidentlens.studio.cinema import Timeline, build_movie_scene
    from incidentlens.studio.cinema.engine import RenderSpec

    architecture, analysis = _analysis()
    narration = build_narration(analysis, mode="template")
    timeline = Timeline(analysis, narration, [3.0] * len(narration.beats))
    movie = build_movie_scene(
        analysis, architecture, timeline, RenderSpec(640, 360, 12, 1.0)
    )
    assert movie.internal is not None
    window = timeline.internal_window()
    assert window is not None
    start, _fail, end = window
    assert movie.blend_at(start - 2.0) == 0.0
    assert movie.blend_at((start + end) / 2.0) == 1.0
    assert movie.blend_at(timeline.total - 0.5) == 0.0
    frame = movie.frame(start + 0.4)  # mid-crossfade renders both worlds
    assert frame.size == (640, 360)


def test_movie_scene_has_stage_module_and_symbol_dives() -> None:
    pytest.importorskip("numpy")
    pytest.importorskip("PIL")
    from incidentlens.studio.cinema import Timeline, build_movie_scene
    from incidentlens.studio.cinema.dependencies import ModuleScene
    from incidentlens.studio.cinema.engine import RenderSpec
    from incidentlens.studio.cinema.symbols import SymbolScene

    architecture, analysis = _analysis()
    narration = build_narration(analysis, mode="template")
    timeline = Timeline(analysis, narration, [3.0] * len(narration.beats))
    movie = build_movie_scene(
        analysis, architecture, timeline, RenderSpec(640, 360, 12, 1.0)
    )
    # Three levels: service stages, module dependencies, static function graph.
    assert len(movie.dives) == 3
    assert isinstance(movie.dives[1].scene, ModuleScene)
    assert isinstance(movie.dives[2].scene, SymbolScene)

    stage_win = timeline.internal_window()
    module_win = timeline.module_window()
    sym_win = timeline.symbol_window()
    assert stage_win is not None and module_win is not None and sym_win is not None
    # the dives are back-to-back and the hand-off never surfaces to macro
    assert abs(stage_win[2] - module_win[0]) < 1e-6
    assert abs(module_win[2] - sym_win[0]) < 1e-6
    assert movie.blend_at(stage_win[2]) > 0.95  # boundary stays fully dived
    module_frame = movie.frame((module_win[0] + module_win[2]) / 2.0)
    assert module_frame.size == (640, 360)
    frame = movie.frame((sym_win[0] + sym_win[2]) / 2.0 + 0.5)
    assert frame.size == (640, 360)
