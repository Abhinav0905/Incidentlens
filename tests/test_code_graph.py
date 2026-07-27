"""Deep code graph: extraction, who-calls answers, enrichment, HTML export."""

from __future__ import annotations

import textwrap

from incidentlens.connectors.code_graph import (
    build_code_graph,
    enrich_trace_with_code,
    load_code_graphs,
    save_code_graphs,
)
from incidentlens.connectors.synthetic import SyntheticConnector
from incidentlens.engines.deterministic import DeterministicAnalysisEngine
from incidentlens.services.incidents import IncidentService
from incidentlens.studio.graphview import incident_overlay, render_code_graph_html
from incidentlens.studio.mermaid import render_symbol_mermaid


def _fixture_service(tmp_path):
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

        def get_llm():
            return settings.base_url
    """))
    (svc / "config").mkdir()
    (svc / "config" / "__init__.py").write_text("")
    (svc / "config" / "settings.py").write_text("base_url = 'x'\n")
    return svc


def test_graph_answers_who_calls_what(tmp_path) -> None:
    graph = build_code_graph(_fixture_service(tmp_path), "svc")
    assert graph is not None

    pii_callers = graph.callers_of("app.pii")
    assert pii_callers and pii_callers[0][0] == "app.guardrail"
    assert "scan" in pii_callers[0][1]

    llm_callers = [src for src, _ in graph.callers_of("app.llm_client")]
    assert "app.rewriter" in llm_callers
    llm_callees = {dst for dst, _ in graph.callees_of("app.llm_client")}
    assert "config.settings" in llm_callees

    # attribute *reads* through an alias carry symbols too
    settings_edge = next(
        e for e in graph.edges
        if e.src == "app.llm_client" and e.dst == "config.settings" and e.kind == "call"
    )
    assert "base_url" in settings_edge.symbols


def test_graphs_round_trip_through_json(tmp_path) -> None:
    graph = build_code_graph(_fixture_service(tmp_path), "svc")
    path = tmp_path / "codegraph.json"
    save_code_graphs({"svc": graph}, path)
    loaded = load_code_graphs(path)
    assert set(loaded) == {"svc"}
    assert {m.name for m in loaded["svc"].modules} == {m.name for m in graph.modules}


def _enriched_analysis():
    connector = SyntheticConnector("gateway-auth-rejection")
    analysis = IncidentService(
        connector=connector, engine=DeterministicAnalysisEngine()
    ).analyze()
    graphs = connector.fetch_code_graphs()
    enrich_trace_with_code(analysis, graphs)
    return analysis, graphs


def test_trace_enrichment_names_the_failing_module_and_callers() -> None:
    analysis, _ = _enriched_analysis()
    trace = analysis.internal_trace
    assert trace.failing_module == "hary.models.llm_factory"
    assert "hary.graph.nodes.agent" in trace.failing_callers
    assert "config.settings" in trace.failing_callees


def test_incident_overlay_marks_path_and_failure() -> None:
    analysis, graphs = _enriched_analysis()
    overlay = incident_overlay(analysis, graphs)
    assert overlay is not None
    assert overlay["failing_module"] == "hary.models.llm_factory"
    assert overlay["module_failure_confirmed"]
    assert "hary.guardrails.pii" in overlay["path_modules"]  # pii-scanner stage
    assert "hary.graph.nodes.router" not in overlay["path_modules"]  # dormant branch


def test_html_export_is_self_contained(tmp_path) -> None:
    analysis, graphs = _enriched_analysis()
    out = render_code_graph_html(graphs, tmp_path / "g.html", analysis=analysis)
    html = out.read_text(encoding="utf-8")
    assert "__DATA__" not in html  # data inlined
    assert "hary.models.llm_factory" in html
    assert "http://" not in html.split("</style>")[1]  # no CDN, no network
    assert "incident" in html
    assert "candidate function" in html
    assert "static candidate" in html


def test_symbol_mermaid_marks_static_locus_as_candidate_not_failure() -> None:
    analysis, graphs = _enriched_analysis()
    graph = graphs["hary-ai"]
    source = render_symbol_mermaid(graph, analysis=analysis)
    symbol = analysis.internal_trace.failing_symbol.rsplit(".", 1)[-1]
    node_line = next(line for line in source.splitlines() if f'"{symbol}()"' in line)
    node_id = node_line.strip().split("[", 1)[0]
    assert f"class {node_id} candidate;" in source
    assert f"class {node_id} failing;" not in source
