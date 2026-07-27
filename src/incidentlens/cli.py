from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def _load_local_env(path: Path = Path(".env")) -> None:
    """Load ``KEY=VALUE`` lines from a local ``.env`` into the process
    environment so narration keys/settings can live in a file.

    Real environment variables always win (a set var is never overwritten).
    Recognises ``export KEY=…`` and quoted values. This is what lets
    ``ANTHROPIC_API_KEY``, ``INCIDENTLENS_NARRATION_MODEL``,
    ``INCIDENTLENS_PIPER_MODEL`` and friends be read from ``incidentlens/.env``.
    """
    if not path.is_file():
        return
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _add_video_flags(parser: argparse.ArgumentParser, *, voice_default: str) -> None:
    parser.add_argument(
        "--voice",
        default=voice_default,
        choices=[
            "auto",
            "genblaze",
            "openai",
            "offline",
            "piper",
            "elevenlabs",
            "silent",
        ],
        help="Text-to-speech voice. genblaze=Genblaze-orchestrated OpenAI narration; "
        "openai=direct calm neural narration; "
        "offline=espeak (robotic, no setup); piper=open-source neural; "
        "auto=direct OpenAI when configured, then local fallbacks",
    )
    parser.add_argument(
        "--narration",
        default="template",
        choices=["template", "llm"],
        help="Narration source: deterministic template or model-written",
    )
    parser.add_argument(
        "--narration-model",
        default=None,
        help="Model id for --narration llm (e.g. claude-opus-4-8, claude-sonnet-5, "
        "gpt-5.1). Default: $INCIDENTLENS_NARRATION_MODEL or claude-sonnet-5",
    )
    parser.add_argument(
        "--narration-provider",
        default=None,
        choices=["anthropic", "openai"],
        help="Force the narration provider. Default: inferred from the model id "
        "(claude-* -> anthropic, else openai). openai uses $INCIDENTLENS_OPENAI_BASE_URL "
        "when set, so a Gateway/ModelProvider gateway works here.",
    )
    parser.add_argument(
        "--style",
        default="cinematic",
        choices=["cinematic", "classic"],
        help="cinematic = continuous 3D camera replay; classic = per-beat stills",
    )
    parser.add_argument(
        "--profile",
        default="high",
        choices=["high", "preview", "ultra"],
        help="Render profile: high 1080p30, preview 720p24 (fast), ultra 1440p30",
    )
    parser.add_argument("--fps", type=int, default=None, help="Override the profile fps")
    parser.add_argument(
        "--intro-video",
        default=None,
        metavar="PATH",
        help="Optional short cinematic bumper to prepend (for example a Sora clip). "
        "Technical diagrams remain deterministic.",
    )
    parser.add_argument(
        "--publish-genblaze",
        action="store_true",
        help="Create a hash-verified Genblaze provenance sidecar for the final MP4",
    )
    parser.add_argument(
        "--upload-b2",
        action="store_true",
        help="Legacy direct upload to Backblaze B2",
    )
    parser.add_argument(
        "--upload-genblaze-b2",
        action="store_true",
        help="Publish the video and canonical manifest to B2 through Genblaze",
    )


def _progress_printer(label: str):
    def _print(p: float) -> None:
        sys.stderr.write(f"\r{label} {p * 100:5.1f}%")
        sys.stderr.flush()
        if p >= 1.0:
            sys.stderr.write("\n")

    return _print


def _log_sources(args, root: Path):
    from incidentlens.connectors.logfile import LogSource

    sources = []
    for spec in args.logs or []:
        if "=" in spec:
            service, pattern = spec.split("=", 1)
        else:
            service, pattern = Path(spec).stem, spec
        sources.append(LogSource(service=service.strip(), pattern=pattern.strip(), root=root))
    return sources


def _load_architecture(path: Path):
    import json

    from incidentlens.domain.models import ArchitectureGraph

    return ArchitectureGraph.model_validate(json.loads(path.read_text(encoding="utf-8")))


def main() -> None:
    _load_local_env()  # pick up ./.env (narration keys/model, voice, gateway url)
    parser = argparse.ArgumentParser(prog="incidentlens")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # ------------------------------------------------------------------ serve
    from incidentlens.config import settings

    serve = subparsers.add_parser("serve", help="Start the IncidentLens API and web app")
    serve.add_argument("--host", default=settings.host)
    serve.add_argument("--port", type=int, default=settings.port)
    serve.add_argument("--reload", action="store_true")

    # ----------------------------------------------------------------- studio
    studio = subparsers.add_parser(
        "studio", help="Render a bundled scenario as a narrated video"
    )
    studio.add_argument("scenario", help="Scenario name, e.g. gateway-auth-rejection")
    studio.add_argument("--out", default="incident.mp4", help="Output MP4 path")
    _add_video_flags(studio, voice_default="auto")
    # --------------------------------------------------------------- discover
    discover = subparsers.add_parser(
        "discover",
        help="Scan a repository, derive the service graph, write starter config",
    )
    discover.add_argument("path", nargs="?", default=".", help="Repository root")
    discover.add_argument("--system", default=None, help="System name for the graph")

    # ------------------------------------------------------------------ graph
    graph_cmd = subparsers.add_parser(
        "graph",
        help="Interactive HTML network of the codebase: who calls what, per service",
    )
    graph_cmd.add_argument("path", nargs="?", default=".", help="Repository root")
    graph_cmd.add_argument("--out", default="code-graph.html")
    graph_cmd.add_argument("--service", default=None,
                           help="Only this service (default: every scannable one)")
    graph_cmd.add_argument("--analysis", default=None,
                           help="An .analysis.json to overlay the incident path")
    graph_cmd.add_argument("--mermaid", action="store_true",
                           help="Also emit a Mermaid diagram (.mmd + viewer .html)")
    graph_cmd.add_argument("--level", default="module", choices=["module", "symbol"],
                           help="Mermaid granularity: module network, or "
                           "methods-in-classes-in-modules (symbol)")
    graph_cmd.add_argument("--focus", default=None,
                           help="Symbol level: center on this module or "
                           "module.Class.method and its callers/callees")

    # ---------------------------------------------------------------- analyze
    analyze = subparsers.add_parser(
        "analyze", help="One-shot: read log files, reconstruct, render the video"
    )
    analyze.add_argument(
        "--logs",
        action="append",
        metavar="SERVICE=PATH",
        help="Log source, e.g. api=logs/api.log (repeatable; glob ok). "
        "Bare paths use the file stem as the service name.",
    )
    analyze.add_argument("--arch", default="incidentlens.arch.json",
                         help="Architecture JSON (from `incidentlens discover`)")
    analyze.add_argument("--config", default=None,
                         help="incidentlens.config.json (overrides --logs/--arch)")
    analyze.add_argument("--out-dir", default="incidentlens-videos")
    analyze.add_argument(
        "--analysis-only",
        action="store_true",
        help="Reconstruct and write the briefing, analysis JSON and diagrams, but "
        "skip the video. Completes in seconds instead of minutes — use this when "
        "you want the answer rather than the film.",
    )
    _add_video_flags(analyze, voice_default="auto")

    # ------------------------------------------------------------------ watch
    watch = subparsers.add_parser(
        "watch", help="Tail logs continuously; render a video when failures burst"
    )
    watch.add_argument("--logs", action="append", metavar="SERVICE=PATH",
                       help="Log source (repeatable), e.g. api=logs/api.log")
    watch.add_argument("--arch", default="incidentlens.arch.json")
    watch.add_argument("--config", default=None,
                       help="incidentlens.config.json (from `incidentlens discover`)")
    watch.add_argument("--out-dir", default="incidentlens-videos")
    watch.add_argument("--threshold", type=int, default=3,
                       help="Error-level lines within the window that trigger a video")
    watch.add_argument("--window", type=float, default=90.0, help="Window seconds")
    watch.add_argument("--cooldown", type=float, default=300.0,
                       help="Seconds to wait before the next video can trigger")
    _add_video_flags(watch, voice_default="auto")

    args = parser.parse_args()
    if getattr(args, "upload_b2", False) and getattr(
        args, "upload_genblaze_b2", False
    ):
        parser.error("--upload-b2 and --upload-genblaze-b2 are mutually exclusive")

    if args.command == "serve":
        import uvicorn

        uvicorn.run(
            "incidentlens.api:app", host=args.host, port=args.port, reload=args.reload
        )

    elif args.command == "studio":
        from incidentlens.studio.narration import DEFAULT_MODEL
        from incidentlens.studio.pipeline import produce_incident_video

        result = produce_incident_video(
            args.scenario,
            args.out,
            voice=args.voice,
            narration_mode=args.narration,
            model=args.narration_model or DEFAULT_MODEL,
            provider=args.narration_provider,
            style=args.style,
            profile=args.profile,
            fps=args.fps,
            intro_video=args.intro_video,
            publish_genblaze=args.publish_genblaze,
            upload=args.upload_b2,
            upload_genblaze_b2=args.upload_genblaze_b2,
            progress=_progress_printer("rendering"),
        )
        print(f"Wrote {result.path} ({result.beats} beats, {result.incident_id})")
        if result.url:
            print(f"Uploaded: {result.url}")
        if result.manifest_path:
            print(f"Manifest: {result.manifest_path}")
            print(f"Manifest hash: {result.manifest_hash}")
        if result.manifest_uri:
            print(f"Manifest URI: {result.manifest_uri}")

    elif args.command == "graph":
        import json as _json

        from incidentlens.connectors.code_graph import build_code_graph
        from incidentlens.connectors.discovery import _service_dirs, discover_architecture
        from incidentlens.studio.graphview import render_code_graph_html

        root = Path(args.path).resolve()
        architecture = discover_architecture(root)
        dirs = _service_dirs(root)
        graphs = {}
        for svc in architecture.services:
            if args.service and svc.name != args.service:
                continue
            directory = dirs.get(svc.name)
            if directory is None:
                continue
            deep = build_code_graph(directory, svc.name, svc.internals)
            if deep is not None:
                graphs[svc.name] = deep
        if not graphs:
            sys.exit("no scannable (Python) services found to graph")
        analysis_obj = None
        if args.analysis:
            from incidentlens.domain.models import IncidentAnalysis

            analysis_obj = IncidentAnalysis.model_validate(
                _json.loads(Path(args.analysis).read_text(encoding="utf-8"))
            )
        out = render_code_graph_html(
            graphs, args.out, analysis=analysis_obj,
            subtitle=f"{architecture.system} · static analysis, nothing executed",
        )
        total_modules = sum(len(g.modules) for g in graphs.values())
        total_edges = sum(len(g.edges) for g in graphs.values())
        total_symbols = sum(len(g.symbols) for g in graphs.values())
        total_calls = sum(len(g.symbol_edges) for g in graphs.values())
        total_cycles = sum(len(g.cycles) for g in graphs.values())
        print(f"Wrote {out} ({len(graphs)} service(s), {total_modules} modules, "
              f"{total_edges} edges; {total_symbols} symbols, {total_calls} calls, "
              f"{total_cycles} module cycles). Open it in a browser.")

        if args.mermaid:
            from incidentlens.studio.mermaid import write_mermaid

            for name, graph in graphs.items():
                if args.service and name != args.service:
                    continue
                stem = Path(args.out).with_suffix("")
                suffix = f".{name}" if len(graphs) > 1 else ""
                mmd_out = Path(f"{stem}{suffix}.{args.level}.mmd")
                mmd, html = write_mermaid(
                    graph, mmd_out, level=args.level,
                    focus=args.focus, analysis=analysis_obj,
                )
                extra = f" (viewer: {html})" if html else ""
                print(f"Wrote {mmd}{extra}")

    elif args.command == "discover":
        from incidentlens.connectors.discovery import write_discovery

        arch_path, config_path = write_discovery(args.path, system=args.system)
        print(f"Architecture proposal: {arch_path}")
        print(f"Watch config:          {config_path}")
        print("Review both — the scan is a starting point, not a verdict. "
              "Then run `incidentlens watch --config " + config_path.name + "`.")

    elif args.command in ("analyze", "watch"):
        from incidentlens.domain.errors import NoIncidentDetected
        from incidentlens.live import (
            IncidentWatcher,
            VideoOptions,
            WatchSettings,
            load_code_graphs_near,
            load_config,
            render_incident,
        )

        root = Path.cwd()
        if args.config:
            architecture, sources, settings_w, out_dir = load_config(args.config)
            code_graphs = load_code_graphs_near(args.config)
        else:
            arch_path = Path(args.arch)
            if not arch_path.is_file():
                sys.exit(
                    f"architecture file {arch_path} not found — run `incidentlens "
                    "discover` first, or pass --arch/--config"
                )
            architecture = _load_architecture(arch_path)
            sources = _log_sources(args, root)
            settings_w = WatchSettings()
            out_dir = Path(args.out_dir)
            code_graphs = load_code_graphs_near(arch_path)
        if not sources:
            sys.exit("no log sources — pass --logs service=path (or use --config)")

        options = VideoOptions(
            voice=args.voice,
            narration_mode=args.narration,
            model=args.narration_model,
            provider=args.narration_provider,
            style=args.style,
            profile=args.profile,
            fps=args.fps,
            intro_video=args.intro_video,
            publish_genblaze=args.publish_genblaze,
            upload_b2=args.upload_b2,
            upload_genblaze_b2=args.upload_genblaze_b2,
        )

        if args.command == "analyze":
            from incidentlens.connectors.logfile import LogFileConnector

            events = LogFileConnector(sources, architecture).fetch_events()
            if not events:
                sys.exit("no log lines parsed — check the paths passed via --logs")

            if args.analysis_only:
                from incidentlens.live import analyze_events, write_companions

                try:
                    analysis = analyze_events(events, architecture)
                except NoIncidentDetected as exc:
                    sys.exit(f"no incident found in the supplied logs: {exc}")
                if code_graphs:
                    from incidentlens.connectors.code_graph import enrich_trace_with_code

                    enrich_trace_with_code(analysis, code_graphs)
                out_dir.mkdir(parents=True, exist_ok=True)
                briefing, analysis_json = write_companions(
                    analysis, out_dir / f"{analysis.incident_id}.mp4"
                )
                trace = analysis.internal_trace
                print(f"incident:  {analysis.incident_id} — {analysis.title}")
                print(f"evidence:  {len(analysis.evidence)} items")
                if trace is not None:
                    print(f"module:    {trace.failing_module}")
                    if trace.failing_symbol:
                        print(f"candidate: {trace.failing_symbol}  (static, not a stack frame)")
                print(f"briefing:  {briefing}")
                print(f"analysis:  {analysis_json}")
                print("(--analysis-only: no video rendered)")
                return

            try:
                analysis, video = render_incident(
                    events, architecture, out_dir, options,
                    code_graphs=code_graphs,
                    progress=_progress_printer("rendering"),
                )
            except NoIncidentDetected as exc:
                sys.exit(f"no incident found in the supplied logs: {exc}")
            print(f"incident:  {analysis.incident_id} — {analysis.title}")
            print(f"video:     {video}")
            print(f"briefing:  {video.with_suffix('')}.briefing.md")
            print(f"analysis:  {video.with_suffix('')}.analysis.json")
            if code_graphs:
                print(f"network:   {video.with_suffix('')}.code-graph.html")
            manifest = video.with_suffix(".genblaze.json")
            if manifest.is_file():
                print(f"manifest:  {manifest}")
        else:
            settings_w = WatchSettings(
                window_seconds=args.window,
                error_threshold=args.threshold,
                cooldown_seconds=args.cooldown,
                poll_seconds=WatchSettings().poll_seconds,
            ) if not args.config else settings_w
            watcher = IncidentWatcher(
                sources, architecture, out_dir, settings_w, options,
                code_graphs=code_graphs,
            )
            try:
                watcher.run_forever()
            except KeyboardInterrupt:
                print("\nstopped")


if __name__ == "__main__":
    main()
