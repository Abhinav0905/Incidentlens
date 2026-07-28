# Demo video — shot list and voice-over

**Target 2:45. Hard limit 3:00.** Judges are not required to watch past three
minutes, so nothing load-bearing goes after 2:30.

Word counts assume ~155 words/minute. Read a little slower than feels natural —
recorded speech always plays back faster than it felt.

---

## Before you press record

- [ ] Warm the app: open https://incidentlens.onrender.com and let it load once
- [ ] Open these tabs in order, so you only ever switch right:
      1. a terminal in `~/Documents/CRIS/incidentlens`
      2. `https://incidentlens.onrender.com`
      3. `https://incidentlens.onrender.com/gallery`
      4. the Backblaze bucket browser
- [ ] Pre-run the analyze command **once** so the filesystem cache is warm
- [ ] Terminal font at 16pt+; hide bookmarks; close notifications
- [ ] Screen at 1920×1080, browser zoom 100%
- [ ] `demo/gallery/agentic-retry-exhaustion/excerpt-35s.mp4` ready to cut in

---

## 0:00 – 0:14 · The problem  (35 words)

**Show:** a terminal tailing raw log lines, scrolling fast. No UI yet.

> "This is what you get at two in the morning. A few thousand log lines from a
> service you did not write. Somewhere in here something failed — and nothing
> here tells you *which function*."

*Let the logs scroll for a beat after you stop talking.*

---

## 0:14 – 0:32 · The claim, and the app  (44 words)

**Show:** switch to the live URL. The hero, then scroll so the replay is visible.

> "IncidentLens reads that log file and the service's source tree. No agents, no
> instrumentation — a log file is the whole integration. It reconstructs what
> failed, how it spread, and what it did to customers, and every single claim
> points back at the line that supports it."

---

## 0:32 – 1:02 · Reconstruct from raw logs, live  (72 words)

**Show:** the terminal. Type and run:

```bash
incidentlens analyze --config demo/model-id-typo/incidentlens.config.json --analysis-only
```

It returns in under a second. Let the output sit on screen.

> "This is running against raw log files on disk — not a fixture. Eighteen pieces
> of evidence. It names the origin service, the propagation chain, and the module
> the failure was logged in. Then it uses a static call graph to offer a candidate
> function — and it labels that a *candidate*, not a stack frame, because a static
> graph cannot prove which line executed. The tool will not claim more than it can
> show you."

*This is your credibility beat. Do not rush it.*

---

## 1:02 – 1:38 · The replay, and the hard case  (84 words)

**Show:** cut to `excerpt-35s.mp4`. Let it play with its own audio ducked under you.

> "The same analysis renders as a narrated replay — the architecture, then the
> failing module, then the function itself.
>
> This one is the interesting failure. Nothing returned a five-hundred. HTTP
> success held at ninety-nine point six percent, so every availability dashboard
> stayed green. But salvaged partial answers went from zero point three percent to
> forty-four. Users were getting truncated replies from an assistant that looked
> perfectly healthy. It still lands on the exact method: AgentNode dot call."

---

## 1:38 – 2:06 · Genblaze  (70 words)

**Show:** the gallery, click **Verify provenance** on Part 3. Then the terminal:

```bash
python -c "from pathlib import Path; from genblaze_core.media import Mp4Handler; \
m = Mp4Handler().extract(Path('demo/gallery/agentic-retry-exhaustion/replay.mp4')); \
print(m.verify(), m.canonical_hash)"
```

> "Genblaze orchestrates the narration. Fourteen text-to-speech steps, each one
> recorded with its provider, its model, and a hash of the audio it produced. And
> the manifest is embedded inside the MP4 itself — so I can pull it back out of the
> video file and check it. True. That is the provenance travelling with the media,
> not alongside it."

---

## 2:06 – 2:30 · Backblaze B2  (68 words)

**Show:** the bucket browser — the named folders. Then the public catalog URL in a
signed-out window. Then the retention metadata on a manifest.

> "Backblaze B2 is the library the app reads from, not somewhere it dumped files.
> One folder per incident — the replay, a poster, the analysis, the briefing, the
> call graph, the manifests. A JSONL catalog the gallery loads at page load. And
> the provenance manifests sit under Object Lock, so the record of what produced
> this film cannot be quietly rewritten while the retention holds."

---

## 2:30 – 2:45 · Close on the refusal  (44 words)

**Show:** the Root cause tab, with the `unknown — 0.00` row and the missing-evidence
list visible.

> "And when the telemetry cannot settle something, it says so — unknown, confidence
> zero, and here is the evidence I would need. Every incident tool will tell you a
> root cause. This one tells you when it does not know."

*Hold on the `unknown` row for two seconds. End.*

---

## What to say if you overrun

Cut in this order:

1. The replay excerpt, 35s → 25s (start it later, not earlier)
2. The B2 folder browse — keep the catalog URL and the retention, drop the folders
3. The first 4 seconds of log scroll

**Never cut:** the `--analysis-only` run, the manifest extraction, or the closing
`unknown` row. Those three are the submission.

---

## Delivery notes

- **Say "candidate function", never "the method that raised."** The tool is careful
  about this on screen; sound careful about it too.
- **Do not say "multi-provider Genblaze orchestration."** It is one provider,
  `openai-tts`. Say "Genblaze orchestrates the narration" — true and sufficient.
- Do not say the manifest "verifies against the analysis". It records a digest of
  the analysis; `verify()` checks the manifest's own integrity.
- Say **"synthetic scenarios"** at least once if you show the bundled ones.
- Pause a full second at each cut. Judges are scrubbing, not watching.

---

## Recording

`Cmd+Shift+5` on macOS, or OBS if you want a webcam bubble (you do not need one).

Record in **passes**, not one take: each segment separately, then assemble.
A fluffed line then costs you 20 seconds, not the whole video.

Export **1080p, H.264**. Upload to YouTube as **Public** (not Unlisted — the rules
say publicly visible). Title it:

> IncidentLens — verifiable incident reconstruction on Backblaze B2 + Genblaze
