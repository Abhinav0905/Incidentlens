# IncidentLens Studio

Studio turns an incident analysis into a narrated MP4. Since 0.4.0 the
default style is **cinematic**: one continuous 3D shot of your architecture,
not a slideshow. The camera glides to whatever the narration is talking
about; services are extruded slabs on a floor grid that recolor, lift and
pulse through their state changes; request traffic runs as particles along
the dependency edges — and reverses, heats up and accelerates on edges the
failure is crossing; state changes ripple shockwaves across the floor; the
hot elements bloom. The wall-clock in the corner ticks between event
timestamps as the camera travels.

The split that matters is unchanged. The **visuals are deterministic** — every
node state, edge ignition and caption is computed from the same analysis the
web UI replays, so the video cannot show a failure path the analysis didn't
find, and identical inputs render identical videos. The **narration is the
generative layer** — an LLM writes the incident story and a text-to-speech
voice reads it. The rule the whole project runs on holds on the audio track:
an inferred cause is spoken as what the evidence points to, never as settled
fact.

Rendering is pure Python (numpy + Pillow; DejaVu fonts ship in the package)
streamed straight into ffmpeg as raw RGB. No browser, no GPU, no node_modules.

## Install

```bash
pip install "incidentlens[studio]"
```

The optional Genblaze workflow requires Python 3.11 or newer:

```bash
pip install "incidentlens[studio,genblaze]"
```

Core IncidentLens and the ordinary Studio renderer continue to support Python
3.10. Installing the `genblaze` extra adds the Genblaze core, OpenAI, and S3
packages; it is not required for direct OpenAI, local voices, or legacy B2
upload.

One system tool is required; a second is only a fallback:

- **ffmpeg** — required, for encoding and audio muxing
- **espeak-ng** — only for the free robotic offline voice

```bash
# macOS
brew install ffmpeg espeak-ng
# Debian / Ubuntu
sudo apt-get install ffmpeg espeak-ng
```

## Render one

```bash
incidentlens studio gateway-auth-rejection --out incident.mp4
```

With `OPENAI_API_KEY` configured, `auto` uses OpenAI neural TTS; otherwise it
falls back to local TTS and finally timed silence. Try the other scenarios
(`checkout-secret-rotation`,
`cache-stampede`) to watch the same code shoot genuinely different films.

Options:

- `--profile high` (default) 1920x1080 @ 30 fps, supersampled ·
  `--profile preview` 1280x720 @ 24 fps for fast iteration ·
  `--profile ultra` 2560x1440 @ 30 fps
- `--style cinematic` (default) · `--style classic` for the original
  per-beat stills (needs cairosvg)
- `--voice auto | genblaze | openai | offline | piper | elevenlabs | silent`
- `--narration template | llm`
- `--fps N` overrides the profile's frame rate
- `--intro-video PATH` prepends an optional short cinematic bumper while the
  evidence replay remains deterministic
- `--publish-genblaze` writes local Genblaze provenance as a sidecar manifest
- `--upload-genblaze-b2` publishes the original video and manifest to B2
  through Genblaze
- `--upload-b2` keeps the legacy direct boto3 upload available

## Polished OpenAI narration

OpenAI's speech model reads either deterministic or model-written narration.
The default `marin` voice is prompted to sound warm, measured, reassuring and
professional rather than urgent or theatrical.
See the official [OpenAI text-to-speech guide](https://developers.openai.com/api/docs/guides/text-to-speech)
for the current model, voices, and disclosure requirements.

```bash
export OPENAI_API_KEY=...
export INCIDENTLENS_OPENAI_VOICE=marin

incidentlens studio gateway-auth-rejection \
  --narration template \
  --voice openai \
  --out incident.mp4
```

OpenAI TTS defaults to `gpt-4o-mini-tts`. Override the voice, model, or delivery
prompt with `INCIDENTLENS_OPENAI_VOICE`,
`INCIDENTLENS_OPENAI_TTS_MODEL`, and
`INCIDENTLENS_OPENAI_VOICE_INSTRUCTIONS`. A dedicated
`INCIDENTLENS_OPENAI_TTS_BASE_URL` is available for speech-compatible
gateways; the chat narration gateway is never reused implicitly.

The rendered HUD explicitly discloses that the voice is AI-generated.

You can still use `--narration llm` for a generated script. Its safety prompt
constrains every line to the evidence record, and it falls back to template
narration if the model call fails.

## Genblaze narration and provenance

Genblaze is an explicit option rather than the `auto` default. That keeps
existing OpenAI narration and its `marin` voice unchanged while providing a
provider-orchestrated path for runs that need media provenance:

```bash
python --version  # 3.11+
pip install "incidentlens[studio,genblaze]"

export OPENAI_API_KEY=...
export INCIDENTLENS_GENBLAZE_VOICE=coral

incidentlens studio gateway-auth-rejection \
  --voice genblaze \
  --publish-genblaze \
  --out incident.mp4
```

`--voice genblaze` sends all narration beats through one
`OpenAITTSProvider` pipeline using `gpt-4o-mini-tts`. Each WAV is hashed before
the run is finalized, every step classifies its prompt visibility as
`private`, and the run manifest captures provider, model, parameters, retries,
timing, and SHA-256 declarations. `private` is a metadata classification, not
encryption or automatic redaction: the canonical narration sidecar still
contains the spoken narration text in clear text. Treat it as sensitive
incident data and do not publish it without review. The calm default is
`coral`: Genblaze 0.3.x currently rejects the newer OpenAI `marin` and `cedar`
voice names. Use direct `--voice openai` when either newer voice or
`INCIDENTLENS_OPENAI_TTS_BASE_URL` is required.

Local publication produces:

```text
incident.mp4
incident.genblaze.json
incident.narration.genblaze.json   # only with --voice genblaze
```

The final-video manifest is a sidecar on this local-only path. IncidentLens
does not embed it into `incident.mp4`, because embedding changes the
container's bytes after its SHA-256 has been recorded. The sidecar therefore
continues to describe the exact local MP4 bytes.

The narration manifest has a narrower lifetime. It contains valid hashes for
the individual beat WAVs, but their `file://` URLs point into the renderer's
temporary working directory. The WAVs are removed after the render, so the
manifest's canonical hash and SHA declarations remain verifiable while its
assets cannot be fetched later. Persist the narration audio separately before
claiming byte-level, fetch-based verification of those WAVs.

## Long-form films and generative video

IncidentLens itself can render a five-minute or longer film because its
duration follows the narration and evidence timeline. As of July 2026, the
OpenAI Sora API generates clips up to 20 seconds; extensions can carry one
sequence to 120 seconds, but there is no single five-minute generation.
`sora-2-pro` is the higher-fidelity choice for a polished opener, while
`sora-2` is the faster iteration model. Assemble longer work editorially and
do not use generated footage as the source of a five-minute technical replay:
it can distort service names, topology, and code labels.

Use a generated clip only as a reusable opener:

```bash
incidentlens analyze --config incidentlens.config.json \
  --voice openai \
  --intro-video demo/incidentlens-sora-opener-branded.mp4
```

The opener can come from Sora, Veo, Runway, or an ordinary motion-graphics
tool. It is normalized to the replay's size and frame rate, its audio is
discarded, and the evidence-backed film follows it.

## From Python

```python
from incidentlens.studio import produce_incident_video

result = produce_incident_video(
    "gateway-auth-rejection",
    "incident.mp4",
    voice="openai",            # also "genblaze" on Python 3.11+
    narration_mode="llm",      # "template" | "llm"
    style="cinematic",         # "cinematic" | "classic"
    profile="high",            # "high" | "preview" | "ultra"
    intro_video="assets/incidentlens-opener.mp4",  # optional
    publish_genblaze=False,    # local sidecar provenance
    upload_genblaze_b2=False,  # Genblaze ObjectStorageSink -> B2
)
print(result.path, result.incident_id, result.beats)
```

For an analysis you built yourself (live logs, custom connector), use
`produce_video_from_analysis(analysis, architecture, out_path, ...)` — the
watch and analyze commands go through exactly this function. Pass
`code_graphs={"service-name": code_graph}` to enable the full blueprint acts
for a custom caller. The CLI loads `incidentlens.codegraph.json`
automatically when it sits beside the architecture/config file.

Voices implement one method, so a new provider is a small class:

```python
from pathlib import Path

class MyVoice:
    def synthesize(self, text: str, out_path: str | Path) -> float:
        # write audio to out_path, return its duration in seconds
        ...
```

## How the movie is built

1. Run the analysis (the deterministic engine) and build narration beats —
   one per timeline event, plus an intro and two closing beats.
2. Synthesize audio per beat; each beat's on-screen hold follows its audio
   length, and every clip is placed at its exact start time on one mixed
   narration track.
3. Derive the global timeline: when each node changes state, when each
   propagation edge ignites, what the clock reads at any second.
4. Plan the camera: an establishing dolly-in for the intro, a framed shot per
   beat fitted around the services that beat talks about (kept clear of the
   caption card and header), a pull-back for the findings. Between keys the
   camera glides on an ease-in-out-quint curve; during holds it keeps a slow
   push-in so the frame always breathes.
5. When the origin service's internals and repository code graph are known,
   plan a three-level dive: the traced request stages; a complete
   package-grouped module blueprint with the incident overlaid; and a focused
   function blueprint with methods nested inside classes and modules. The 2D
   blueprint camera establishes the whole design before zooming to the
   evidence-backed locus. Teal distinguishes logged/traced or verified
   structure, red is reserved for log-confirmed failure, amber marks
   structural risk or static attribution, and dim nodes have no runtime proof.
   If no code graph was scanned, compact dependency/caller-callee views remain
   available. Short refocus transitions separate the levels before the camera
   returns to the macro propagation story.
6. Render every frame — floor grid, shadows, edges, particles, shockwaves,
   depth-sorted slabs, labels, bloom pass, HUD — and stream raw RGB into
   ffmpeg, which muxes the narration track in.

Everything on screen traces back to the analysis. The captions carry the
evidence IDs, so the video is legible even on mute.

## Performance

Pure software rendering costs CPU: roughly 0.1–0.7 s per frame depending on
profile and machine. A 90-second incident is typically a few minutes at
`preview` and 10–25 minutes at `high`. Use `preview` while iterating and
`high` for the version you post in the incident channel.

## Upload to Backblaze B2

The Genblaze path stores the original rendered MP4 and its canonical manifest
through `ObjectStorageSink` with hierarchical keys:

```bash
export B2_BUCKET=my-bucket
export B2_REGION=us-west-004
export B2_KEY_ID=...
export B2_APP_KEY=...

incidentlens studio gateway-auth-rejection \
  --voice genblaze \
  --upload-genblaze-b2 \
  --out incident.mp4
```

`B2_PUBLIC_URL_BASE` may be set for an intentionally public bucket, and
`INCIDENTLENS_B2_PREFIX` changes the default `incidentlens` object prefix.
Without a public URL base, Genblaze returns a durable, credential-free object
URL; it does not persist expiring credentials in the manifest, so that URL can
return 403 for a private bucket. Generate a fresh read-time presigned URL at
the application boundary when private access is required.

The Genblaze path also accepts the older IncidentLens variable names:

- `B2_APPLICATION_KEY` is used when `B2_APP_KEY` is absent.
- The region is derived from a standard
  `B2_ENDPOINT_URL=https://s3.<region>.backblazeb2.com` when `B2_REGION` is
  absent.

If both secret variables are set, their values must match or publication
fails. `B2_REGION` takes precedence over a region derived from
`B2_ENDPOINT_URL`.

The B2 object is uploaded before local manifest embedding. As a result, the
canonical manifest and its asset SHA describe the original bytes stored in
B2. IncidentLens then embeds the same manifest into the local `incident.mp4`
for convenient extraction; that local embedded copy is intentionally
byte-different from the B2 object. The local `.genblaze.json` sidecar points
at the durable B2 asset and manifest URI. If Genblaze cannot modify the MP4
container, it safely reports a sidecar fallback instead; IncidentLens keeps
that verifiable result rather than treating a valid fallback as an inline
embed.

The pre-existing direct upload remains available and keeps its old
configuration and behavior:

```bash
export B2_BUCKET=my-bucket
export B2_ENDPOINT_URL=https://s3.us-west-004.backblazeb2.com
export B2_KEY_ID=...
export B2_APPLICATION_KEY=...

incidentlens studio gateway-auth-rejection --upload-b2
```

This legacy command uploads the MP4 under its incident ID and prints a
time-limited presigned URL. `--upload-b2` and `--upload-genblaze-b2` are
mutually exclusive.

## Limits

- The offline voice is robotic by design; use OpenAI TTS for public demos.
- Software rendering is CPU-bound; there is no GPU path (that's what keeps
  the dependency list at "pip install + ffmpeg").
- Genblaze publication and configuration are covered by local and mocked
  tests; a real B2 bucket still requires an environment-specific live smoke
  test.
