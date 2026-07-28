#!/bin/zsh
# Stitch the recorded segments into the submission video.
#
# Record each segment separately (see VIDEO_SCRIPT.md), drop them in
# ~/Desktop/incidentlens-takes/ named 1.mov, 2.mov, 3.mov ... and run this.
# Numeric order is the edit order. Anything not numbered is ignored.
#
#   ./scripts/assemble_video.sh
#
# Output: ~/Desktop/incidentlens-submission.mp4  (1080p, H.264 + AAC)
#
# Re-record any single segment and run this again — the others are untouched.

set -euo pipefail

TAKES="${1:-$HOME/Desktop/incidentlens-takes}"
OUT="${2:-$HOME/Desktop/incidentlens-submission.mp4}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

if [[ ! -d "$TAKES" ]]; then
  echo "No takes folder at $TAKES"
  echo "Create it and put your segments in as 1.mov, 2.mov, ..."
  exit 1
fi

# Numeric sort, so 2.mov comes before 10.mov.
segments=($(ls "$TAKES" | grep -E '^[0-9]+\.(mov|mp4|m4v)$' | sort -n))
if (( ${#segments[@]} == 0 )); then
  echo "No numbered segments found in $TAKES"
  exit 1
fi

echo "Segments, in order:"
total=0
for s in $segments; do
  d=$(ffprobe -v error -show_entries format=duration -of default=nw=1:nk=1 "$TAKES/$s")
  printf "  %-10s %6.1fs\n" "$s" "$d"
  total=$(python3 -c "print($total + $d)")
done
printf "  %-10s %6.1fs\n" "TOTAL" "$total"
python3 - "$total" <<'PY'
import sys
t = float(sys.argv[1])
if t >= 180:
    print(f"\n  ⚠️  {t:.0f}s is over the 3:00 limit — trim before uploading.")
elif t > 170:
    print(f"\n  ⚠️  {t:.0f}s leaves almost no margin. Aim for 165s or less.")
else:
    print(f"\n  ✓ {t:.0f}s — inside the limit with {180 - t:.0f}s spare.")
PY

# Normalise every segment to the same codec, size, frame rate and audio layout.
# Concat refuses to join clips that disagree on any of those.
echo "\nNormalising..."
i=1
for s in $segments; do
  ffmpeg -hide_banner -loglevel error -y -i "$TAKES/$s" \
    -vf "scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2,fps=30,format=yuv420p" \
    -c:v libx264 -preset medium -crf 20 \
    -af "aresample=48000,loudnorm=I=-16:TP=-1.5:LRA=11" \
    -c:a aac -b:a 192k -ar 48000 -ac 2 \
    "$WORK/$(printf '%03d' $i).mp4"
  printf "  %s ✓\n" "$s"
  i=$((i+1))
done

: > "$WORK/list.txt"
for f in "$WORK"/[0-9][0-9][0-9].mp4; do
  echo "file '$f'" >> "$WORK/list.txt"
done

echo "\nJoining..."
ffmpeg -hide_banner -loglevel error -y -f concat -safe 0 -i "$WORK/list.txt" \
  -c copy -movflags +faststart "$OUT"

echo "\nDone: $OUT"
ffprobe -v error -show_entries format=duration -show_entries stream=codec_name,width,height \
  -of default=noprint_wrappers=1 "$OUT" | sed 's/^/  /'
echo "\n  Upload this to YouTube as PUBLIC (not Unlisted)."
