from incidentlens.connectors.base import TelemetryConnector
from incidentlens.domain.models import IncidentAnalysis
from incidentlens.engines.base import AnalysisEngine


class IncidentService:
    def __init__(
        self,
        connector: TelemetryConnector,
        engine: AnalysisEngine,
    ) -> None:
        self.connector = connector
        self.engine = engine

    def analyze(self) -> IncidentAnalysis:
        events = self.connector.fetch_events()
        architecture = self.connector.fetch_architecture()
        return self.engine.analyze(events, architecture)
