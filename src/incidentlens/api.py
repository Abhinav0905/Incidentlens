from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from incidentlens import __version__
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
