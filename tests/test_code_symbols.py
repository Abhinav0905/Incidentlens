"""Fine-grained call graph: symbol resolution, dynamic imports, cycles, enrichment."""

from __future__ import annotations

import textwrap
import types
from datetime import datetime, timezone

from incidentlens.connectors.code_graph import build_code_graph, enrich_trace_with_code
from incidentlens.domain.models import (
    CodeGraph,
    CodeModule,
    InternalStageTrace,
    InternalTrace,
    SourceType,
    StageStatus,
    TelemetryEvent,
)


def _service(tmp_path):
    svc = tmp_path / "svc"
    (svc / "app").mkdir(parents=True)
    (svc / "app" / "__init__.py").write_text("")
    (svc / "app" / "pii.py").write_text("def scan(text):\n    return text\n")
    (svc / "app" / "guardrail.py").write_text(
        "from app.pii import scan\n\ndef check(q):\n    return scan(q)\n"
    )
    (svc / "app" / "rewriter.py").write_text(textwrap.dedent("""
        from app import llm_client
        def rewrite(q):
            return llm_client.get_llm().invoke(q)
    """))
    (svc / "app" / "llm_client.py").write_text(textwrap.dedent("""
        import config.settings as settings
        import importlib
        def get_llm():
            mod = importlib.import_module("app.pii")
            return settings.base_url
        class Client:
            def call(self):
                return get_llm()
    """))
    (svc / "config").mkdir()
    (svc / "config" / "__init__.py").write_text("")
    (svc / "config" / "settings.py").write_text("base_url = 'x'\n")
    return svc


def test_symbol_edges_resolve_across_import_styles(tmp_path) -> None:
    g = build_code_graph(_service(tmp_path), "svc")
    pairs = {(e.src, e.dst, e.kind) for e in g.symbol_edges}
    # from x import symbol
    assert ("app.guardrail.check", "app.pii.scan", "call") in pairs
    # module-alias call
    assert ("app.rewriter.rewrite", "app.llm_client.get_llm", "call") in pairs
    # same-module bare call from inside a method
    assert ("app.llm_client.Client.call", "app.llm_client.get_llm", "call") in pairs


def test_dynamic_import_is_detected(tmp_path) -> None:
    g = build_code_graph(_service(tmp_path), "svc")
    dyn = {(e.src, e.dst) for e in g.symbol_edges if e.kind == "dynamic"}
    assert ("app.llm_client.get_llm", "app.pii") in dyn


def test_symbol_roles_and_kinds(tmp_path) -> None:
    g = build_code_graph(_service(tmp_path), "svc")
    by_qual = {s.qualname: s for s in g.symbols}
    assert by_qual["app.llm_client.Client"].kind == "class"
    assert by_qual["app.llm_client.Client.call"].kind == "method"
    # a client-ish module colours its symbols "client"
    assert by_qual["app.llm_client.get_llm"].role == "client"


def test_cycle_detection(tmp_path) -> None:
    svc = tmp_path / "svc"
    (svc / "pkg").mkdir(parents=True)
    (svc / "pkg" / "__init__.py").write_text("")
    (svc / "pkg" / "a.py").write_text("from pkg import b\n\ndef fa():\n    return b.fb()\n")
    (svc / "pkg" / "b.py").write_text("from pkg import a\n\ndef fb():\n    return a.fa()\n")
    (svc / "pkg" / "c.py").write_text("def fc():\n    return 1\n")
    g = build_code_graph(svc, "svc")
    coupled = {frozenset(c) for c in g.cycles}
    assert frozenset({"pkg.a", "pkg.b"}) in coupled
    assert all(m.in_cycle for m in g.modules if m.name in ("pkg.a", "pkg.b"))
    assert not next(m for m in g.modules if m.name == "pkg.c").in_cycle


def test_blast_radius_counts_transitive_dependents(tmp_path) -> None:
    g = build_code_graph(_service(tmp_path), "svc")
    settings = next(m for m in g.modules if m.name == "config.settings")
    # llm_client uses settings, rewriter uses llm_client -> settings has >=2 dependents
    assert settings.blast_radius >= 2


def test_enrichment_names_the_failing_symbol(tmp_path) -> None:
    g = build_code_graph(_service(tmp_path), "svc")
    for m in g.modules:  # attribute the client module to a pipeline stage
        if m.name == "app.llm_client":
            m.stage = "llm"
    trace = InternalTrace(service="svc", stages=[], failing_stage="llm")
    analysis = types.SimpleNamespace(internal_trace=trace)

    enrich_trace_with_code(analysis, {"svc": g})

    assert trace.failing_module == "app.llm_client"
    assert trace.failing_symbol == "app.llm_client.get_llm"  # externally called
    assert "app.rewriter.rewrite" in trace.failing_symbol_callers


def test_enrichment_prefers_linked_error_logger_over_largest_module() -> None:
    trace = InternalTrace(
        service="svc",
        stages=[
            InternalStageTrace(
                stage="llm",
                status=StageStatus.FAILED,
                detail="model call failed",
                evidence_ids=["failure"],
            )
        ],
        failing_stage="llm",
    )
    analysis = types.SimpleNamespace(
        internal_trace=trace,
        evidence=[
            TelemetryEvent(
                id="failure",
                source_type=SourceType.LOG,
                source="svc",
                timestamp=datetime(2026, 7, 24, tzinfo=timezone.utc),
                detail="model call failed",
                attributes={"logger": "app.exact_client", "level": "ERROR"},
            )
        ],
    )
    graph = CodeGraph(
        service="svc",
        modules=[
            CodeModule(name="app.large_helper", stage="llm", loc=900),
            CodeModule(name="app.exact_client", stage="llm", loc=20),
        ],
    )

    enrich_trace_with_code(analysis, {"svc": graph})

    assert trace.failing_module == "app.exact_client"
