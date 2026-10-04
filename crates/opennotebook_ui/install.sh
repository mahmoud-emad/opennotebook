#!/usr/bin/env bash
# Build the bundle and install it where the server serves it: the data
# directory's ui/ (or $OPENNOTEBOOK_UI_DIR), mounted at /ui/.
#
# dx emits ORIGIN-ABSOLUTE asset refs and the bundle is served under /ui/, so the
# refs have to be made relative or the page loads and the app never boots. Two places need it, and the second
# one is easy to miss because index.html looks fixed on its own:
#
#   index.html   src/href="/./assets/x.js"        -> "assets/x.js"
#   the js glue  module_or_path:"/./assets/y.wasm" -> new URL("./y.wasm", import.meta.url)
#
# The JS one is the one that bites: with only index.html fixed, the script loads
# from the right place and then fetches the wasm from the ORIGIN root, which 404s
# with a blank page and nothing in the server log.
set -euo pipefail

crate_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
out="$crate_dir/target/dx/opennotebook_ui/release/web/public"
# The same directory the server resolves: OPENNOTEBOOK_UI_DIR, else
# <data dir>/ui, the data dir being OPENNOTEBOOK_HOME or the platform's.
if [[ -n "${OPENNOTEBOOK_UI_DIR:-}" ]]; then
  www="$OPENNOTEBOOK_UI_DIR"
else
  if [[ -n "${OPENNOTEBOOK_HOME:-}" ]]; then
    data="$OPENNOTEBOOK_HOME"
  elif [[ "$(uname)" == "Darwin" ]]; then
    data="$HOME/Library/Application Support/opennotebook"
  else
    data="${XDG_DATA_HOME:-$HOME/.local/share}/opennotebook"
  fi
  www="$data/ui"
fi

# dx does NOT clean its own output directory between builds, so assets from
# every previous build pile up in it. Copying that whole directory shipped three
# bundles at once, and an index.html cached in a browser kept resolving its OLD
# hashed bundle for as long as the file stayed on disk — the page loaded, worked,
# and silently stayed several versions behind. Clearing it means the deployed
# bundle is exactly the one just built.
rm -rf "$crate_dir/target/dx/opennotebook_ui/release/web"

dx build --release --platform web --base-path ""

rm -rf "$www"
mkdir -p "$www/assets"
cp "$out/assets/"* "$www/assets/"
sed 's#"/\./assets/#"assets/#g' "$out/index.html" > "$www/index.html"

# Relative to the module's own URL. The glue and the wasm are siblings in
# assets/, so "./<file>" is correct wherever the bundle is mounted.
for js in "$www/assets/"*.js; do
  perl -0pi -e 's{module_or_path:"/\./assets/([^"]+)"}{module_or_path:new URL("./$1",import.meta.url)}g' "$js"
done

# A cached index.html pointing at a bundle that no longer exists is a blank
# page, so say what shipped and make a stale deploy obvious at a glance.
echo "installed to $www"
echo "assets now present:"
ls -1 "$www/assets"
grep -oE '(src|href)="[^"]*"' "$www/index.html" | sort -u
