# The demo

The README's GIF and the site's hero video are recorded from a running studio with [webreel](https://webreel.dev), so they can be made again when the app changes. `make demo` does the recording and joins the clips.

## What you need

- A studio with a provider connected (`make dev`, then the setup tour).
- A collection to film, with two Wikipedia sources and four outputs already made. Making them costs about $0.25. With the studio on `localhost:5173`:

```sh
API=localhost:5173/api
C=$(curl -s -X POST $API/collections -H 'content-type: application/json' -d '{}' | jq -r .id)
curl -s -X POST $API/collections/$C/sources -H 'content-type: application/json' \
  -d '{"kind":"urls","urls":["https://en.wikipedia.org/wiki/Coral_reef","https://en.wikipedia.org/wiki/Kelp_forest"]}'
curl -s -X POST $API/collections/$C/outputs -H 'content-type: application/json' -d '{"kind":"slides","slide_count":6}'
curl -s -X POST $API/collections/$C/outputs -H 'content-type: application/json' -d '{"kind":"audio","audio_format":"deep_dive","audio_length":"shorter"}'
curl -s -X POST $API/collections/$C/mindmaps -H 'content-type: application/json' -d '{}'
curl -s -X POST $API/collections/$C/notes -H 'content-type: application/json' -d '{}'
```

- The slides' session id: `curl -s $API/sessions | jq -r '.[] | select(.kind=="slides") | .id'`.
- Node 24. The first run fetches webreel's own Chrome and ffmpeg into `~/.webreel` (about 400 MB).

## Recording

```sh
DEMO_COLLECTION=<collection id> DEMO_SLIDES=<slides session id> make demo
```

`DEMO_URL` defaults to `http://localhost:5173`. The result is `demo/out/demo.mp4` (1080p, for the site) and `demo/out/demo.gif` (for the README). Copy them to `site/img/demo.mp4` and `docs/img/demo.gif`.

Clear the chat first if you are filming again (`DELETE /api/collections/<id>/chat`), so the question starts fresh.

## Why it is shaped this way

webreel 0.1.4 has three limits that decide how the script is written. The comments in [webreel.config.json](webreel.config.json) explain each one. In short: it cannot type into a field, a recording stops at the first full page load, and CSS zoom makes capture too slow. So the demo is three clips on three pages, every step is a click, and the window is small so the app's text fills the frame. [build.sh](build.sh) trims the clips and joins them.
