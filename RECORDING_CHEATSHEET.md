# Recording cheat sheet

Keep this open on the **laptop screen**. Record the **external 1920×1080 display**.

**You need ONE terminal and ONE browser. Not three.** CRIS AI is never started —
its logs are already on disk, sanitised.

---

## Do this once, before recording

```bash
cd ~/Documents/CRIS/incidentlens
source .venv/bin/activate
```

**This is the step that was missing.** Without it the shell has no `incidentlens`
command and prints nothing at all. After it, your prompt shows `(.venv)`.

Check it worked — this must print a path:

```bash
which incidentlens
```

Then warm everything up so nothing is slow on camera:

```bash
incidentlens analyze --config demo/model-id-typo/incidentlens.config.json --analysis-only
open https://incidentlens.onrender.com
```

Now clear the screen and start recording.

---

## Segment 1 · 0:00–0:14 · terminal

**Type and run:**
```bash
clear && cat demo/model-id-typo/logs/hary-ai.log
```

**Say:**
> "This is what you get at two in the morning. A few thousand log lines from a
> service you did not write. Somewhere in here something failed — and nothing here
> tells you *which function*."

---

## Segment 2 · 0:14–0:32 · browser — the home page

**Show:** `https://incidentlens.onrender.com`, starting at the very top.

You asked which replay: **the dropdown is already on the right one.** It reads
*"Hary assistant silently truncating answers after a prompt-template rollout"* —
that is Part 3, the silent-degradation incident, and it is the one you want.

1. Let the hero sit on screen for ~4 seconds (the headline and "Built with" panel)
2. Click the blue **Reconstruct incident** button
3. Scroll down so the animated service graph is visible and let it play

**Say:**
> "IncidentLens reads that log file and the service's source tree. No agents, no
> instrumentation — a log file is the whole integration. It reconstructs what
> failed, how it spread, and what it did to customers, and every single claim
> points back at the line that supports it."

*That animation is drawn live in the browser, not a video. Don't call it a video.*

---

## Segment 3 · 0:32–1:02 · same terminal

**Type and run:**
```bash
clear && incidentlens analyze --config demo/model-id-typo/incidentlens.config.json --analysis-only
```

You will get, in under a second:

```
incident:  INC-20260723-e7cbd6 — hary-frontend failures after hary-ai change
evidence:  18 items
module:    hary.models.llm_factory
candidate: hary.models.llm_factory.get_llm_for_tier  (static, not a stack frame)
```

**Say:**
> "This is running against raw log files on disk — not a fixture. Eighteen pieces
> of evidence. It names the origin service, the propagation chain, and the module
> the failure was logged in. Then it uses a static call graph to offer a candidate
> function — and it labels that a *candidate*, not a stack frame, because a static
> graph cannot prove which line executed. The tool will not claim more than it can
> show you."

*Point at the word `candidate` on screen while you say it. This is your credibility
beat — do not rush it.*

---

## Segment 4 · 1:02–1:38 · the replay excerpt

**Show:** open the pre-cut clip full screen and let it play.

```bash
open demo/gallery/agentic-retry-exhaustion/excerpt-35s.mp4
```

Press space to play, then talk over it. Its own narration will be underneath you —
that is fine, it is your product's voice. Mute your Mac first if it distracts you.

**Say:**
> "The same analysis renders as a narrated replay — the architecture, then the
> failing module, then the function itself.
>
> This one is the interesting failure. Nothing returned a five-hundred. HTTP
> success held at ninety-nine point six percent, so every availability dashboard
> stayed green. But salvaged partial answers went from zero point three percent to
> forty-four. Users were getting truncated replies from an assistant that looked
> perfectly healthy. It still lands on the exact method: AgentNode dot call."

---

## Segment 5 · 1:38–2:06 · browser then terminal

**Show, part A —** go to `https://incidentlens.onrender.com/gallery`.
On the **Part 3 · Agentic Retry Exhaustion** card, click **Verify provenance**.
A panel opens showing `narration steps 14`, `provider openai-tts`,
`model gpt-4o-mini-tts`, and the canonical hash.

**Show, part B —** switch back to the terminal and run:

```bash
clear && python -c "from pathlib import Path; from genblaze_core.media import Mp4Handler; m = Mp4Handler().extract(Path('demo/gallery/agentic-retry-exhaustion/replay.mp4')); print(m.verify(), m.canonical_hash)"
```

It prints `True` and a hash.

**Say:**
> "Genblaze orchestrates the narration. Fourteen text-to-speech steps, each one
> recorded with its provider, its model, and a hash of the audio it produced. And
> the manifest is embedded inside the MP4 itself — so I can pull it back out of the
> video file and check it. True. That is the provenance travelling with the media,
> not alongside it."

---

## Segment 6 · 2:06–2:30 · browser — Backblaze

**Show:** your Backblaze bucket browser with the folder list visible
(`Hary_Part1-…`, `Hary_Part2-…`, `Hary_Part3-…`, `Other-Architectures/`).

Then open this in a new tab so a judge sees it is genuinely public:

```
https://s3.us-east-005.backblazeb2.com/Hackproject/incidents.jsonl
```

**Say:**
> "Backblaze B2 is the library the app reads from, not somewhere it dumped files.
> One folder per incident — the replay, a poster, the analysis, the briefing, the
> call graph, the manifests. A JSONL catalog the gallery loads at page load. And
> the provenance manifests sit under Object Lock, so the record of what produced
> this film cannot be quietly rewritten while the retention holds."

---

## Segment 7 · 2:30–2:45 · browser — the close

**Show:** back on the home page, in the reconstructed incident, click the
**Root cause** tab. Scroll to the row showing `unknown` with confidence `0.00`,
and the missing-evidence list.

**Say:**
> "And when the telemetry cannot settle something, it says so — unknown, confidence
> zero, and here is the evidence I would need. Every incident tool will tell you a
> root cause. This one tells you when it does not know."

*Hold still for two seconds. Stop recording.*

---

## Then

Put the seven files in `~/Desktop/incidentlens-takes/` named `1.mov` … `7.mov`, and:

```bash
./scripts/assemble_video.sh
```

It stitches, matches the audio levels, and tells you the total length.

---

## If something goes wrong on camera

- **No output from a command** → you forgot `source .venv/bin/activate`
- **The page is slow** → you did not warm it; load it once before recording
- **You fluff a line** → stop, re-record *that segment only*, overwrite the file
