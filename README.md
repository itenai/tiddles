---
license: mit
title: Api
sdk: gradio
emoji: 📈
colorFrom: green
colorTo: green
short_description: nope
app_file: main.py
---
# Ultimate Music API

Unified FastAPI backend for TIDAL, Qobuz, Deezer, SoundCloud and Beatport.
All provider endpoints are included in one FastAPI application and one OpenAPI
schema. `/docs` lists every provider; there are no separate provider Swagger pages.

## Provider namespaces

- `/tidal/*` — TIDAL catalog, playback, lyrics and media
- `/qobuz/*` — Qobuz search, metadata, lyrics, playlists and file URL lookup
- `/deezer/*` — Deezer search, metadata, settings and direct downloads
- `/soundcloud/*` — SoundCloud search, metadata and stream resolution
- `/beatport/*` — Beatport login, status, search, track, stream and download
- `/api/v1/search` — unified multi-provider search
- `/api/v1/providers` — provider route registry
- `/health` — service and provider configuration status
- `/docs` — complete Swagger UI
- `/openapi.json` — complete OpenAPI schema

## Example routes

TIDAL:
- `GET /tidal/track/?id=...`
- `GET /tidal/album/?id=...`
- `GET /tidal/artist/?id=...`
- `GET /tidal/lyrics/?id=...`

Qobuz:
- `GET /qobuz/search/tracks?q=...`
- `GET /qobuz/tracks/{track_id}`
- `GET /qobuz/tracks/{track_id}/file-url?format_id=7`
- `GET /qobuz/tracks/{track_id}/lyrics`

Deezer:
- `GET /deezer/search?q=...&type=track` — full Deezer API response by default; add `raw=false` for compact results
- `GET /deezer/track/{id}` — raw track metadata
- `GET /deezer/album/{id}` and `/deezer/album/{id}/tracks`
- `GET /deezer/artist/{id}`, `/albums`, `/top`, `/related`
- `GET /deezer/playlist/{id}` and `/deezer/playlist/{id}/tracks`
- `GET /deezer/resolve?url=...` — canonical ID/type and full metadata
- `GET /deezer/lyrics/{track_id}` — plain and synced lyrics, separate from audio downloads
- `GET /deezer/library` — authenticated account library; or `/deezer/library/tracks`, `/albums`, `/artists`, `/playlists`
- `GET /deezer/download?url=...&quality=FLAC` — direct audio response for a track; direct ZIP response for an album, playlist or artist
- `GET /deezer/download/{id}?type=track&quality=FLAC` — direct download by ID
- `GET /deezer/download/track/{id}` or `/deezer/download/album/{id}` — explicit direct download routes

Downloads are generated in a temporary server-side folder and streamed in the same
HTTP response. The temporary folder is removed after the response completes. There
are no job IDs, job polling, queue endpoints, persistent local download library or
public URLs pointing at another person's local file.

SoundCloud:
- `GET /soundcloud/search?q=...`
- `GET /soundcloud/tracks/{id}`
- `GET /soundcloud/tracks/{id}/streams?quality=hq&mode=all&expand_segments=true`

Beatport:
- `POST /beatport/auth/login`
- `GET /beatport/auth/status`
- `GET /beatport/search?q=...`
- `GET /beatport/tracks/{id}/stream`
- `GET /beatport/tracks/{id}/download?quality=medium`

## Hugging Face Secrets

TIDAL: `TOKEN_JSON` (or `TIDAL_FILE` / `TOKEN_JSON_SECRET`), optional `COUNTRY_CODE`.

Qobuz: `QOBUZ_CONFIG_JSON` (or `QOBUZ_ENV_CONTENTS` / `ENV_CONTENTS`), or
`QOBUZ_EMAIL`, `QOBUZ_PASSWORD`, `QOBUZ_APP_ID`, `QOBUZ_APP_SECRETS`.

Deezer: `DEEZLOAD_ARL`.

SoundCloud: `SOUNDCLOUD_CLIENT_ID`, optional `SOUNDCLOUD_OAUTH_TOKEN`,
`SOUNDCLOUD_USER_AGENT`, `SOUNDCLOUD_APP_VERSION`.

Beatport: client ID and redirect URI are already in code. Optional
`BEATPORT_USERNAME` and `BEATPORT_PASSWORD` enable automatic login.

SoundCloud DRM variants can expose provider-returned manifest and segment URLs,
but the API does not request DRM licenses, extract content keys from WVD files,
or decrypt protected media. No WVD secret is required or consumed.

## Run on Termux

```bash
pkg update && pkg upgrade -y
pkg install python rust clang make libffi openssl -y
python -m venv .venv
source .venv/bin/activate
pip install --upgrade pip setuptools wheel
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 7860 --workers 1
```

Open `http://127.0.0.1:7860/docs`.

## Hugging Face

Deploy as a Docker Space using the included Dockerfile. Add provider credentials
under Space Settings → Variables and secrets → Secrets. No Gradio is required.

Use one worker for the provider sessions.
Protect public deployments with HTTPS, authentication and rate limiting.


### SoundCloud search and stream resolution

- `GET /soundcloud/search?q=...&limit=20` returns compact track cards by default,
  including `id`, `track_id`, `urn`, title, artist, artwork, permalink, duration,
  and basic statistics. Use `full=true` only when the full upstream object is needed.
- `GET /soundcloud/tracks/{track_id}` accepts a numeric track ID, SoundCloud track
  URL, or track URN.
- `GET /soundcloud/resolve?url=...` accepts the exact SoundCloud
  `/media/.../stream/...` transcoding URL.
- `GET /soundcloud/download?url=...` is the URL-based resolver and expands HLS
  manifests into available segment URLs by default (`expand_segments=false` disables it).
- `GET /soundcloud/tracks/{track_id}/streams` lists available variants.
- `GET /soundcloud/tracks/{track_id}/stream` returns the preferred variant.

Example full media URL input (URL-encode it when placing it in a query string):

`/soundcloud/download?url=https%3A%2F%2Fapi-mobile.soundcloud.com%2Fmedia%2Fsoundcloud%3Atracks%3A1481796646%2F0acb8483-f287-4949-acd5-1a04e7625a20%2Fstream%2Fctr-encrypted-hls`

For non-DRM variants, the API returns the provider stream URL and HLS segment
URLs. For DRM variants, it may return the protected manifest and segment URLs
plus encryption metadata, but deliberately omits key URIs and never performs
license exchange, WVD key extraction, or decryption.

### SoundCloud URL-based stream / download

Use the full SoundCloud media transcoding URL as the `url` query parameter (URL-encode it in clients):

- `GET /soundcloud/stream?url=<full_media_url>`
- `GET /soundcloud/download?url=<full_media_url>`
- `GET /soundcloud/resolve?url=<full_media_url>`

All three expand HLS playlists by default and return `manifest_url`,
`segment_urls`, `init_urls`, and any child playlist URLs exposed by the provider.
Pass `expand_segments=false` to disable expansion. For DRM streams, segment
locations are informational/protected; key URIs and key material are withheld.
The older `/soundcloud/tracks/{track_id}/stream` route remains available for
compatibility, but is deprecated.


### Optional WVD file path configuration

Set `WVD` (or the compatibility alias `WVD_FILE`) to the path of a WVD file.
Relative paths are resolved from the project root, next to `main.py`; for example,
place `device.wvd` beside `main.py` and set `WVD=device.wvd`. Absolute paths are
also accepted. The `/soundcloud/status` endpoint reports whether the configured
file exists. This setting only resolves and validates the file path; it does not
enable Widevine license processing or DRM decryption in this API.
## Adaptive traffic protection (Hugging Face / single worker)

The API now includes an in-process traffic controller in `providers/traffic.py`.
It applies weighted per-IP token buckets, a bounded fair queue, per-IP and global
concurrency caps, adaptive priority (lower recent usage is served first), a small
usage-based delay for frequent clients, and a bounded TTL cache for safe JSON GET
responses. Streaming, playback, download, authentication, health and status routes
are excluded from response caching.

The implementation is intentionally dependency-free and suited to the existing
single-worker Hugging Face deployment. Keep one Uvicorn worker: rate-limit state,
queue state and cache are process-local and are not shared across workers or Space
replicas. A multi-worker or multi-replica deployment needs a shared Redis backend.

Optional settings (also listed in `.env.example`):

- `RATE_LIMIT_CAPACITY` / `RATE_LIMIT_REFILL_PER_MINUTE`: per-IP token bucket.
- `MAX_GLOBAL_REQUESTS` / `MAX_CONCURRENT_PER_IP`: concurrency protection.
- `MAX_WAIT_QUEUE` / `QUEUE_TIMEOUT_SECONDS`: bounded waiting queue.
- `HTTP_MAX_CONNECTIONS` / `HTTP_KEEPALIVE_CONNECTIONS`: upstream HTTP pool sizing.
- `ALBUM_TRACKS_CONCURRENCY`: album fan-out limit.
- `RESPONSE_CACHE_ITEMS` / `RESPONSE_CACHE_MAX_BYTES`: in-memory cache bounds.
- `MAX_REQUEST_BODY_BYTES`: request body size guard (Content-Length based).
- `TRUST_PROXY_HEADERS`: enable only behind a trusted proxy that overwrites
  `X-Forwarded-For`; otherwise clients could spoof their IP identity.

`X-Cache`, `X-RateLimit-Policy`, `X-Client-Usage` and `X-Queue-Active` response
headers expose basic per-response diagnostics. A cache hit is still charged a
small token cost. The adaptive scheduler uses decaying recent weighted usage,
not a fixed FIFO queue, and applies aging to avoid starvation.


## Local lyrics translation (LibreTranslate)

Lyrics translation is handled by a local LibreTranslate service, not a hosted translation API.
The API exposes:
- `GET /lyrics/translation/languages`
- `POST /lyrics/translate` with `{ "text": "...", "source": "auto", "target": "ro", "format": "text" }`

For a local Docker deployment, run `docker compose up --build`. The API connects to the
LibreTranslate container over the private Compose network. Language models are persisted in
the `libretranslate_models` volume. Set `LIBRETRANSLATE_URL` if your local service runs
somewhere else; it defaults to `http://127.0.0.1:5000`.

The browser's Lyrics panel lets the user choose source and target languages. Translation
preserves the displayed lyric text; timestamp alignment remains active when the translated
line count matches the synced source. Click any timestamped lyric line to seek playback.

## Traffic controls

The application-level adaptive IP limiter, fair request queue, and traffic response-cache
middleware have been removed. Upstream providers may still return their own HTTP 429 responses;
those are provider-side limits and cannot be disabled by this API.


## Local lyrics translation (LibreTranslate)

Lyrics translation is self-hosted inside the same Python container. Installing
`requirements.txt` installs LibreTranslate; the FastAPI lifespan starts it on
`127.0.0.1:5000` automatically and stops it with the API process. The player
calls only `/lyrics/translate` and `/lyrics/translation/languages` on this API.
No external translation URL, API key, or separate service configuration is
required. LibreTranslate/Argos downloads language models on first startup; the
first boot can take longer and needs outbound network access. Once installed,
translation runs locally on CPU. On ephemeral Hugging Face storage, downloaded
models may need to be fetched again after a full Space rebuild/restart unless
the model directory is persisted.

## Download performance

Deezer album/playlist/artist collection downloads use parallel track workers
(default 4, configurable from 1 to 8 with `DEEZER_DOWNLOAD_CONCURRENCY`).
SoundCloud downloads fetch independent HLS segments concurrently (default 6,
configurable from 1 to 12 with `SOUNDCLOUD_DOWNLOAD_CONCURRENCY`) and assemble
them in original sequence order. The SoundCloud HTTP client uses HTTP/2 and
connection pooling. Single Deezer track decryption remains sequential because
its encrypted 2,048-byte block layout must be preserved.
