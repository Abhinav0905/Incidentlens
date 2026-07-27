"""Narration for the incident video.

Two modes:

* ``template`` — deterministic lines built straight from the analysis. No
  network, no keys. This is the default and the reliable demo fallback.
* ``llm`` — a hosted model writes the narration from the analysis JSON. The
  system prompt forbids stating an inferred cause as fact and requires every
  line to stay inside the evidence already in the record. On any error the
  pipeline falls back to ``template`` so a live demo never dead-ends.

The ``llm`` mode is model-agnostic. Pick the provider and model per run:

* Claude (``claude-opus-4-8``, ``claude-sonnet-5``, …) via the ``anthropic`` SDK
* Any OpenAI-compatible endpoint via the ``openai`` SDK — hosted OpenAI GPT
  models, or a self-hosted gateway (e.g. the Hary Gateway → ModelProvider gateway)
  by pointing ``INCIDENTLENS_OPENAI_BASE_URL`` at it.

The provider is inferred from the model id when not given explicitly:
``claude-*`` → anthropic, everything else → openai.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field

from incidentlens.domain.models import IncidentAnalysis
from incidentlens.studio.evidence import module_failure_is_log_confirmed

logger = logging.getLogger(__name__)

DEFAULT_MODEL = os.environ.get("INCIDENTLENS_NARRATION_MODEL", "claude-sonnet-5")
DEFAULT_PROVIDER = os.environ.get("INCIDENTLENS_NARRATION_PROVIDER")  # or inferred


def resolve_provider(model: str, provider: str | None = None) -> str:
    """anthropic for ``claude-*``, openai otherwise — unless forced."""
    if provider:
        return provider.lower()
    if DEFAULT_PROVIDER:
        return DEFAULT_PROVIDER.lower()
    return "anthropic" if model.lower().startswith("claude") else "openai"


@dataclass(frozen=True)
class NarrationBeat:
    kind: str  # "intro" | "event" | "outro"
    timeline_index: int  # -1 for intro, last index folded for outro
    title: str
    text: str
    clock_label: str = ""
    evidence_ids: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Narration:
    incident_id: str
    title: str
    beats: list[NarrationBeat]


def _clock(iso: str) -> str:
    # iso like 2026-07-10T02:01:00Z or with offset; take HH:MM.
    try:
        time_part = iso.split("T", 1)[1]
        return time_part[:5] + " UTC"
    except (IndexError, ValueError):
        return ""


# Match single- and double-quoted spans separately so each pairs minimally
# (a naive [^'"]{n,} span mis-pairs a short quote's close with the next open).
_QUOTED = re.compile(r"'([^']+)'|\"([^\"]+)\"")


def _offending_value(text: str) -> str | None:
    """A quoted id/path/url in the failure detail — e.g. a misspelled model id.

    Config/validation failures usually name the bad value in quotes; we surface
    it so the narration can speak it instead of truncating it away. Only values
    that look like identifiers (a ``.``/``:``/``/`` separator, no spaces, 5+
    chars) qualify, so ordinary quoted words don't get picked up."""
    for match in _QUOTED.finditer(text):
        value = (match.group(1) or match.group(2)).strip()
        if len(value) >= 5 and " " not in value and any(sep in value for sep in "./:"):
            return value
    return None


def _speak_qual(qual: str) -> str:
    """A qualname spoken aloud: ``...agent.AgentNode.__call__`` -> "AgentNode call"."""
    parts = [p for p in qual.split(".") if p]
    tail = parts[-2:] if len(parts) >= 2 and parts[-1].startswith("__") else parts[-1:]
    return " ".join(tail).replace("__", "").replace("_", " ").replace("-", " ").strip()


def _speak_module(module: str) -> str:
    return module.rsplit(".", 1)[-1].replace("_", " ").replace("-", " ")


def _short_symbol(qual: str) -> str:
    """A qualname for a caption: ``...agent.AgentNode.__call__`` -> "AgentNode.__call__"."""
    parts = [p for p in qual.split(".") if p]
    if len(parts) >= 3 and parts[-2][:1].isupper():
        return ".".join(parts[-2:])  # Class.method
    return parts[-1] if parts else qual


def _first_sentence(text: str, limit: int = 200) -> str:
    text = text.strip()
    for sep in (". ", "; "):
        if sep in text:
            text = text.split(sep, 1)[0]
            break
    if len(text) > limit:
        clipped = text[: limit - 1]
        boundary = len(clipped)
        if (
            clipped
            and not clipped[-1].isspace()
            and boundary < len(text)
            and not text[boundary].isspace()
            and " " in clipped
        ):
            clipped = clipped.rsplit(" ", 1)[0]
        text = clipped.rstrip(" ,;:-") + "…"
    return text


def _is_model_policy_rejection(text: str) -> bool:
    lowered = text.lower()
    return "model_blocked" in lowered or (
        "model" in lowered and "not allowed" in lowered and "virtual key" in lowered
    )


def _is_invalid_model_identifier(text: str) -> bool:
    lowered = text.lower()
    return "model identifier is invalid" in lowered or (
        "validationexception" in lowered
        and "model" in lowered
        and ("invalid" in lowered or "not found" in lowered)
    )


def _is_model_configuration_change(text: str) -> bool:
    lowered = text.lower()
    return (
        ("model_id" in lowered or "model id" in lowered)
        and ("config" in lowered or "environment variable" in lowered)
        and ("set" in lowered or "change" in lowered or "updated" in lowered)
    )


def _spoken_failure_detail(text: str, limit: int = 200) -> str:
    if _is_model_policy_rejection(text):
        return (
            "The gateway rejected the configured model because it is not allowed "
            "for the active virtual key"
        )
    if _is_invalid_model_identifier(text):
        return "The model provider rejected the configured model identifier as invalid"
    return _first_sentence(text, limit=limit)


def _event_line(analysis: IncidentAnalysis, index: int) -> str:
    frame = analysis.timeline[index]
    sev = frame.severity.value
    svc = frame.services[0] if frame.services else "a service"
    evidence_ids = set(frame.evidence_ids)
    policy_detail = next(
        (
            event.detail
            for event in analysis.evidence
            if event.id in evidence_ids and _is_model_policy_rejection(event.detail)
        ),
        None,
    )
    detail = _spoken_failure_detail(policy_detail or frame.description)

    if sev == "info":
        return "The system starts healthy. Baseline telemetry looks normal across the graph."
    if frame.title.startswith("Change on"):
        if _is_model_configuration_change(frame.description):
            return f"Then the configured model identifier changes on {svc}."
        return f"Then a change lands on {svc}. {detail}."
    if frame.title == "Customer impact":
        quoted = next(
            (
                (match.group(1) or match.group(2)).strip()
                for match in _QUOTED.finditer(analysis.customer_impact)
                if (match.group(1) or match.group(2)).strip()
            ),
            None,
        )
        if quoted:
            return f"Customers are now affected. Users see {quoted!r}."
        return f"Customers are now affected. {analysis.customer_impact}"
    if frame.title == "Recovery":
        return f"The system recovers. {detail}."
    if sev == "critical":
        return f"{svc} fails. {detail}."
    return f"{svc} shows early strain. {detail}."


def _dive_after_index(analysis: IncidentAnalysis) -> int | None:
    """The timeline beat after which the camera dives into the origin service:
    the first critical event naming the traced service."""
    trace = analysis.internal_trace
    if trace is None or trace.failing_stage is None:
        return None
    for i, frame in enumerate(analysis.timeline):
        if frame.severity.value == "critical" and trace.service in frame.services:
            return i
    return None


def _internal_beats(analysis: IncidentAnalysis, timeline_index: int) -> list[NarrationBeat]:
    """Two beats narrating the journey through the service's internal pipeline."""
    trace = analysis.internal_trace
    assert trace is not None and trace.failing_stage is not None
    clock = _clock(analysis.timeline[timeline_index].timestamp.isoformat())

    by_name = {t.stage: t for t in trace.stages}
    upstream = [s for s in trace.path if s != trace.failing_stage]
    pretty = [
        stage.replace("_", " ").replace("-", " ") for stage in upstream
    ]
    if len(pretty) > 1:
        path_summary = f"from {pretty[0]} through {pretty[-1]}"
    elif pretty:
        path_summary = f"through {pretty[0]}"
    else:
        path_summary = "to the failure point"

    path_evidence = sorted({eid for s in upstream for eid in by_name[s].evidence_ids})[:5]
    dive = NarrationBeat(
        kind="internal_path",
        timeline_index=timeline_index,
        title=f"{trace.service} — traced request path",
        text=(
            f"Inside {trace.service}, the request crosses {len(upstream)} stages, "
            f"{path_summary}. Teal marks passed or traced; labels separate logged "
            "evidence from inference."
        )
        if upstream
        else (
            f"Inside {trace.service}, the request reaches the failure point. "
            "Labels separate logged evidence from inference."
        ),
        clock_label=clock,
        evidence_ids=path_evidence,
    )

    failing = by_name[trace.failing_stage]
    fail = NarrationBeat(
        kind="internal_fail",
        timeline_index=timeline_index,
        title=f"{trace.failing_stage}: logged failure",
        text=(
            f"At {trace.failing_stage.replace('_', ' ').replace('-', ' ')}, "
            f"the path turns red. "
            f"{_spoken_failure_detail(failing.detail, limit=125)}."
        ),
        clock_label=clock,
        evidence_ids=list(failing.evidence_ids)[:5],
    )
    return [dive, fail]


def _module_beats(analysis: IncidentAnalysis, timeline_index: int) -> list[NarrationBeat]:
    """Two concise beats for the failure-centred static module graph."""
    trace = analysis.internal_trace
    if trace is None or not trace.failing_module:
        return []
    if not trace.failing_callers and not trace.failing_callees:
        return []

    failing = next(
        (stage for stage in trace.stages if stage.stage == trace.failing_stage),
        None,
    )
    clock = _clock(analysis.timeline[timeline_index].timestamp.isoformat())
    module_spoken = _speak_module(trace.failing_module)
    confirmed = module_failure_is_log_confirmed(trace, analysis)
    path = NarrationBeat(
        kind="module_path",
        timeline_index=timeline_index,
        title=f"Package blueprint — {trace.failing_module}",
        text=(
            "One level deeper, the service opens as a package blueprint around "
            f"{module_spoken}. Teal marks verified code relationships, not "
            "runtime execution."
        ),
        clock_label=clock,
    )

    if confirmed:
        lead = f"The failure is logged inside {module_spoken}."
        locus = " Red marks that logged failure."
    else:
        lead = f"Static attribution identifies {module_spoken} as the failure locus."
        locus = " Amber marks that unconfirmed attribution."
    if trace.blast_radius:
        impact = (
            f" Its static blast radius contains {trace.blast_radius} potential "
            "dependents; amber highlights displayed dependency risk, while dim "
            "nodes are static context without runtime proof."
        )
    else:
        impact = " Amber marks potential dependents, not proven failures."
    fail = NarrationBeat(
        kind="module_fail",
        timeline_index=timeline_index,
        title=(
            f"{trace.failing_module}: failure logged in module"
            if confirmed
            else f"{trace.failing_module}: attributed failure locus"
        ),
        text=f"{lead}{locus}{impact}",
        clock_label=clock,
        evidence_ids=list(failing.evidence_ids)[:5] if failing else [],
    )
    return [path, fail]


def _symbol_beats(analysis: IncidentAnalysis, timeline_index: int) -> list[NarrationBeat]:
    """Two beats for the static function-level candidate and logged defect.

    Deeper than ``_internal_beats`` (stage level): this speaks the failing
    ``module.Class.method`` and the functions it calls — the same resolution as
    the symbol Mermaid. Returns ``[]`` when the trace has no call-graph context.
    """
    trace = analysis.internal_trace
    if trace is None or not trace.failing_symbol:
        return []
    callees = list(trace.failing_symbol_callees)
    callers = list(trace.failing_symbol_callers)
    if not callees and not callers:
        return []

    clock = _clock(analysis.timeline[timeline_index].timestamp.isoformat())
    short = _short_symbol(trace.failing_symbol)
    sym_spoken = _speak_qual(trace.failing_symbol)

    if callers:
        who = [_speak_qual(c) for c in callers[:3]]
        joined = ", ".join(who[:-1]) + f" and {who[-1]}" if len(who) > 1 else who[0]
        anatomy = f"between {joined} and "
    else:
        anatomy = "at "
    if callees:
        names = [_speak_qual(c) for c in callees[:3]]
        listed = ", ".join(names[:-1]) + f" and {names[-1]}" if len(names) > 1 else names[0]
        calls = f"its helpers, {listed}."
    else:
        calls = "the model call."
    path = NarrationBeat(
        kind="symbol_path",
        timeline_index=timeline_index,
        title=f"Function blueprint — {short}",
        text=(
            "Now functions open inside their classes. Static calls narrow toward "
            f"{sym_spoken}, {anatomy}{calls}"
        ),
        clock_label=clock,
    )

    failing = next((s for s in trace.stages if s.stage == trace.failing_stage), None)
    detail = failing.detail if failing else ""
    fail = NarrationBeat(
        kind="symbol_fail",
        timeline_index=timeline_index,
        title=f"{short}: candidate failure locus",
        text=(
            "This is a candidate locus, not a confirmed stack frame. The logged "
            f"failure remains {_spoken_failure_detail(detail, limit=105).lower()}."
        ),
        clock_label=clock,
        evidence_ids=list(failing.evidence_ids)[:5] if failing else [],
    )
    return [path, fail]


def _template_beats(analysis: IncidentAnalysis) -> list[NarrationBeat]:
    system_label = analysis.title.split("(")[-1].rstrip(")") or "system"
    beats: list[NarrationBeat] = [
        NarrationBeat(
            kind="intro",
            timeline_index=-1,
            title="Incident reconstruction",
            text=(
                f"Reconstructing an incident on the {system_label}. Here is how it "
                "started, how it spread, and what reached customers — every step "
                "tied to evidence."
            ),
            clock_label="",
            evidence_ids=[],
        )
    ]

    last_index = 0
    dive_after = _dive_after_index(analysis)
    for i, frame in enumerate(analysis.timeline):
        last_index = i
        beats.append(
            NarrationBeat(
                kind="event",
                timeline_index=i,
                title=frame.title if len(frame.title) <= 70 else frame.title[:69] + "…",
                text=_event_line(analysis, i),
                clock_label=_clock(frame.timestamp.isoformat()),
                evidence_ids=list(frame.evidence_ids),
            )
        )
        if i == dive_after:
            beats.extend(_internal_beats(analysis, i))
            beats.extend(_module_beats(analysis, i))
            beats.extend(_symbol_beats(analysis, i))

    root = next((h for h in analysis.hypotheses if h.status.value == "inferred"), None)
    top_action = min(
        analysis.recommended_actions, key=lambda a: a.priority, default=None
    )

    if root:
        pct = round(root.confidence * 100)
        lead = re.sub(
            r"^(?:root cause|likely cause):\s*",
            "",
            root.title.split(" — ")[0],
            flags=re.IGNORECASE,
        ).strip()
        beats.append(
            NarrationBeat(
                kind="outro",
                timeline_index=last_index,
                title="Likely cause",
                text=(
                    f"The evidence points to a {lead}, at about {pct} percent "
                    "confidence — a lead to verify, not a verdict."
                ),
                clock_label=_clock(analysis.detected_at.isoformat()),
                evidence_ids=list(root.evidence_ids),
            )
        )

    check_bits: list[str] = []
    if top_action:
        spoken_action = re.sub(r"\s*\([^)]*\)", "", top_action.action)
        first_check = _first_sentence(spoken_action, limit=120).rstrip(".!?…")
        check_bits.append(f"First check: {first_check}.")
    if analysis.missing_evidence:
        gap = analysis.missing_evidence[0].rstrip(".")
        if len(gap) > 120:
            gap = _first_sentence(gap, limit=120)
        check_bits.append(f"Still open: {gap.lower().rstrip('.!?…')}.")
    beats.append(
        NarrationBeat(
            kind="outro",
            timeline_index=last_index,
            title="What to check first",
            text=" ".join(check_bits) or "Reconstruction complete.",
            clock_label=_clock(analysis.detected_at.isoformat()),
            evidence_ids=[],
        )
    )
    return beats


_SYSTEM_PROMPT = (
    "You narrate distributed-systems incident replays for on-call engineers. "
    "You are given an incident analysis as JSON. Write a spoken narration, one "
    "short line per timeline beat, plus an intro and an outro. Rules: never "
    "state an inferred root cause as established fact — phrase it as what the "
    "evidence points to, and say confirmed only for beats the analysis marks "
    "confirmed. Stay strictly inside the evidence in the JSON; invent nothing. "
    "When 'internal_trace' names a failing_symbol, failing_symbol_callers or "
    "blast_radius, you MAY name that symbol only as a candidate selected by "
    "static call-graph ranking. Never call it the failing function or a "
    "confirmed runtime frame unless the evidence contains a stack frame. "
    "Keep each line to one or two sentences, plain spoken English, no jargon "
    "the audio can't carry. Return ONLY a JSON array, no prose, no code fences. "
    "Each element: {\"role\": \"intro\"|\"event\"|\"outro\", \"timeline_index\": int, "
    "\"text\": string}. Use timeline_index -1 for intro and the final index for "
    "outro. Provide exactly one event element per timeline beat, in order."
)


def _complete_anthropic(system: str, user: str, model: str) -> str:
    import anthropic
    from anthropic.types import TextBlock

    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
    message = client.messages.create(
        model=model, max_tokens=1500, system=system,
        messages=[{"role": "user", "content": user}],
    )
    return "".join(
        block.text for block in message.content if isinstance(block, TextBlock)
    ).strip()


def _complete_openai(system: str, user: str, model: str) -> str:
    """Any OpenAI-compatible endpoint: hosted OpenAI, or a gateway such as the
    Hary Gateway → ModelProvider proxy via ``INCIDENTLENS_OPENAI_BASE_URL``."""
    from openai import OpenAI

    base_url = os.environ.get("INCIDENTLENS_OPENAI_BASE_URL") or os.environ.get(
        "OPENAI_BASE_URL"
    )
    # reads OPENAI_API_KEY; base_url lets a gateway (Gateway/ModelProvider) stand in
    client = OpenAI(base_url=base_url) if base_url else OpenAI()
    resp = client.chat.completions.create(
        model=model,
        max_completion_tokens=1500,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )
    return (resp.choices[0].message.content or "").strip()


def _complete(system: str, user: str, model: str, provider: str) -> str:
    if provider == "anthropic":
        return _complete_anthropic(system, user, model)
    if provider == "openai":
        return _complete_openai(system, user, model)
    raise ValueError(f"unknown narration provider: {provider!r}")


def _llm_beats(
    analysis: IncidentAnalysis, model: str, provider: str
) -> list[NarrationBeat]:
    payload = analysis.model_dump(mode="json")
    user = (
        "Incident analysis JSON:\n\n"
        + json.dumps(payload, indent=2)
        + "\n\nReturn the narration JSON array now."
    )
    text = _complete(_SYSTEM_PROMPT, user, model, provider).strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    raw = json.loads(text)

    template = {b.timeline_index: b for b in _template_beats(analysis) if b.kind == "event"}
    beats: list[NarrationBeat] = []
    for item in raw:
        role = str(item.get("role", "event"))
        idx = int(item.get("timeline_index", -1))
        line = str(item.get("text", "")).strip()
        if not line:
            continue
        if role == "event" and 0 <= idx < len(analysis.timeline):
            frame = analysis.timeline[idx]
            beats.append(
                NarrationBeat(
                    kind="event",
                    timeline_index=idx,
                    title=frame.title if len(frame.title) <= 70 else frame.title[:69] + "…",
                    text=line,
                    clock_label=_clock(frame.timestamp.isoformat()),
                    evidence_ids=list(frame.evidence_ids),
                )
            )
        elif role == "intro":
            beats.append(
                NarrationBeat(
                    kind="intro",
                    timeline_index=-1,
                    title="Incident reconstruction",
                    text=line,
                )
            )
        elif role == "outro":
            last = len(analysis.timeline) - 1
            beats.append(
                NarrationBeat(
                    kind="outro",
                    timeline_index=last,
                    title="What to check first",
                    text=line,
                    clock_label=_clock(analysis.detected_at.isoformat()),
                )
            )
    # Guardrail: if the model dropped beats, backfill missing events from template.
    covered = {b.timeline_index for b in beats if b.kind == "event"}
    for idx, tmpl in template.items():
        if idx not in covered:
            beats.append(tmpl)

    # The internal dive is deterministic (evidence-bound), so it is inserted
    # from the template even when Claude writes the surrounding narration.
    dive_after = _dive_after_index(analysis)
    if dive_after is not None:
        beats.extend(_internal_beats(analysis, dive_after))
        beats.extend(_module_beats(analysis, dive_after))
        beats.extend(_symbol_beats(analysis, dive_after))

    _SUBRANK = {
        "event": 0,
        "internal_path": 1,
        "internal_fail": 2,
        "module_path": 3,
        "module_fail": 4,
        "symbol_path": 5,
        "symbol_fail": 6,
    }

    def _order(beat: NarrationBeat) -> tuple[int, int, int]:
        rank = 0 if beat.kind == "intro" else 2 if beat.kind == "outro" else 1
        return (rank, beat.timeline_index, _SUBRANK.get(beat.kind, 0))

    beats.sort(key=_order)
    if not any(b.kind == "event" for b in beats):
        raise ValueError("LLM narration produced no usable event beats")
    return beats


def build_narration(
    analysis: IncidentAnalysis,
    *,
    mode: str = "template",
    model: str = DEFAULT_MODEL,
    provider: str | None = None,
) -> Narration:
    if mode == "llm":
        resolved = resolve_provider(model, provider)
        try:
            beats = _llm_beats(analysis, model, resolved)
        except Exception as exc:  # noqa: BLE001 - demo safety: never dead-end
            logger.warning(
                "LLM narration failed (provider=%s model=%s: %s); using template",
                resolved, model, exc,
            )
            beats = _template_beats(analysis)
    elif mode == "template":
        beats = _template_beats(analysis)
    else:
        raise ValueError(f"unknown narration mode: {mode!r}")
    return Narration(incident_id=analysis.incident_id, title=analysis.title, beats=beats)
