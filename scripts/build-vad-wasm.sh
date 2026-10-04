#!/usr/bin/env bash
#
# Build opennotebook_vad for the browser and install it where the server serves
# it from.
#
#   scripts/build-vad-wasm.sh
#
# It builds into its own target dir under the repo's target/, so the wasm
# build, with its own triple and features, stays apart from the workspace's
# native artifacts and can be deleted on its own.
#
# Installs to `vad.wasm` in the data directory, resolved the way the server and
# crates/opennotebook_ui/install.sh resolve it: OPENNOTEBOOK_HOME, else the
# platform's application-data directory.
#
# A box that never runs this still works: /api/session/vad.wasm answers 404 and
# player.html falls back to the peak threshold it shipped with.
set -Eeuo pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"
TARGET_DIR="$HERE/target/wasm32-vad"
if [[ -n "${OPENNOTEBOOK_HOME:-}" ]]; then
  DATA="$OPENNOTEBOOK_HOME"
elif [[ "$(uname)" == "Darwin" ]]; then
  DATA="$HOME/Library/Application Support/opennotebook"
else
  DATA="${XDG_DATA_HOME:-$HOME/.local/share}/opennotebook"
fi
DEST="$DATA/vad.wasm"

say() { printf '\033[1m== %s\033[0m\n' "$*"; }

rustup target list --installed 2>/dev/null | grep -qx wasm32-unknown-unknown \
  || { echo "wasm32-unknown-unknown is not installed: rustup target add wasm32-unknown-unknown" >&2; exit 1; }

say "building"
# --no-default-features drops the `wav` feature, and with it hound: a container
# parser has no business in an audio thread, and the worklet is handed decoded
# f32 frames by the browser anyway.
cd "$HERE"
CARGO_TARGET_DIR="$TARGET_DIR" cargo build \
  -p opennotebook_vad \
  --target wasm32-unknown-unknown \
  --release \
  --no-default-features

WASM="$TARGET_DIR/wasm32-unknown-unknown/release/opennotebook_vad.wasm"
[ -f "$WASM" ] || { echo "no artifact at $WASM" >&2; exit 1; }

say "installing"
mkdir -p "$(dirname "$DEST")"
cp "$WASM" "$DEST"
printf '   %s  ->  %s\n' "$(du -h "$WASM" | cut -f1)" "$DEST"

# The six exports the worklet calls by name. A rename that compiles and then
# breaks the page at run time is exactly the failure this catches.
say "checking exports"
python3 - "$DEST" <<'PY'
import sys
d = open(sys.argv[1], 'rb').read()
assert d[:4] == b'\0asm', "not a wasm module"
def uleb(b, i):
    r = s = 0
    while True:
        x = b[i]; i += 1; r |= (x & 0x7f) << s; s += 7
        if not x & 0x80: return r, i
found, i = set(), 8
while i < len(d):
    sid = d[i]; i += 1
    size, i = uleb(d, i)
    if sid == 7:
        n, j = uleb(d, i)
        for _ in range(n):
            ln, j = uleb(d, j); found.add(d[j:j+ln].decode()); j += ln
            j += 1; _, j = uleb(d, j)
    i += size
want = {"memory", "vad_new", "vad_free", "vad_push", "vad_reset", "vad_alloc", "vad_dealloc"}
missing = want - found
assert not missing, f"missing exports: {sorted(missing)}"
print(f"   all {len(want)} exports present")
PY
