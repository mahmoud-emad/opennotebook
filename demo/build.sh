#!/usr/bin/env bash
# Joins the clips webreel recorded into demo/out/demo.mp4 (1080p, for the
# site) and demo/out/demo.gif (for the README). Run by `make demo`, after
# `webreel record`.
set -euo pipefail

cd "$(dirname "$0")/out"
FFMPEG="${FFMPEG_PATH:-$(command -v ffmpeg || echo "$HOME/.webreel/bin/ffmpeg")}"
FADE=0.4

# Each clip is trimmed to the part worth watching: start, length (seconds).
A_START=0.0;  A_LEN=7.0     # the collection and its cost estimate
B_START=0.6;  B_LEN=8.0     # a narrated deck, playing
C_START=0.4;  C_LEN=17.0    # a topic asked, answered with citations

AB_OFFSET=$(python3 -c "print($A_LEN - $FADE)")
ABC_OFFSET=$(python3 -c "print($A_LEN + $B_LEN - $FADE - $FADE)")

"$FFMPEG" -v error -y -i 1-collection.mp4 -i 2-slides.mp4 -i 3-map.mp4 -filter_complex "
  [0:v]trim=start=$A_START:duration=$A_LEN,setpts=PTS-STARTPTS[a];
  [1:v]trim=start=$B_START:duration=$B_LEN,setpts=PTS-STARTPTS[b];
  [2:v]trim=start=$C_START:duration=$C_LEN,setpts=PTS-STARTPTS[c];
  [a][b]xfade=transition=fade:duration=$FADE:offset=$AB_OFFSET[ab];
  [ab][c]xfade=transition=fade:duration=$FADE:offset=$ABC_OFFSET,fps=30,format=yuv420p[v]
" -map "[v]" joined.mp4

# The site's video: 1080p, scaled up from the recording with Lanczos.
"$FFMPEG" -v error -y -i joined.mp4 -vf "scale=1920:1080:flags=lanczos" \
  -c:v libx264 -preset slow -crf 20 -movflags +faststart -an demo.mp4

# The README's GIF: 1000 px wide, 12 fps, one palette for the whole clip.
"$FFMPEG" -v error -y -i joined.mp4 \
  -vf "fps=12,scale=1000:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=128:stats_mode=full[p];[b][p]paletteuse=dither=sierra2_4a" \
  -loop 0 demo.gif

rm -f joined.mp4
ls -lh demo.mp4 demo.gif
