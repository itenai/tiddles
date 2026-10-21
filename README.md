# HiFi Player — Studio Edition

A single-file, browser-based HiFi music player with a brutalist-editorial aesthetic. Streams TIDAL tracks via a self-hosted API, with DASH segment streaming, synced lyrics, queue management, multi-theme appearance, and batch download support.

![Status](https://img.shields.io/badge/status-active-e47757)
![Single File](https://img.shields.io/badge/single--file-HTML-203138)
![License](https://img.shields.io/badge/license-Unlicense-203138)

---

## Features

- **Hi-Res Streaming** — FLAC, Hi-Res FLAC, AAC, and Dolby Atmos playback via MPEG-DASH
- **Smart Buffering** — Segment cache, lookahead prefetch, and next-track preloading for smooth playback
- **Queue System** — Add, reorder, and play tracks; shuffle and repeat modes
- **Search** — Tracks, albums, artists, and playlists
- **Manual Load** — Direct track / album / playlist ID entry with quality override
- **Downloads** — Single track, album, playlist, or full queue as individual files or ZIP
- **Synced Lyrics** — Timestamp-synced lyrics with auto-scroll
- **Floating Now Playing** — Draggable mini-player popup
- **6 Themes** — TIDAL, Stillwater, Plume, Flint, Forage, Cobalt
- **Configurable Queue Layout** — Side panel, slide-out menu (left or right), or disabled
- **Persistent Settings** — Stored in `localStorage`

---

## Quick Start

1. **Clone or download** this repo
2. Open `index.html` in any modern browser
3. Click the **Settings** gear (top-right) to configure:
   - API Base URL (defaults to the public hosted instance)
   - Default audio quality
   - Queue layout & theme
4. Search for something, add to queue, and hit play

> No build step, no dependencies to install. Everything runs client-side. `JSZip` is loaded from CDN for bulk downloads.

---

## Requirements

- A modern browser with **Media Source Extensions (MSE)** support (Chrome, Edge, Firefox, Safari 16+)
- Network access to a compatible TIDAL API backend
- The public demo backend is rate-limited — for heavy use, self-host your own

---

## Configuration

All settings are saved to `localStorage` under the key `hifi_settings`.

| Setting | Options | Default |
|---|---|---|
| API Base URL | Any compatible endpoint | `https://igameten10-hifi-api.hf.space` |
| Quality | Hi-Res, Lossless, High, Low, Atmos | `LOSSLESS` |
| Queue Layout | Side panel, Slide-out, Disabled | `Side panel` |
| Queue Side | Left, Right | `Left` |
| Theme | 6 presets | `TIDAL` |
| Auto-play next | On / Off | On |
| Synced lyrics | On / Off | On |
| Preload next track | On / Off | On |
| Segment cache | On / Off | On |

---

## Keyboard Shortcuts

| Key | Action |
|---|---|
| `Space` | Play / Pause |
| `→` | Next track |
| `←` | Previous track |
| `R` | Toggle repeat |
| `S` | Toggle shuffle |
| `L` | Toggle lyrics overlay |

---

## Architecture

Everything lives in a single HTML file, split into three parts:

- **`<style>`** — Design tokens, theme variables, component styles, responsive layout
- **`<body>`** — Nav, hero/visualizer, search, results, queue panel, floating player, modals
- **`<script>`** — State, DASH streamer, MSE segment pipeline, queue engine, settings, event wiring

### DASH Streaming Pipeline

1. Fetch manifest from `/trackManifests/`
2. Parse MPD XML → segment URLs + durations
3. Feed initialization segment + media segments into `MediaSource`
4. Continuously prefetch ahead (40 segments) and trim behind (60s)
5. Cache segments in-memory (up to 15 tracks) for instant replays

Fallback path: if MSE doesn't support the codec, the file is fully assembled into a `Blob` and played directly.

---

## Self-Hosting the API

This player expects a TIDAL-compatible REST API with these endpoints:

- `GET /search/` — search (params: `s`, `al`, `a`, `p`, `limit`)
- `GET /info/` — track info (`id`)
- `GET /album/` — album tracks (`id`)
- `GET /playlist/` — playlist tracks (`id`)
- `GET /artist/` — artist info / albums (`id`, `f`)
- `GET /lyrics/` — lyrics (`id`)
- `GET /trackManifests/` — DASH manifest (`id`, `formats`, etc.)

Point the player at your backend in **Settings → API Base URL**.

---

## Known Limitations

- No offline persistence for the queue (refreshing clears it)
- No OAuth / user accounts
- Downloads are client-side only (browser memory limited for very large batches)
- Requires MSE — older browsers may fall back to slower blob assembly

---

## License

**This is free and unencumbered software released into the public domain.**

Anyone is free to copy, modify, publish, use, compile, sell, or distribute this software, either in source code form or as a compiled binary, for any purpose, commercial or non-commercial, and by any means.

See [UNLICENSE](https://unlicense.org) for the full text.

---

## Credits

Built by **Tiddles** · [tiddles.vercel.app](https://tiddles.vercel.app)

Fonts: DM Sans, DM Mono (Google Fonts) · Brutalist design system in the spirit of Swiss editorial layouts.
