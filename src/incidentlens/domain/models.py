from __future__ import annotations

from datetime import datetime
from typing import Any

try:  # Python 3.11+
    from enum import StrEnum
except ImportError:  # pragma: no cover - Python 3.10 fallback, same semantics for our use
    from enum import Enum

    class StrEnum(str, Enum):  # type: ignore[no-redef]
        """Minimal backport: members are strings; ``.value`` behaves identically."""

        def __str__(self) -> str:  # match 3.11 StrEnum
            return str(self.value)

from pydantic import BaseModel, Field


class SourceType(StrEnum):
    LOG = "log"
    METRIC = "metric"
    DEPLOYMENT = "deployment"
    ARCHITECTURE = "architecture"


class ConclusionStatus(StrEnum):
    CONFIRMED = "confirmed"
    INFERRED = "inferred"
    UNKNOWN = "unknown"


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"
    RECOVERY = "recovery"


class TelemetryEvent(BaseModel):
    id: str
    source_type: SourceType
    source: str
    timestamp: datetime
    detail: str
    attributes: dict[str, Any] = Field(default_factory=dict)


class InternalStage(BaseModel):
    """One nut-or-bolt inside a service: an endpoint, middleware, graph node,
    or shared client. ``modules`` are dotted module prefixes used to attribute
    log lines (via their logger name) to this stage."""

    name: str
    modules: list[str] = Field(default_factory=list)
    description: str = ""


class ServiceInternals(BaseModel):
    """The internal pipeline of a service: stages plus flow edges (from -> to).

    Produced by ``incidentlens discover`` (static code scan) or written by
    hand — either way it is an editable proposal, not a verdict.
    """

    stages: list[InternalStage] = Field(default_factory=list)
    edges: list[tuple[str, str]] = Field(default_factory=list)
    entry: str | None = None  # defaults to the first stage with no inbound edge

    def entry_stage(self) -> str | None:
        if self.entry:
            return self.entry
        targets = {dst for _, dst in self.edges}
        for stage in self.stages:
            if stage.name not in targets:
                return stage.name
        return self.stages[0].name if self.stages else None


class ServiceNode(BaseModel):
    name: str
    owner: str | None = None
    depends_on: list[str] = Field(default_factory=list)
    user_facing: bool = False
    internals: ServiceInternals | None = None


class ArchitectureGraph(BaseModel):
    system: str
    services: list[ServiceNode]
    edges: list[tuple[str, str]] = Field(default_factory=list)

    def dependency_edges(self) -> list[tuple[str, str]]:
        """Edges as (service, dependency) pairs, derived from depends_on when absent."""
        if self.edges:
            return self.edges
        return [(svc.name, dep) for svc in self.services for dep in svc.depends_on]


class TimelineEvent(BaseModel):
    timestamp: datetime
    title: str
    description: str
    severity: Severity
    services: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)


class Hypothesis(BaseModel):
    title: str
    confidence: float = Field(ge=0.0, le=1.0)
    status: ConclusionStatus
    explanation: str
    evidence_ids: list[str] = Field(default_factory=list)


class PropagationStep(BaseModel):
    from_service: str
    to_service: str
    mechanism: str
    evidence_ids: list[str] = Field(default_factory=list)


class ActionItem(BaseModel):
    priority: int = Field(ge=1)
    action: str
    reason: str
    risk: str


class StageStatus(StrEnum):
    OK = "ok"  # direct telemetry from this stage during the traversal
    INFERRED = "inferred"  # upstream of the failure on the path, but silent
    FAILED = "failed"  # the stage that raised
    NOT_REACHED = "not-reached"  # downstream of the failure, never ran
    DORMANT = "dormant"  # off the traversed path
    UNKNOWN = "unknown"


class CodeModule(BaseModel):
    """One module in a service's code graph — a nut in the machine."""

    name: str  # dotted module path, e.g. hary.models.llm_factory
    kind: str = "module"  # endpoint | middleware | graph-node | client | config | module
    stage: str | None = None  # internal pipeline stage this module belongs to
    defs: list[str] = Field(default_factory=list)  # top-level classes/functions
    loc: int = 0
    # Structural metrics (populated by the deep scan; 0 when unknown):
    fan_in: int = 0  # modules that use this one
    fan_out: int = 0  # modules this one uses
    blast_radius: int = 0  # modules that transitively depend on this one
    in_cycle: bool = False  # part of an import/call cycle (a coupled cluster)


class CodeEdge(BaseModel):
    """src uses dst: an import, or resolved calls (with the symbols called)."""

    src: str
    dst: str
    kind: str = "import"  # import | call | dynamic-import
    symbols: list[str] = Field(default_factory=list)
    count: int = 1


class CodeSymbol(BaseModel):
    """A function, method, or class — a node in the fine-grained call graph.

    Where ``CodeModule`` is file-level, this is the ``module.Class.method`` /
    ``module.function`` granularity a true call graph needs. Static and
    best-effort (Python is dynamic), from ``ast`` — nothing imported or run.
    """

    qualname: str  # hary.models.llm_factory.get_llm  (module.[Class.]name)
    module: str  # owning module, e.g. hary.models.llm_factory
    name: str  # short name, e.g. get_llm
    kind: str = "function"  # function | async-function | method | async-method | class | module
    parent: str | None = None  # enclosing class qualname, for methods
    role: str = "logic"  # endpoint|client|config|middleware|graph-node|logic|test
    stage: str | None = None  # internal pipeline stage the owning module belongs to
    lineno: int = 0
    loc: int = 0
    decorators: list[str] = Field(default_factory=list)


class SymbolEdge(BaseModel):
    """caller ``src`` calls callee ``dst`` (both qualnames)."""

    src: str
    dst: str
    kind: str = "call"  # call | construct | dynamic
    lineno: int = 0
    count: int = 1


class CodeGraph(BaseModel):
    """Dependency network of one service, from static analysis.

    Two granularities live here. ``modules``/``edges`` are the file-level view
    (kept stable for callers that predate the deep pass). ``symbols``/
    ``symbol_edges`` are the fine-grained call graph — ``module.Class.method``
    nodes and the resolved calls between them. ``cycles`` lists coupled module
    clusters (strongly-connected components with more than one member), the
    structural-flaw signal.
    """

    service: str
    modules: list[CodeModule] = Field(default_factory=list)
    edges: list[CodeEdge] = Field(default_factory=list)
    symbols: list[CodeSymbol] = Field(default_factory=list)
    symbol_edges: list[SymbolEdge] = Field(default_factory=list)
    cycles: list[list[str]] = Field(default_factory=list)  # module-level SCCs (>1)
    symbol_cycles: list[list[str]] = Field(default_factory=list)  # recursion clusters

    def symbol_callers_of(self, qualname: str) -> list[str]:
        """Qualnames that call ``qualname`` (deduped, order-preserving)."""
        out: list[str] = []
        seen: set[str] = set()
        for e in self.symbol_edges:
            if e.dst == qualname and e.src not in seen:
                seen.add(e.src)
                out.append(e.src)
        return out

    def symbol_callees_of(self, qualname: str) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for e in self.symbol_edges:
            if e.src == qualname and e.dst not in seen:
                seen.add(e.dst)
                out.append(e.dst)
        return out

    def symbols_in_module(self, module: str) -> list[CodeSymbol]:
        return [s for s in self.symbols if s.module == module]

    def callers_of(self, module: str) -> list[tuple[str, list[str]]]:
        """Who uses ``module`` — (caller, symbols), call edges ranked first."""
        found = [
            (e.src, e.symbols)
            for e in sorted(self.edges, key=lambda e: e.kind != "call")
            if e.dst == module
        ]
        seen: set[str] = set()
        out: list[tuple[str, list[str]]] = []
        for src, symbols in found:
            if src not in seen:
                seen.add(src)
                out.append((src, symbols))
        return out

    def callees_of(self, module: str) -> list[tuple[str, list[str]]]:
        found = [
            (e.dst, e.symbols)
            for e in sorted(self.edges, key=lambda e: e.kind != "call")
            if e.src == module
        ]
        seen: set[str] = set()
        out: list[tuple[str, list[str]]] = []
        for dst, symbols in found:
            if dst not in seen:
                seen.add(dst)
                out.append((dst, symbols))
        return out


class InternalStageTrace(BaseModel):
    stage: str
    status: StageStatus
    detail: str = ""
    evidence_ids: list[str] = Field(default_factory=list)


class InternalTrace(BaseModel):
    """The request's journey through the origin service's internal pipeline.

    Statuses keep the evidence rule: ``ok`` only with direct telemetry,
    ``inferred`` for silent stages the path must have crossed, ``failed``
    for the stage that raised — never a guess presented as fact.
    """

    service: str
    stages: list[InternalStageTrace]
    path: list[str] = Field(default_factory=list)  # entry ... failing stage
    failing_stage: str | None = None
    summary: str = ""
    # Code-graph context for the failing stage (when a deep scan exists):
    failing_module: str | None = None  # e.g. hary.models.llm_factory
    failing_callers: list[str] = Field(default_factory=list)  # who calls it
    failing_callees: list[str] = Field(default_factory=list)  # what it calls
    # Fine-grained (call-graph) context for the failing symbol:
    failing_symbol: str | None = None  # e.g. hary.models.llm_factory.get_llm
    failing_symbol_role: str | None = None  # endpoint|client|config|...
    failing_symbol_callers: list[str] = Field(default_factory=list)  # qualnames
    failing_symbol_callees: list[str] = Field(default_factory=list)  # qualnames
    blast_radius: int = 0  # modules that transitively depend on the failing module
    failing_in_cycle: list[str] = Field(default_factory=list)  # coupled cluster, if any


class ScenarioInfo(BaseModel):
    name: str
    title: str
    description: str


class IncidentAnalysis(BaseModel):
    incident_id: str
    title: str
    started_at: datetime
    detected_at: datetime
    affected_services: list[str]
    customer_impact: str
    timeline: list[TimelineEvent]
    hypotheses: list[Hypothesis]
    propagation: list[PropagationStep]
    evidence: list[TelemetryEvent]
    recommended_actions: list[ActionItem]
    engineer_briefing: str
    executive_summary: str
    missing_evidence: list[str]
    replay_script: list[str]
    internal_trace: InternalTrace | None = None
