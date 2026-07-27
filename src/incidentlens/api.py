from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from incidentlens import __version__, gallery, sandbox
from incidentlens.connectors.synthetic import SyntheticConnector, available_scenarios
from incidentlens.domain.errors import NoIncidentDetected
from incidentlens.domain.models import IncidentAnalysis, ScenarioInfo
from incidentlens.engines.deterministic import DeterministicAnalysisEngine
from incidentlens.services.incidents import IncidentService

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(
    title="IncidentLens",
    version=__version__,
    description="Evidence-backed incident reconstruction for distributed systems",
)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


class AnalyzeRequest(BaseModel):
    scenario: str = "default"


def _connector(scenario: str) -> SyntheticConnector:
    try:
        return SyntheticConnector(scenario)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/")
def home() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/v1/health")
def health() -> dict[str, str]:
    return {"status": "ok", "version": __version__}


@app.get("/api/v1/scenarios")
def scenarios() -> list[ScenarioInfo]:
    return available_scenarios()


@app.get("/api/v1/scenarios/{name}")
def scenario(name: str) -> dict[str, object]:
    connector = _connector(name)
    return {
        "info": connector.info().model_dump(mode="json"),
        "architecture": connector.fetch_architecture().model_dump(mode="json"),
        "events": [event.model_dump(mode="json") for event in connector.fetch_events()],
    }


@app.post("/api/v1/incidents/analyze")
def analyze(request: AnalyzeRequest) -> IncidentAnalysis:
    connector = _connector(request.scenario)
    service = IncidentService(connector=connector, engine=DeterministicAnalysisEngine())
    try:
        return service.analyze()
    except NoIncidentDetected as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


# --------------------------------------------------------------- incident library
#
# The published library lives in Backblaze B2. These routes read it back out —
# the catalog, the media URLs, and the Genblaze provenance for each incident.
# The bucket is public, so this service holds no B2 credentials.


@app.get("/gallery")
def gallery_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "gallery.html")


@app.get("/api/v1/incidents")
def incidents() -> dict[str, object]:
    """The incident catalog, read from B2."""
    entries = gallery.fetch_catalog()
    return {
        "source": gallery.public_base(),
        "count": len(entries),
        "incidents": entries,
    }


@app.get("/api/v1/incidents/{prefix:path}/provenance")
def incident_provenance(prefix: str) -> dict[str, object]:
    """Genblaze manifests for one incident, summarised."""
    if gallery.find_incident(prefix) is None:
        raise HTTPException(status_code=404, detail=f"unknown incident {prefix!r}")
    summary = gallery.fetch_provenance(prefix)
    if summary is None:
        raise HTTPException(
            status_code=502, detail="provenance manifests unreachable in object storage"
        )
    return {"prefix": prefix, "provenance": summary}


# ----------------------------------------------------------------------- sandbox
#
# Try the engine on telemetry that is not ours. Pasted log lines need no model;
# a one-line description is turned into structured telemetry by one, and that
# path is labelled synthesised wherever it surfaces. Either way the conclusions
# come from the deterministic engine, never from a model.


class SandboxRequest(BaseModel):
    prompt: str = Field(default="", max_length=sandbox.MAX_PROMPT_CHARS)
    logs: str = Field(default="", max_length=sandbox.MAX_LOG_CHARS)
    service: str = Field(default="service", max_length=64)


@app.get("/api/v1/sandbox")
def sandbox_state() -> dict[str, object]:
    """Whether scenario generation is configured here, and what is left today."""
    return {
        "generation_enabled": sandbox.enabled(),
        "paste_enabled": True,
        "max_prompt_chars": sandbox.MAX_PROMPT_CHARS,
        "max_log_chars": sandbox.MAX_LOG_CHARS,
        "quota": sandbox.quota_state(),
    }


@app.post("/api/v1/sandbox/reconstruct")
def sandbox_reconstruct(request: SandboxRequest, http: Request) -> dict[str, object]:
    """Reconstruct an incident from pasted logs, or from a described scenario."""
    synthesised = False
    try:
        if request.logs.strip():
            events = sandbox.parse_log_text(request.logs, service=request.service or "service")
            from incidentlens.domain.models import ArchitectureGraph, ServiceNode

            architecture = ArchitectureGraph(
                system="pasted-logs",
                services=[ServiceNode(name=request.service or "service", user_facing=True)],
            )
        else:
            client = http.client.host if http.client else "unknown"
            sandbox.check_quota(client)
            architecture, events = sandbox.synthesise_events(request.prompt)
            synthesised = True
    except sandbox.RateLimited as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except sandbox.SandboxDisabled as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except sandbox.SandboxError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        analysis = DeterministicAnalysisEngine().analyze(events, architecture)
    except NoIncidentDetected as exc:
        raise HTTPException(
            status_code=422,
            detail=(
                f"{exc} No error-level signal was found, so there is nothing to "
                "reconstruct — which is the honest answer rather than a guess."
            ),
        ) from exc

    result: dict[str, object] = {
        "synthesised": synthesised,
        "disclosure": (
            "Telemetry for this reconstruction was written by a language model from "
            "your description. The reconstruction itself is deterministic."
            if synthesised
            else "Reconstructed from the log lines you provided. No model was involved."
        ),
        "architecture": architecture.model_dump(mode="json"),
        "events": [event.model_dump(mode="json") for event in events],
        "analysis": analysis.model_dump(mode="json"),
    }
    if synthesised:
        # Surfaced rather than swallowed: if the model emitted events the domain
        # rejected, say which and why.
        dropped = sandbox.last_dropped()
        if dropped:
            result["dropped_events"] = dropped
    return result


@app.get("/api/v1/incidents/{prefix:path}")
def incident(prefix: str) -> dict[str, object]:
    """One incident bundle: media URLs plus the files published beside it."""
    entry = gallery.find_incident(prefix)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"unknown incident {prefix!r}")
    return {
        "incident": entry,
        "files": {
            name: {"label": label, "url": entry["files"][name]}
            for name, label in gallery.BUNDLE_FILES.items()
        },
    }
