"""Screen-space overlay: title block, live clock, caption card, progress rail.

Drawn at output resolution after the 3D scene is downsampled, so HUD type is
always pixel-crisp regardless of supersampling. Everything scales with frame
height so 720p, 1080p and 4K renders keep identical proportions.
"""

from __future__ import annotations

from PIL import Image, ImageDraw

from incidentlens.studio.cinema import palette
from incidentlens.studio.cinema.easing import clamp, with_alpha
from incidentlens.studio.cinema.fonts import fit_text, font
from incidentlens.studio.cinema.timeline import BeatSpan, Timeline
from incidentlens.studio.evidence import module_failure_is_log_confirmed


def _beat_color(timeline: Timeline, span: BeatSpan) -> palette.RGB:
    beat = span.beat
    if beat.kind in ("internal_path", "module_path", "symbol_path"):
        return palette.EDGE_TRACE
    if beat.kind == "internal_fail":
        return palette.SEVERITY_COLOR["critical"]
    if beat.kind == "module_fail":
        trace = timeline.analysis.internal_trace
        confirmed = bool(
            trace
            and module_failure_is_log_confirmed(trace, timeline.analysis)
        )
        state = "critical" if confirmed else "warning"
        return palette.SEVERITY_COLOR[state]
    if beat.kind == "symbol_fail":
        return palette.SEVERITY_COLOR["warning"]
    if beat.kind == "intro":
        return palette.ACCENT
    if beat.kind == "outro":
        return palette.SEVERITY_COLOR["recovery"] if beat.title.lower().startswith(
            "what"
        ) else palette.ACCENT
    if 0 <= beat.timeline_index < len(timeline.analysis.timeline):
        sev = timeline.analysis.timeline[beat.timeline_index].severity.value
        return palette.SEVERITY_COLOR.get(sev, palette.ACCENT)
    return palette.ACCENT


def _wrap(draw: ImageDraw.ImageDraw, text: str, fnt, max_width: int) -> list[str]:
    words = text.split()
    lines: list[str] = []
    line = ""
    for word in words:
        candidate = f"{line} {word}".strip()
        if draw.textlength(candidate, font=fnt) > max_width and line:
            lines.append(line)
            line = word
        else:
            line = candidate
    if line:
        lines.append(line)
    return lines


def _breadcrumb(timeline: Timeline, span: BeatSpan) -> str:
    trace = timeline.analysis.internal_trace
    if trace is None:
        return ""
    if span.beat.kind.startswith("internal"):
        return f"{trace.service}  ›  REQUEST PATH"
    module = trace.failing_module or "attributed module"
    if span.beat.kind.startswith("module"):
        return f"{trace.service}  ›  {module}  ›  PACKAGE BLUEPRINT"
    if span.beat.kind.startswith("symbol"):
        symbol = (
            trace.failing_symbol.rsplit(".", 1)[-1]
            if trace.failing_symbol
            else "candidate function"
        )
        return f"{trace.service}  ›  {module}  ›  {symbol}"
    return ""


def draw_hud(img: Image.Image, timeline: Timeline, t: float) -> None:
    w, h = img.size
    u = h / 1080.0  # HUD unit
    draw = ImageDraw.Draw(img, "RGBA")
    span = timeline.span_at(t)
    alpha = timeline.caption_alpha(span, t)
    color = _beat_color(timeline, span)

    # ---------------------------------------------------------------- header
    x0 = int(64 * u)
    title_font = font("sans-bold", int(30 * u))
    draw.text(
        (x0 + int(1.5 * u), int(42 * u) + int(1.5 * u)),
        timeline.analysis.title,
        font=title_font,
        fill=(0, 0, 0, 140),
    )
    draw.text((x0, int(42 * u)), timeline.analysis.title, font=title_font,
              fill=with_alpha(palette.TEXT, 0.96))
    meta = (
        f"{timeline.analysis.incident_id} · evidence-backed reconstruction "
        "· AI-generated voice"
    )
    draw.text((x0, int(82 * u)), meta, font=font("mono", int(15 * u)),
              fill=with_alpha(palette.DIM, 0.9))

    clock = timeline.clock_at(t)
    if clock:
        cfont = font("mono-bold", int(30 * u))
        cw = draw.textlength(clock, font=cfont)
        draw.text((w - int(64 * u) - cw, int(44 * u)), clock, font=cfont,
                  fill=with_alpha(palette.ACCENT, 0.95))
        tag = "INCIDENT REPLAY"
        tfont = font("mono", int(13 * u))
        tw = draw.textlength(tag, font=tfont)
        draw.text((w - int(64 * u) - tw, int(84 * u)), tag, font=tfont,
                  fill=with_alpha(palette.DIM, 0.75))

    breadcrumb = _breadcrumb(timeline, span)
    if breadcrumb:
        crumb_font = font("mono-bold", int(15 * u))
        draw.text(
            (x0, int(116 * u)),
            breadcrumb,
            font=crumb_font,
            fill=with_alpha(color, 0.92),
        )

    # ---------------------------------------------------------- caption card
    if alpha > 0.01:
        card_w = min(int(1320 * u), w - int(128 * u))
        card_h = int(210 * u)
        cx0 = x0
        cy0 = h - int(82 * u) - card_h
        rise = int((1.0 - alpha) * 14 * u)  # slides up as it fades in
        cy0 += rise
        draw.rounded_rectangle(
            (cx0, cy0, cx0 + card_w, cy0 + card_h),
            radius=int(16 * u),
            fill=(12, 16, 24, int(232 * alpha)),
            outline=(72, 86, 112, int(255 * alpha)),
            width=max(1, int(2 * u)),
        )
        # severity spine on the card's left edge
        draw.rounded_rectangle(
            (cx0, cy0, cx0 + int(6 * u), cy0 + card_h),
            radius=int(3 * u),
            fill=with_alpha(color, alpha),
        )
        pad = int(28 * u)
        tfont = fit_text(draw, span.beat.title, "sans-bold", int(30 * u),
                         card_w - 2 * pad - int(150 * u))
        draw.text((cx0 + pad, cy0 + int(20 * u)), span.beat.title, font=tfont,
                  fill=(235, 238, 244, int(255 * alpha)))
        kind_tag = {
            "intro": "OVERVIEW",
            "outro": "FINDINGS",
            "internal_path": "REQUEST PATH",
            "internal_fail": "FAILURE PATH",
            "module_path": "MODULE BLUEPRINT",
            "module_fail": "INCIDENT OVERLAY",
            "symbol_path": "FUNCTION BLUEPRINT",
            "symbol_fail": "STATIC CANDIDATE",
        }.get(span.beat.kind, f"T+{span.beat.timeline_index:02d}")
        kfont = font("mono", int(13 * u))
        kw = draw.textlength(kind_tag, font=kfont)
        draw.text((cx0 + card_w - pad - kw, cy0 + int(24 * u)), kind_tag,
                  font=kfont, fill=with_alpha(color, 0.9 * alpha))

        bfont = font("sans", int(21 * u))
        lines = _wrap(draw, span.beat.text, bfont, card_w - 2 * pad)[:3]
        ty = cy0 + int(68 * u)
        for line in lines:
            draw.text((cx0 + pad, ty), line, font=bfont,
                      fill=(205, 212, 224, int(255 * alpha)))
            ty += int(31 * u)

        if span.beat.evidence_ids:
            ex = cx0 + pad
            ey = cy0 + card_h - int(34 * u)
            efont = font("mono", int(13 * u))
            for eid in span.beat.evidence_ids[:5]:
                ew = draw.textlength(eid, font=efont)
                draw.rounded_rectangle(
                    (ex, ey, ex + ew + int(16 * u), ey + int(22 * u)),
                    radius=int(11 * u),
                    fill=(140, 168, 218, int(34 * alpha)),
                )
                draw.text((ex + int(8 * u), ey + int(4 * u)), eid, font=efont,
                          fill=(150, 178, 228, int(230 * alpha)))
                ex += int(ew + 24 * u)

    # ------------------------------------------------------------- legend
    intro_end = timeline.spans[0].end if timeline.spans else 0.0
    if span.beat.kind.startswith("internal"):
        legend = [
            ("passed / traced", "recovery"),
            ("failed · logged", "critical"),
            ("not reached", "dormant"),
        ]
        legend_alpha = 1.0
    elif span.beat.kind.startswith("module"):
        trace = timeline.analysis.internal_trace
        confirmed = bool(
            trace and module_failure_is_log_confirmed(trace, timeline.analysis)
        )
        legend = (
            [
                ("verified link", "recovery"),
                ("potential dependent", "warning"),
                ("failure logged", "critical"),
            ]
            if confirmed
            else [
                ("verified link", "recovery"),
                ("attributed / dependent", "warning"),
                ("static only", "dormant"),
            ]
        )
        legend_alpha = 1.0
    elif span.beat.kind.startswith("symbol"):
        legend = [
            ("static call", "recovery"),
            ("candidate locus", "warning"),
            ("runtime unconfirmed", "dormant"),
        ]
        legend_alpha = 1.0
    else:
        legend = [
            ("recovery", "recovery"),
            ("critical", "critical"),
            ("warning", "warning"),
            ("healthy", "healthy"),
        ]
        legend_alpha = (
            clamp((intro_end + 1.0 - t) / 1.0) if intro_end else 0.0
        )
    if legend_alpha > 0.01:
        lx = w - int(64 * u)
        ly = int(126 * u)
        lfont = font("mono", int(13 * u))
        for word, state in legend:
            tw = draw.textlength(word, font=lfont)
            lx -= int(tw)
            draw.text((lx, ly), word, font=lfont,
                      fill=with_alpha(palette.DIM, 0.85 * legend_alpha))
            lx -= int(16 * u)
            draw.ellipse((lx, ly + int(3 * u), lx + int(9 * u), ly + int(12 * u)),
                         fill=with_alpha(palette.STATE_STROKE[state], legend_alpha))
            lx -= int(22 * u)

    # ------------------------------------------------------------- progress
    px0, px1 = x0, w - int(64 * u)
    py = h - int(52 * u)
    bar_h = int(6 * u)
    draw.rounded_rectangle((px0, py, px1, py + bar_h), radius=bar_h // 2,
                           fill=(255, 255, 255, 26))
    for s in timeline.spans[1:]:
        mx = px0 + int((px1 - px0) * (s.start / timeline.total))
        draw.line((mx, py - int(2 * u), mx, py + bar_h + int(2 * u)),
                  fill=(255, 255, 255, 34), width=max(1, int(1.5 * u)))
    prog = timeline.progress_at(t)
    fill_x = px0 + int((px1 - px0) * prog)
    if fill_x > px0:
        draw.rounded_rectangle((px0, py, fill_x, py + bar_h), radius=bar_h // 2,
                               fill=with_alpha(palette.ACCENT, 0.85))
    # playhead with a soft halo
    for r, a in ((int(14 * u), 26), (int(9 * u), 60), (int(5 * u), 255)):
        draw.ellipse((fill_x - r, py + bar_h // 2 - r, fill_x + r, py + bar_h // 2 + r),
                     fill=with_alpha(color, a / 255.0))

    brand = "IncidentLens · OBSERVE → TRACE → EXPLAIN"
    bfont2 = font("mono-bold", int(14 * u))
    bw = draw.textlength(brand, font=bfont2)
    draw.text((w - int(64 * u) - bw, h - int(40 * u)), brand, font=bfont2,
              fill=with_alpha(palette.DIM, 0.6))
