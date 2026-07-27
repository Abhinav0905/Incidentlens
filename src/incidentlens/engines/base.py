from abc import ABC, abstractmethod

from incidentlens.domain.models import ArchitectureGraph, IncidentAnalysis, TelemetryEvent


class AnalysisEngine(ABC):
    @abstractmethod
    def analyze(
        self,
        events: list[TelemetryEvent],
        architecture: ArchitectureGraph,
    ) -> IncidentAnalysis:
        raise NotImplementedError
