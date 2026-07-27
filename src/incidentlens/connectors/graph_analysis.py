"""Structural analysis over a dependency graph: cycles and blast radius.

Large legacy architectures accumulate tightly-coupled clusters — modules that
(transitively) import each other. Those strongly-connected components are the
structural-flaw signal this module surfaces, alongside *blast radius*: how many
modules transitively depend on a given one (how far breakage spreads).

Uses :mod:`networkx` when it is installed (SCCs, simple cycles, ancestors);
falls back to a self-contained iterative Tarjan + BFS otherwise, so the core
tool has no hard third-party dependency.
"""

from __future__ import annotations

from collections import defaultdict, deque

try:  # optional, richer when present
    import networkx as nx  # type: ignore[import-untyped]

    _HAVE_NX = True
except ImportError:  # pragma: no cover - exercised in minimal installs
    _HAVE_NX = False


def _adjacency(
    nodes: list[str], edges: list[tuple[str, str]]
) -> dict[str, list[str]]:
    adj: dict[str, list[str]] = {n: [] for n in nodes}
    valid = set(nodes)
    for src, dst in edges:
        if src in valid and dst in valid and dst not in adj[src]:
            adj[src].append(dst)
    return adj


def _tarjan_sccs(
    nodes: list[str], adj: dict[str, list[str]]
) -> list[list[str]]:
    """Iterative Tarjan (no recursion-depth ceiling on big graphs)."""
    index_of: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    counter = 0
    result: list[list[str]] = []

    for root in nodes:
        if root in index_of:
            continue
        work: list[tuple[str, int]] = [(root, 0)]
        while work:
            node, pos = work[-1]
            if pos == 0:
                index_of[node] = low[node] = counter
                counter += 1
                stack.append(node)
                on_stack.add(node)
            recursed = False
            neighbours = adj.get(node, [])
            for i in range(pos, len(neighbours)):
                nxt = neighbours[i]
                if nxt not in index_of:
                    work[-1] = (node, i + 1)
                    work.append((nxt, 0))
                    recursed = True
                    break
                if nxt in on_stack:
                    low[node] = min(low[node], index_of[nxt])
            if recursed:
                continue
            if low[node] == index_of[node]:
                component: list[str] = []
                while True:
                    w = stack.pop()
                    on_stack.discard(w)
                    component.append(w)
                    if w == node:
                        break
                result.append(component)
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
    return result


def find_cycles(
    nodes: list[str], edges: list[tuple[str, str]]
) -> list[list[str]]:
    """Coupled clusters: SCCs with more than one member, plus self-loops.

    Returned largest-first; each inner list is one cluster.
    """
    adj = _adjacency(nodes, edges)
    self_loops = [s for s, d in edges if s == d and s in adj]

    if _HAVE_NX:
        g = nx.DiGraph()
        g.add_nodes_from(nodes)
        g.add_edges_from((s, d) for s, d in edges if s != d)
        comps = [list(c) for c in nx.strongly_connected_components(g) if len(c) > 1]
    else:
        comps = [c for c in _tarjan_sccs(nodes, adj) if len(c) > 1]

    clusters = [sorted(c) for c in comps]
    clusters.extend([s] for s in self_loops)
    clusters.sort(key=len, reverse=True)
    return clusters


def blast_radii(
    nodes: list[str], edges: list[tuple[str, str]]
) -> dict[str, int]:
    """For each node, how many nodes transitively depend on it.

    ``edges`` are (user, used) — an edge ``a -> b`` means a depends on b — so we
    count ancestors of each node in that graph.
    """
    if not nodes:
        return {}
    if _HAVE_NX:
        g = nx.DiGraph()
        g.add_nodes_from(nodes)
        g.add_edges_from((s, d) for s, d in edges if s != d)
        return {n: len(nx.ancestors(g, n)) for n in nodes}

    reverse: dict[str, list[str]] = defaultdict(list)
    valid = set(nodes)
    for src, dst in edges:
        if src != dst and src in valid and dst in valid:
            reverse[dst].append(src)

    radii: dict[str, int] = {}
    for node in nodes:
        seen: set[str] = set()
        queue = deque(reverse.get(node, []))
        while queue:
            cur = queue.popleft()
            if cur in seen:
                continue
            seen.add(cur)
            queue.extend(reverse.get(cur, []))
        radii[node] = len(seen)
    return radii


def degrees(
    nodes: list[str], edges: list[tuple[str, str]]
) -> tuple[dict[str, int], dict[str, int]]:
    """(fan_out, fan_in) per node — distinct out-neighbours and in-neighbours."""
    valid = set(nodes)
    out: dict[str, set[str]] = {n: set() for n in nodes}
    inc: dict[str, set[str]] = {n: set() for n in nodes}
    for src, dst in edges:
        if src != dst and src in valid and dst in valid:
            out[src].add(dst)
            inc[dst].add(src)
    return ({n: len(v) for n, v in out.items()}, {n: len(v) for n, v in inc.items()})
