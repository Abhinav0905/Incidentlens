"""Derive an architecture graph straight from a code repository.

``incidentlens discover /path/to/repo`` walks the checkout and proposes the
service graph the videos will animate:

* **docker-compose files** — every entry under ``services:`` becomes a node;
  ``depends_on`` becomes edges.
* **service directories** — a top-level directory carrying a build manifest
  (``package.json``, ``pyproject.toml``, ``requirements.txt``, ``pom.xml``,
  ``build.gradle``, ``go.mod``) is a service. Nested one level is also checked
  (``myapp/myapp_microservice`` style layouts).
* **cross-references** — mentions of one service's name inside another's
  config/env files become dependency edges.
* **external gateways** — ``http(s)://`` URLs found in config files (settings,
  .env, yaml, properties) become external nodes, e.g. a shared LLM gateway.
  The service whose config carries the URL depends on it.

The result is written to ``incidentlens.arch.json`` (editable — the scan is a
proposal, not a verdict) plus an ``incidentlens.config.json`` with commented
log-source stubs for the watch command.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from incidentlens.domain.models import ArchitectureGraph, ServiceNode

MANIFESTS = (
    "package.json",
    "pyproject.toml",
    "requirements.txt",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "go.mod",
    "Cargo.toml",
)

CONFIG_GLOBS = (
    "*.env", ".env", ".env.*", "*.yaml", "*.yml", "*.properties", "*.toml",
    "settings.py", "config.py", "application*.properties", "application*.yml",
    "*.json",
)

FRONTEND_HINTS = ("frontend", "front-end", "webapp", "web-app", "ui", "www", "client")
SKIP_DIRS = {
    ".git", ".hg", "node_modules", ".venv", "venv", "__pycache__", "target",
    "dist", "build", ".next", ".idea", ".vscode", "eval_reports", ".pytest_cache",
    "logs", "coverage",
}

_URL = re.compile(r"https?://([a-zA-Z0-9.\-]+)")
_COMPOSE_SERVICE = re.compile(r"^  ([A-Za-z0-9_.\-]+):\s*$")
_COMPOSE_DEP = re.compile(r"^\s+-\s+([A-Za-z0-9_.\-]+)\s*$")

# Hostnames that are package registries / clouds, not part of the caller's system.
IGNORED_HOSTS = (
    "localhost", "127.0.0.1", "0.0.0.0",
    "github.com", "gitlab.com", "bitbucket.org", "npmjs.org", "registry.npmjs.org",
    "pypi.org", "files.pythonhosted.org", "maven.apache.org", "repo.maven.apache.org",
    "docker.io", "docker.com", "schema.org", "www.w3.org", "json-schema.org",
    "example.com", "sentry.io", "googleapis.com", "internal.com",
    "openai.com", "api.openai.com", "anthropic.com", "api.anthropic.com",
    "elevenlabs.io", "backblazeb2.com",
)


def _norm(name: str) -> str:
    return name.strip().lower().replace("_", "-").replace(" ", "-")


def _is_frontend(name: str, directory: Path | None) -> bool:
    n = _norm(name)
    if any(h in n for h in FRONTEND_HINTS):
        return True
    if directory is not None:
        pkg = directory / "package.json"
        if pkg.is_file():
            try:
                data = json.loads(pkg.read_text(encoding="utf-8", errors="replace"))
            except (json.JSONDecodeError, OSError):
                return False
            deps = {**data.get("dependencies", {}), **data.get("devDependencies", {})}
            return any(k in deps for k in ("react", "vue", "next", "@angular/core", "svelte"))
    return False


def _service_dirs(root: Path) -> dict[str, Path]:
    """Top-level (and one-deep) directories that look like deployable services."""
    found: dict[str, Path] = {}
    for entry in sorted(root.iterdir()):
        if not entry.is_dir() or entry.name.startswith(".") or entry.name in SKIP_DIRS:
            continue
        if any((entry / m).is_file() for m in MANIFESTS):
            found[_norm(entry.name)] = entry
            continue
        # myapp/myapp_microservice style: manifest one level down
        for sub in sorted(p for p in entry.iterdir() if p.is_dir()):
            if sub.name.startswith(".") or sub.name in SKIP_DIRS:
                continue
            if any((sub / m).is_file() for m in MANIFESTS):
                found[_norm(entry.name)] = sub
                break
    return found


def _compose_services(root: Path) -> dict[str, list[str]]:
    """service -> depends_on from any docker-compose file (tolerant parser)."""
    services: dict[str, list[str]] = {}
    for compose in list(root.glob("docker-compose*.y*ml")) + list(root.glob("compose*.y*ml")):
        try:
            lines = compose.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        in_services = False
        current: str | None = None
        in_depends = False
        for line in lines:
            if re.match(r"^services:\s*$", line):
                in_services = True
                continue
            if in_services and re.match(r"^[A-Za-z0-9_#\-]+:", line):  # next top-level key
                in_services = False
            if not in_services:
                continue
            m = _COMPOSE_SERVICE.match(line)
            if m:
                current = _norm(m.group(1))
                services.setdefault(current, [])
                in_depends = False
                continue
            if current and re.match(r"^\s{4,}depends_on:\s*$", line):
                in_depends = True
                continue
            if current and in_depends:
                dep = _COMPOSE_DEP.match(line)
                if dep:
                    services[current].append(_norm(dep.group(1)))
                elif line.strip() and not line.startswith(" " * 6):
                    in_depends = False
    return services


def _config_text(directory: Path, budget: int = 40) -> str:
    """Concatenated config-ish file contents for one service (bounded)."""
    chunks: list[str] = []
    seen = 0
    for pattern in CONFIG_GLOBS:
        for path in directory.rglob(pattern):
            if seen >= budget:
                return "\n".join(chunks)
            if any(part in SKIP_DIRS or part.startswith(".git") for part in path.parts):
                continue
            try:
                if path.stat().st_size > 300_000:
                    continue
                chunks.append(path.read_text(encoding="utf-8", errors="replace"))
                seen += 1
            except OSError:
                continue
    return "\n".join(chunks)


def _external_name(host: str) -> str:
    label = host.split(".")[0]
    return _norm(label) + "-gateway" if not label.endswith("gateway") else _norm(label)


def discover_architecture(root: str | Path, system: str | None = None) -> ArchitectureGraph:
    root = Path(root).resolve()
    system = system or _norm(root.name) or "discovered-system"

    dirs = _service_dirs(root)
    compose = _compose_services(root)
    names = sorted(set(dirs) | set(compose))
    if not names:
        raise ValueError(
            f"no services found under {root} — expected service directories with a "
            "build manifest, or a docker-compose file"
        )

    depends: dict[str, set[str]] = {name: set(compose.get(name, [])) for name in names}
    externals: dict[str, str] = {}  # node name -> host

    for name in names:
        directory = dirs.get(name)
        if directory is None:
            continue
        text = _config_text(directory)
        lowered = text.lower()
        for other in names:
            if other == name:
                continue
            # a service that names another in its config likely calls it
            if re.search(rf"[^a-z0-9]{re.escape(other)}[^a-z0-9]", lowered):
                depends[name].add(other)
        for host in set(_URL.findall(text)):
            h = host.lower()
            if any(h == ig or h.endswith("." + ig) for ig in IGNORED_HOSTS):
                continue
            node = _external_name(h)
            externals[node] = h
            depends[name].add(node)

    # frontends call BFFs/backends, not the other way round: drop reverse edges
    for name in names:
        if _is_frontend(name, dirs.get(name)):
            for other in names:
                if other != name:
                    depends[other].discard(name)

    services: list[ServiceNode] = []
    for name in names:
        internals = None
        directory = dirs.get(name)
        if directory is not None:
            from incidentlens.connectors.internals_scan import scan_service_internals

            internals = scan_service_internals(directory)
        services.append(
            ServiceNode(
                name=name,
                owner=None,
                depends_on=sorted(depends[name]),
                user_facing=_is_frontend(name, dirs.get(name)),
                internals=internals,
            )
        )
    for node, host in sorted(externals.items()):
        services.append(
            ServiceNode(name=node, owner=f"external · {host}", depends_on=[], user_facing=False)
        )
    return ArchitectureGraph(system=system, services=services)


def write_discovery(
    root: str | Path,
    *,
    arch_file: str = "incidentlens.arch.json",
    config_file: str = "incidentlens.config.json",
    codegraph_file: str = "incidentlens.codegraph.json",
    system: str | None = None,
) -> tuple[Path, Path]:
    """Scan ``root`` and write the architecture + a starter watch config,
    plus the deep code graphs (module network per Python service)."""
    root = Path(root).resolve()
    graph = discover_architecture(root, system=system)

    arch_path = root / arch_file
    arch_path.write_text(
        json.dumps(graph.model_dump(mode="json"), indent=2) + "\n", encoding="utf-8"
    )

    # deep scan: the code network behind each scannable service
    from incidentlens.connectors.code_graph import build_code_graph, save_code_graphs

    dirs = _service_dirs(root)
    code_graphs = {}
    for svc in graph.services:
        directory = dirs.get(svc.name)
        if directory is None:
            continue
        deep = build_code_graph(directory, svc.name, svc.internals)
        if deep is not None:
            code_graphs[svc.name] = deep
    if code_graphs:
        save_code_graphs(code_graphs, root / codegraph_file)

    config = {
        "_comment": (
            "IncidentLens watch config. Point each service at the file its logs go "
            "to (redirect stdout, e.g. `python main.py 2>&1 | tee logs/api.log`). "
            "Globs are allowed. Remove entries you don't run."
        ),
        "architecture": arch_file,
        "logs": [
            {"service": s.name, "path": f"logs/{s.name}.log"}
            for s in graph.services
            if not (s.owner or "").startswith("external")
        ],
        "watch": {
            "error_threshold": 3,
            "window_seconds": 90,
            "cooldown_seconds": 300,
            "out_dir": "incidentlens-videos",
        },
    }
    config_path = root / config_file
    config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    return arch_path, config_path
