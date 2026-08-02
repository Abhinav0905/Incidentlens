#!/bin/zsh
# Bring a recorded demo under the three-minute limit without re-recording.
#
#   ./scripts/fit_under_3min.sh ~/Desktop/my-recording.mov
#
# Two passes, gentlest first:
#   1. Cap dead air. Natural pauses stay, but anything over ~0.45s is shortened.
#      This is invisible to a viewer and usually recovers 10-25 seconds.
#   2. Only if still over, nudge the tempo. atempo preserves pitch, so you do not
#      sound like a chipmunk; up to about 1.12x is imperceptible for speech.
#
# Output: <input>-under3.mp4, and it tells you the final duration.

set -euo pipefail

IN="${1:?usage: fit_under_3min.sh <recording.mov|mp4>}"
[[ -f "$IN" ]] || { echo "No such file: $IN"; exit 1; }

TARGET=176          # 2:56 — leaves margin under the 180s limit
OUT="${IN:r}-under3.mp4"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

dur() { ffprobe -v error -show_entries format=duration -of default=nw=1:nk=1 "$1"; }

start=$(dur "$IN")
printf "input: %.1fs (%d:%02d)\n" "$start" $((${start%.*}/60)) $((${start%.*}%60))

if (( $(python3 -c "print(1 if $start < $TARGET else 0)") )); then
  echo "Already under the limit — nothing to do."
  exit 0
fi

# ---- pass 1: cap dead air -------------------------------------------------
echo "\nPass 1 — shortening long pauses..."
ffmpeg -hide_banner -loglevel error -y -i "$IN" \
  -af "silenceremove=stop_periods=-1:stop_duration=0.45:stop_threshold=-38dB,aresample=48000" \
  -c:v libx264 -preset medium -crf 20 -c:a aac -b:a 192k \
  "$WORK/p1.mp4" 2>/dev/null || cp "$IN" "$WORK/p1.mp4"

# silenceremove trims audio only; the video must be re-timed to match or A/V drifts.
a=$(dur "$WORK/p1.mp4")
ratio=$(python3 -c "print(f'{$start/$a:.6f}')")
ffmpeg -hide_banner -loglevel error -y -i "$WORK/p1.mp4" \
  -filter:v "setpts=PTS/$ratio" -c:v libx264 -preset medium -crf 20 -c:a copy \
  "$WORK/p1s.mp4"

after1=$(dur "$WORK/p1s.mp4")
printf "  %.1fs -> %.1fs  (recovered %.1fs of dead air)\n" "$start" "$after1" \
  "$(python3 -c "print(f'{$start-$after1:.1f}')")"

if (( $(python3 -c "print(1 if $after1 < $TARGET else 0)") )); then
  ffmpeg -hide_banner -loglevel error -y -i "$WORK/p1s.mp4" -c copy -movflags +faststart "$OUT"
else
  # ---- pass 2: gentle tempo ----------------------------------------------
  tempo=$(python3 -c "print(f'{max(1.0, $after1/$TARGET):.4f}')")
  printf "\nPass 2 — tempo x%s (pitch preserved)...\n" "$tempo"
  if (( $(python3 -c "print(1 if $tempo > 1.15 else 0)") )); then
    echo "  ⚠️  That needs more than 1.15x, which starts to sound rushed."
    echo "     Consider cutting a segment instead. Continuing anyway."
  fi
  ffmpeg -hide_banner -loglevel error -y -i "$WORK/p1s.mp4" \
    -filter:v "setpts=PTS/$tempo" -filter:a "atempo=$tempo" \
    -c:v libx264 -preset medium -crf 20 -c:a aac -b:a 192k \
    -movflags +faststart "$OUT"
fi

final=$(dur "$OUT")
printf "\nDone: %s\n" "$OUT"
printf "  %.1fs (%d:%02d)  " "$final" $((${final%.*}/60)) $((${final%.*}%60))
python3 -c "
f = $final
print('✓ under the limit with %.0fs to spare' % (180 - f) if f < 180 else '✗ STILL OVER — cut a segment')
"
echo "\n  Watch it once before uploading — check the audio still sounds natural."
