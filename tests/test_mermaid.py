"""Mermaid rendering + multi-model narrator provider resolution."""

from __future__ import annotations

import re
import textwrap

from incidentlens.connectors.code_graph import build_code_graph
from incidentlens.studio.mermaid import (
    render_module_mermaid,
    render_symbol_mermaid,
    write_mermaid,
)
from incidentlens.studio.narration import _offending_value, resolve_provider


def _service(tmp_path):
    svc = tmp_path / "svc"
    (svc / "app").mkdir(parents=True)
    (svc / "app" / "__init__.py").write_text("")
    (svc / "app" / "pii.py").write_text("def scan(text):\n    return text\n")
    (svc / "app" / "guardrail.py").write_text(
        "from app.pii import scan\n\n\n"
        "class Guard:\n    def check(self, q):\n        return scan(q)\n"
    )
    (svc / "app" / "llm_client.py").write_text(textwrap.dedent("""
        from app import guardrail
        def get_llm():
            return guardrail.Guard().check('x')
    """))
    return svc


def _balanced_subgraphs(src: str) -> bool:
    opens = sum(1 for line in src.splitlines() if line.strip().startswith("subgraph"))
    ends = sum(1 for line in src.splitlines() if line.strip() == "end")
    return opens == ends and opens > 0


def test_module_mermaid_is_valid_flowchart(tmp_path) -> None:
    g = build_code_graph(_service(tmp_path), "svc")
    src = render_module_mermaid(g)
    assert src.startswith("flowchart")
    assert "classDef" in src
    assert _balanced_subgraphs(src)
    assert "-->" in src  # at least one edge
    # class ids must be hyphen-free (graph-node -> graph_node)
    assert "classDef graph-node" not in src


def test_symbol_mermaid_nests_methods_in_classes(tmp_path) -> None:
    g = build_code_graph(_service(tmp_path), "svc")
    src = render_symbol_mermaid(g, focus="app.guardrail")
    assert src.startswith("flowchart")
    assert _balanced_subgraphs(src)
    assert "class Guard" in src  # a class subgraph
    assert "check()" in src  # a method node inside it


def test_symbol_mermaid_declares_every_edge_endpoint(tmp_path) -> None:
    g = build_code_graph(_service(tmp_path), "svc")
    src = render_symbol_mermaid(g, focus="app.guardrail")

    node_ids: set[str] = set()
    edge_endpoints: set[str] = set()
    for line in src.splitlines():
        if node := re.match(r'\s*(n\d+)\["', line):
            node_ids.add(node.group(1))
        if edge := re.match(r"\s*(n\d+)\s+(?:-->|==>|-\.->)\s+(n\d+)\s*$", line):
            edge_endpoints.update(edge.groups())

    assert "==>" in src  # get_llm constructs Guard, whose methods are also visible
    assert edge_endpoints <= node_ids


def test_write_mermaid_emits_mmd_and_html(tmp_path) -> None:
    g = build_code_graph(_service(tmp_path), "svc")
    mmd, html = write_mermaid(g, tmp_path / "g.mmd", level="symbol", focus="app.guardrail")
    assert mmd.exists() and mmd.read_text().startswith("flowchart")
    assert html is not None and html.exists()
    assert "mermaid" in html.read_text().lower()


def test_provider_resolution() -> None:
    assert resolve_provider("claude-opus-4-8") == "anthropic"
    assert resolve_provider("claude-sonnet-5") == "anthropic"
    assert resolve_provider("gpt-5.1") == "openai"
    assert resolve_provider("gpt-5.1", "anthropic") == "anthropic"  # explicit wins


def test_offending_value_extraction() -> None:
    # the model id must be picked even though a short quote ('fast') precedes it
    detail = (
        "ModelProvider InvokeModel failed for tier 'fast': ValidationException - "
        "invalid model identifier: 'us.modelhst.fast-tier-4-5-20251001-v1:0'."
    )
    assert _offending_value(detail) == "us.modelhst.fast-tier-4-5-20251001-v1:0"
    # ordinary prose with no identifier yields nothing
    assert _offending_value("the gateway returned 401 unauthorized on both schemes") is None
    assert _offending_value("retry 'again' later") is None  # too short / no separator
