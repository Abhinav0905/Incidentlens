from abc import ABC, abstractmethod

from incidentlens.domain.models import ArchitectureGraph, TelemetryEvent


class TelemetryConnector(ABC):
    @abstractmethod
    def fetch_events(self) -> list[TelemetryEvent]:
        raise NotImplementedError

    @abstractmethod
    def fetch_architecture(self) -> ArchitectureGraph:
        raise NotImplementedError
