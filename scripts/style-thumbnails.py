#!/usr/bin/env python3
"""Render the style picker's thumbnails from the studio's own style kits.

Each thumbnail is the sample slide the running studio serves for a style at
/api/session/style_sample?style=<id> — the same kit (fonts, colours, textures,
components) every slide of a session in that style is drawn with. It is
screenshotted in headless Chromium (Playwright) at twice the picker's size and
written to crates/opennotebook_ui/assets/styles/<id>.jpg.

Needs the opennotebook server (this build) running at OPENNOTEBOOK_URL
(default http://127.0.0.1:7878), and Playwright for Python with Chromium:

    pip install playwright && playwright install chromium

Free: no model is called. Run from anywhere:

    scripts/style-thumbnails.py            # every style the studio ships
    scripts/style-thumbnails.py clay       # just these
"""

import html as htmllib
import os
import pathlib
import sys
import urllib.parse
import urllib.request

from playwright.sync_api import sync_playwright

SERVER = os.environ.get("OPENNOTEBOOK_URL", "http://127.0.0.1:7878").rstrip("/")
REPO = pathlib.Path(__file__).resolve().parent.parent
OUT = REPO / "crates/opennotebook_ui/assets/styles"
STYLES = ["editorial", "professional", "bento", "instructional", "scientific", "sketchnote", "clay", "bricks"]

# The thumbnail's size in the picker, at twice the pixels for sharp screens.
THUMB_W, THUMB_H = 384, 216


def sample(style_id):
    url = f"{SERVER}/api/session/style_sample?style={urllib.parse.quote(style_id)}"
    with urllib.request.urlopen(url, timeout=30) as r:
        return r.read().decode()


def shoot(browser, style_id):
    # The browser does the scaling: a page the size of the thumbnail holding
    # the 1920x1080 slide in a frame scaled to fit.
    slide = htmllib.escape(sample(style_id), quote=True)
    scale = THUMB_W / 1920
    doc = (
        "<!doctype html><html><body style='margin:0;overflow:hidden'>"
        f"<iframe srcdoc=\"{slide}\" style='width:1920px;height:1080px;"
        f"border:0;transform:scale({scale});transform-origin:0 0'></iframe></body></html>")
    page = browser.new_page(viewport={"width": THUMB_W, "height": THUMB_H})
    try:
        page.set_content(doc, wait_until="load", timeout=30000)
        page.wait_for_timeout(3000)  # web fonts
        out = OUT / f"{style_id}.jpg"
        page.screenshot(path=str(out), type="jpeg", quality=90)
    finally:
        page.close()
    return out


def main():
    ids = sys.argv[1:] or STYLES
    OUT.mkdir(parents=True, exist_ok=True)
    failed = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            for sid in ids:
                try:
                    print(f"{sid}: {shoot(browser, sid)}")
                except Exception as e:  # one style failing should not cost the others
                    print(f"{sid}: FAILED — {e}", file=sys.stderr)
                    failed.append(sid)
        finally:
            browser.close()
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
