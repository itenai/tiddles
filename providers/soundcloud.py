"""SoundCloud catalog, stream resolver, and DRM-capable downloader.

Encrypted HLS (ctr-encrypted-hls / cbc-encrypted-hls) is Widevine-only:
pywidevine + the configured WVD device acquire the content key, then
segments are decrypted (pycenc for CTR, pycryptodome for CBC) and remuxed
via ffmpeg for /download, or returned as URLs + key for /stream.

Endpoints:
  GET /soundcloud/status
  GET /soundcloud/search
  GET /soundcloud/tracks/{track_id}
  GET /soundcloud/tracks/{track_id}/streams   (variant listing)
  GET /soundcloud/tracks/{track_id}/stream    (chunks + key + song)
  GET /soundcloud/tracks/{track_id}/download  (remuxed file)
"""
import base64
import concurrent.futures
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.parse import urljoin

import httpx
from fastapi import APIRouter, BackgroundTasks, HTTPException, Query
from fastapi.responses import FileResponse

router = APIRouter(prefix="/soundcloud", tags=["SoundCloud"])

DEFAULT_CLIENT_ID = "nvCx0RFcGkeJvu3OX4Eu1ttc8j43WmQR"
PROJECT_ROOT = Path(__file__).resolve().parents[1]

# --- optional DRM stack ---------------------------------------------------
try:
    from pywidevine.cdm import Cdm
    from pywidevine.device import Device
    from pywidevine.pssh import PSSH
    PYWIDEVINE_OK = True
except ImportError:
    try:
        from pywidevine import Cdm, PSSH
        from pywidevine.device import Device
        PYWIDEVINE_OK = True
    except ImportError:
        PYWIDEVINE_OK = False
        Cdm = Device = PSSH = None

try:
    from pycenc import decrypt_bytes
    PYCENC_OK = True
except ImportError:
    PYCENC_OK = False
    decrypt_bytes = None

try:
    from Crypto.Cipher import AES as _AES
    PYCRYPTODOME_OK = True
except ImportError:
    PYCRYPTODOME_OK = False
    _AES = None


WIDEVINE_LICENSE_URL = "https://license.media-streaming.soundcloud.cloud/playback/widevine"
HLS_PLAYLIST_RE = re.compile(r"^#EXT-X-", re.M)
INIT_RE = re.compile(r'#EXT-X-MAP:URI="([^"]+)"')
PSSH_RE = re.compile(r'URI="data:text/plain;base64,([^"]+)"')
IV_RE = re.compile(r"IV=0x([a-fA-F0-9]+)")


# --- config helpers -------------------------------------------------------

def _wvd_path():
    configured = (os.getenv("WVD") or os.getenv("WVD_FILE") or "").strip()
    if not configured:
        return None
    path = Path(configured).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    try:
        return path.resolve()
    except OSError:
        return path


def _config():
    cid = (os.getenv("SOUNDCLOUD_CLIENT_ID") or DEFAULT_CLIENT_ID).strip()
    oauth = (os.getenv("SOUNDCLOUD_OAUTH_TOKEN") or os.getenv("SOUNDCLOUD_TOKEN") or "").strip()
    return cid, oauth


def _headers(oauth):
    headers = {
        "User-Agent": os.getenv("SOUNDCLOUD_USER_AGENT", "okhttp/5.3.2"),
        "Accept": "application/json",
        "Origin": "https://soundcloud.com",
        "Referer": "https://soundcloud.com/",
    }
    if oauth:
        headers["Authorization"] = f"OAuth {oauth}"
    return headers


async def _client():
    _, oauth = _config()
    return httpx.AsyncClient(
        timeout=httpx.Timeout(35, connect=10),
        follow_redirects=True,
        headers=_headers(oauth),
    )


def _sync_client():
    _, oauth = _config()
    return httpx.Client(
        http2=True,
        timeout=httpx.Timeout(60, connect=8, read=45, write=20, pool=10),
        limits=httpx.Limits(
            max_connections=max(8, min(32, int(os.getenv("SOUNDCLOUD_DOWNLOAD_CONNECTIONS", "12")))),
            max_keepalive_connections=max(4, min(16, int(os.getenv("SOUNDCLOUD_DOWNLOAD_CONNECTIONS", "12")))),
            keepalive_expiry=30.0,
        ),
        follow_redirects=True,
        headers=_headers(oauth),
    )


async def _get_json(url, params=None):
    cid, _ = _config()
    async with await _client() as client:
        response = await client.get(url, params={**(params or {}), "client_id": cid})
    if response.status_code != 200:
        raise HTTPException(502, f"SoundCloud upstream returned HTTP {response.status_code}")
    try:
        return response.json()
    except ValueError:
        raise HTTPException(502, "SoundCloud returned invalid JSON")


# --- track / search endpoints --------------------------------------------

def _extension(transcoding):
    mime = (transcoding.get("format") or {}).get("mime_type", "").lower()
    if "mpeg" in mime or "mp3" in mime:
        return "mp3"
    if "aac" in mime or "mp4" in mime:
        return "m4a"
    if "ogg" in mime:
        return "ogg"
    if "flac" in mime:
        return "flac"
    return "m4a"


def _is_drm(transcoding):
    protocol = (transcoding.get("format") or {}).get("protocol", "").lower()
    return "encrypted" in protocol or protocol in {"widevine", "drm"}


def _track_id_from_value(value: str):
    value = (value or "").strip()
    if not value:
        return None
    match = re.search(r"soundcloud:tracks:(\d+)", value)
    if match:
        return match.group(1)
    match = re.search(r"(?:^|/)tracks[=/](\d+)(?:[/?#]|$)", value)
    if match:
        return match.group(1)
    if value.isdigit():
        return value
    return None


def _compact_track(track_data):
    user = track_data.get("user") or track_data.get("_embedded", {}).get("user") or {}
    stats = track_data.get("_embedded", {}).get("stats") or {}
    urn = track_data.get("urn") or ""
    track_id = _track_id_from_value(urn)
    artwork = track_data.get("artwork_url")
    if not artwork and track_data.get("artwork_url_template"):
        artwork = track_data["artwork_url_template"].replace("{size}", "t500x500")
    transcodings = (track_data.get("media") or {}).get("transcodings") or []
    return {
        "id": track_id,
        "track_id": track_id,
        "urn": urn,
        "title": track_data.get("title"),
        "artist": user.get("username") or track_data.get("publisher_metadata", {}).get("artist"),
        "user": {
            "id": (re.search(r"soundcloud:users:(\d+)", user.get("urn", "")) or [None, None])[1],
            "urn": user.get("urn"),
            "username": user.get("username"),
            "permalink": user.get("permalink"),
            "avatar_url": user.get("avatar_url"),
            "verified": user.get("verified", False),
        },
        "duration": track_data.get("duration"),
        "full_duration": track_data.get("full_duration"),
        "genre": track_data.get("genre"),
        "artwork_url": artwork,
        "permalink_url": track_data.get("permalink_url"),
        "created_at": track_data.get("created_at"),
        "playback_count": stats.get("playback_count"),
        "likes_count": stats.get("likes_count"),
        "streamable": bool(transcodings),
        "transcoding_count": len(transcodings),
    }


@router.get("/status")
async def status():
    wvd = _wvd_path()
    return {
        "configured": bool((os.getenv("SOUNDCLOUD_CLIENT_ID") or DEFAULT_CLIENT_ID).strip()),
        "oauth_token_configured": bool(os.getenv("SOUNDCLOUD_OAUTH_TOKEN", "").strip()),
        "wvd_path_configured": bool(os.getenv("WVD") or os.getenv("WVD_FILE")),
        "wvd_file_exists": bool(wvd and wvd.is_file()),
        "wvd_file_name": wvd.name if wvd and wvd.is_file() else None,
        "pywidevine_available": PYWIDEVINE_OK,
        "pycenc_available": PYCENC_OK,
        "pycryptodome_available": PYCRYPTODOME_OK,
        "drm_download_enabled": (
            PYWIDEVINE_OK
            and (PYCENC_OK or PYCRYPTODOME_OK)
            and bool(wvd and wvd.is_file())
        ),
        "stream_endpoint": "/soundcloud/tracks/{track_id}/stream",
        "download_endpoint": "/soundcloud/tracks/{track_id}/download",
    }


@router.get("/search")
async def search(q: str = Query(..., min_length=1, max_length=250),
                 limit: int = Query(20, ge=1, le=50),
                 full: bool = Query(False)):
    data = await _get_json(
        "https://api-mobile.soundcloud.com/search/query",
        {"q": q, "limit": str(limit), "filter.content_type": "all", "version": "v4"},
    )
    entities = data.get("entities", {})
    results, seen = [], set()

    def add(urn):
        if not urn or urn in seen or urn not in entities:
            return
        entity = entities[urn]
        if entity.get("type") != "track":
            return
        track_data = dict(entity.get("data", {}))
        if not track_data.get("user"):
            track_data["user"] = track_data.get("_embedded", {}).get("user") or {}
        results.append(track_data if full else _compact_track(track_data))
        seen.add(urn)

    for section in data.get("sections", []):
        urn = section.get("urn", "")
        if urn.endswith("search_top_result"):
            add(section.get("data", {}).get("result"))
        elif urn.endswith(("search_tracks", "search_universal")):
            for item in section.get("data", {}).get("results", []):
                add(item)

    return {"query": q, "count": len(results[:limit]), "results": results[:limit]}


@router.get("/tracks/{track_id}")
async def track(track_id: str):
    normalized_id = _track_id_from_value(track_id)
    if not normalized_id:
        raise HTTPException(400, "Use a numeric SoundCloud track ID, track URL, or SoundCloud URN")
    data = await _get_json(
        "https://api-v2.soundcloud.com/tracks",
        {"ids": normalized_id,
         "app_version": os.getenv("SOUNDCLOUD_APP_VERSION", "1789138724"),
         "app_locale": "en"},
    )
    if not data:
        raise HTTPException(404, "SoundCloud track not found")
    return data[0]


# --- transcoding pick + variant listing ----------------------------------

def _pick_transcoding(transcodings, quality: str, nodrm: bool):
    """Return (transcoding, is_drm, protocol). Encrypted first when not nodrm."""
    def proto(t):
        return (t.get("format") or {}).get("protocol", "")

    if nodrm:
        for t in transcodings:
            if proto(t) == "hls" and t.get("quality") == quality:
                return t, False, "hls"
        for t in transcodings:
            if proto(t) == "hls":
                return t, False, "hls"
        return None, False, ""

    for wanted in ("ctr-encrypted-hls", "cbc-encrypted-hls"):
        for t in transcodings:
            if proto(t) == wanted and t.get("quality") == quality:
                return t, True, wanted
        for t in transcodings:
            if proto(t) == wanted:
                return t, True, wanted

    for t in transcodings:
        if proto(t) == "hls":
            return t, False, "hls"
    return None, False, ""


async def _resolve_transcoding_meta(info, transcoding, expand_segments=False):
    """Used by /tracks/{id}/streams to show per-variant details."""
    cid, _ = _config()
    auth_url = transcoding.get("url")
    if not auth_url:
        return None
    drm = _is_drm(transcoding)
    result = {
        "transcoding_url": auth_url,
        "stream_url": None,
        "quality": transcoding.get("quality"),
        "preset": transcoding.get("preset"),
        "protocol": (transcoding.get("format") or {}).get("protocol"),
        "mime_type": (transcoding.get("format") or {}).get("mime_type"),
        "extension": _extension(transcoding),
        "drm": drm,
    }

    params = {"track_authorization": info.get("track_authorization", ""), "client_id": cid}
    async with await _client() as client:
        response = await client.get(auth_url, params=params)
    if response.status_code != 200:
        return {**result, "resolution_error": f"HTTP {response.status_code}"}
    try:
        resolved = response.json()
    except ValueError:
        return {**result, "resolution_error": "Invalid stream resolver response"}

    stream_url = resolved.get("url")
    result["stream_url"] = stream_url
    if resolved.get("licenseAuthToken"):
        result["license_auth_token_present"] = True

    if stream_url and expand_segments:
        async with await _client() as client:
            manifest_response = await client.get(stream_url)
        if manifest_response.status_code != 200:
            return {**result, "resolution_error": f"manifest HTTP {manifest_response.status_code}"}
        manifest = manifest_response.text
        init_urls = []
        segment_urls = []
        for line in manifest.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                if line.startswith("#EXT-X-MAP"):
                    m = INIT_RE.search(line)
                    if m:
                        init_urls.append(urljoin(stream_url, m.group(1)))
                continue
            absolute = urljoin(stream_url, line)
            if line.lower().endswith((".m3u8", ".m3u")):
                continue
            segment_urls.append(absolute)
        result["init_urls"] = init_urls
        result["segment_urls"] = segment_urls
        result["segment_count"] = len(segment_urls)
        result["manifest_url"] = stream_url
    elif drm:
        result["availability"] = "protected_stream_url_only"
    return result


@router.get("/tracks/{track_id}/streams")
async def streams(track_id: str,
                  quality: str = Query("hq", pattern="^(sq|hq)$"),
                  mode: str = Query("all", pattern="^(all|nodrm|drm)$"),
                  expand_segments: bool = Query(False)):
    info = await track(track_id)
    transcodings = info.get("media", {}).get("transcodings", [])
    variants = []
    for item in transcodings:
        drm = _is_drm(item)
        protocol = (item.get("format") or {}).get("protocol", "")
        if mode == "nodrm" and (drm or protocol != "hls"):
            continue
        if mode == "drm" and not drm:
            continue
        variants.append(item)

    variants.sort(key=lambda item: item.get("quality") != quality)
    output = []
    for item in variants:
        output.append(await _resolve_transcoding_meta(
            info, item,
            expand_segments=(expand_segments and not _is_drm(item)),
        ))
    output = [item for item in output if item]
    return {
        "track_id": _track_id_from_value(track_id) or str(track_id),
        "title": info.get("title"),
        "artist": (info.get("user") or {}).get("username"),
        "mode": mode,
        "requested_quality": quality,
        "available_streams": output,
        "count": len(output),
    }


# --- Widevine license acquisition ----------------------------------------

def _init_cdm(wvd_path: Path):
    if not PYWIDEVINE_OK:
        raise HTTPException(503, "pywidevine is not installed; cannot request Widevine licenses")
    device = Device.load(str(wvd_path))
    cdm = Cdm.from_device(device)
    return cdm, cdm.open()


def _normalize_hex(value):
    """Accept str/bytes/UUID-like; return lowercase hex string or None."""
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray)):
        return value.hex()
    if isinstance(value, str):
        s = value.strip().lower()
        if s.startswith("0x"):
            s = s[2:]
        return s or None
    # UUID or other object
    try:
        return str(value).replace("-", "").lower() or None
    except Exception:
        return None


def _request_widevine_key_full(cdm, session_id, pssh_data: bytes, license_token: str):
    """Returns (content_key_hex, kid_hex_or_None).

    Handles pywidevine's inconsistent Key.kid type across versions:
      - bytes         -> .hex()
      - str (already hex) -> used directly
      - uuid.UUID     -> str() then strip dashes
    """
    pssh = PSSH(pssh_data)
    challenge = cdm.get_license_challenge(session_id, pssh)
    if not challenge:
        raise HTTPException(502, "Could not build Widevine challenge")

    license_url = WIDEVINE_LICENSE_URL
    if license_token:
        sep = "&" if "?" in license_url else "?"
        license_url = f"{license_url}{sep}license_token={license_token}"

    headers = {
        "Accept": "*/*",
        "Content-Type": "application/octet-stream",
        "Origin": "https://soundcloud.com",
        "Referer": "https://soundcloud.com/",
        "User-Agent": os.getenv("SOUNDCLOUD_USER_AGENT", "okhttp/5.3.2"),
    }
    with httpx.Client(timeout=30.0) as client:
        resp = client.post(license_url, content=challenge, headers=headers)
    if resp.status_code != 200:
        raise HTTPException(502, f"Widevine license request failed: HTTP {resp.status_code}")

    cdm.parse_license(session_id, resp.content)
    for key in cdm.get_keys(session_id):
        if key.type != "CONTENT":
            continue
        key_hex = _normalize_hex(key.key)
        kid_hex = _normalize_hex(getattr(key, "kid", None))
        if not key_hex:
            continue
        return key_hex, kid_hex
    raise HTTPException(502, "Widevine license response contained no content key")


# ---------------------------------------------------------------------------
# Shared resolver — used by both /download and /stream so they can never
# diverge on selection, playlist resolution, PSSH extraction, or licensing.
# ---------------------------------------------------------------------------

def _resolve_for_stream(track_id: str, quality: str, nodrm: bool):
    wvd = _wvd_path()

    with _sync_client() as client:
        cid, _ = _config()
        r = client.get(
            "https://api-v2.soundcloud.com/tracks",
            params={"ids": track_id, "client_id": cid,
                    "app_version": os.getenv("SOUNDCLOUD_APP_VERSION", "1789138724"),
                    "app_locale": "en"},
        )
        r.raise_for_status()
        data = r.json()
        if not data:
            raise HTTPException(404, "SoundCloud track not found")
        info = data[0]

        title = info.get("title") or f"track_{track_id}"
        artist = (info.get("user") or {}).get("username") or "Unknown"

        transcodings = (info.get("media") or {}).get("transcodings") or []
        transcoding, is_drm, protocol = _pick_transcoding(transcodings, quality, nodrm)
        if transcoding is None:
            raise HTTPException(404, "No suitable transcoding for this track/quality")

        auth_url = transcoding["url"]
        sep = "&" if "?" in auth_url else "?"
        resolved_url = (
            f"{auth_url}{sep}track_authorization={info.get('track_authorization', '')}"
            f"&client_id={cid}"
        )
        rr = client.get(resolved_url)
        rr.raise_for_status()
        resolved = rr.json()
        playlist_url = resolved.get("url")
        license_token = resolved.get("licenseAuthToken") or ""
        if not playlist_url:
            raise HTTPException(502, "SoundCloud did not return a playlist URL")

        pr = client.get(playlist_url)
        pr.raise_for_status()
        playlist = pr.text
        if not HLS_PLAYLIST_RE.search(playlist):
            raise HTTPException(502, "SoundCloud returned a non-HLS playlist")

        pairs = [(int(m.group(2)), m.group(1))
                 for m in re.finditer(r"(https://[^\s]+data(\d+)\.m4s[^\s]*)", playlist)]
        pairs.sort(key=lambda p: p[0])
        init_m = INIT_RE.search(playlist)
        if not init_m or not pairs:
            raise HTTPException(502, "Could not parse HLS segments")
        init_url = init_m.group(1)

        content_key = None
        key_id = None
        pssh_present = False

        if is_drm and not nodrm:
            if not PYWIDEVINE_OK or not PYCENC_OK:
                raise HTTPException(503, "DRM requires pywidevine and pycenc")
            if not wvd or not wvd.is_file():
                raise HTTPException(503, "DRM requires a WVD device file (set WVD env var)")

            pssh_match = PSSH_RE.search(playlist)
            if not pssh_match:
                fp_only = (
                    'KEYFORMAT="com.apple.streamingkeydelivery"' in playlist
                    and "urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed" not in playlist
                )
                if fp_only:
                    raise HTTPException(
                        501,
                        "Track is FairPlay-encrypted only; a Widevine WVD cannot license it.",
                    )
                raise HTTPException(502, "DRM playlist missing PSSH")
            pssh_present = True
            pssh_data = base64.b64decode(pssh_match.group(1))

            cdm, session_id = _init_cdm(wvd)
            try:
                content_key, key_id = _request_widevine_key_full(
                    cdm, session_id, pssh_data, license_token
                )
            finally:
                try:
                    cdm.close(session_id)
                except BaseException:
                    pass

        # Normalize for downstream consumers (both /stream and /download).
        content_key = _normalize_hex(content_key)
        key_id = _normalize_hex(key_id)

        iv_hex = None
        if protocol == "cbc-encrypted-hls":
            iv_m = IV_RE.search(playlist)
            if iv_m:
                iv_hex = iv_m.group(1)

        return {
            "info": info,
            "title": title,
            "artist": artist,
            "transcoding": transcoding,
            "is_drm": is_drm,
            "protocol": protocol,
            "playlist_url": playlist_url,
            "playlist": playlist,
            "init_url": init_url,
            "pairs": pairs,
            "segments": [u for _, u in pairs],
            "license_token": license_token,
            "pssh_present": pssh_present,
            "content_key": content_key,
            "key_id": key_id,
            "iv": iv_hex,
        }


# --- /tracks/{id}/stream -------------------------------------------------

@router.get("/tracks/{track_id}/stream")
async def track_stream(
    track_id: str,
    quality: str = Query("hq", pattern="^(sq|hq)$"),
    nodrm: bool = Query(False, description="Skip Widevine; return segments only"),
    include_key: bool = Query(True, description="Include Widevine content key"),
):
    """Chunk URLs + Widevine content key + song metadata.

    Shares the exact upstream path /download uses (via _resolve_for_stream).
    """
    import asyncio
    import traceback

    normalized = _track_id_from_value(track_id)
    if not normalized:
        raise HTTPException(400, "Invalid SoundCloud track id")

    try:
        resolved = await asyncio.to_thread(
            _resolve_for_stream, normalized, quality, nodrm
        )
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(500, f"resolve failed:\n{traceback.format_exc()}")

    try:
        info = resolved.get("info") or {}
        tr = resolved.get("transcoding") or {}
        fmt = tr.get("format") or {}
        user = info.get("user") or {}
        publisher = info.get("publisher_metadata") or {}
        stats = (info.get("_embedded") or {}).get("stats") or {}

        artwork = info.get("artwork_url")
        if not artwork:
            tpl = info.get("artwork_url_template") or ""
            if "{size}" in tpl:
                artwork = tpl.replace("{size}", "t500x500")

        artist_id = None
        m = re.search(r"soundcloud:users:(\d+)", user.get("urn") or "")
        if m:
            artist_id = m.group(1)

        song = {
            "track_id": normalized,
            "urn": info.get("urn"),
            "title": info.get("title"),
            "artist": user.get("username") or publisher.get("artist"),
            "artist_id": artist_id,
            "album": publisher.get("album_title"),
            "isrc": publisher.get("isrc"),
            "label": publisher.get("label_name"),
            "duration_ms": info.get("full_duration") or info.get("duration"),
            "genre": info.get("genre"),
            "tag_list": info.get("tag_list"),
            "artwork_url": artwork,
            "permalink_url": info.get("permalink_url"),
            "created_at": info.get("created_at"),
            "playback_count": stats.get("playback_count"),
            "likes_count": stats.get("likes_count"),
        }

        segments = resolved.get("segments") or []
        stream = {
            "protocol": resolved.get("protocol") or "",
            "quality": tr.get("quality"),
            "preset": tr.get("preset"),
            "mime_type": fmt.get("mime_type"),
            "drm": bool(resolved.get("is_drm")),
            "manifest_url": resolved.get("playlist_url"),
            "init_url": resolved.get("init_url"),
            "segment_urls": segments,
            "segment_count": len(segments),
            "widevine_pssh_present": bool(resolved.get("pssh_present")),
        }

        key_block = {
            "content_key": None,
            "key_id": None,
            "iv": None,
            "decrypt_algo": None,
            "decrypt_hint": None,
            "decrypt_note": None,
        }

        content_key = resolved.get("content_key")
        protocol = resolved.get("protocol") or ""
        is_drm = bool(resolved.get("is_drm"))

        if include_key and is_drm and not nodrm and content_key:
            key_id = resolved.get("key_id")
            iv = resolved.get("iv")   # None for CTR — this is fine
            algo = (
                "AES-CTR" if protocol == "ctr-encrypted-hls"
                else "AES-CBC" if protocol == "cbc-encrypted-hls"
                else None
            )
            key_block.update({
                "content_key": content_key,
                "key_id": key_id,
                "iv": iv,
                "decrypt_algo": algo,
            })
            if protocol == "ctr-encrypted-hls":
                key_block["decrypt_hint"] = (
                    "MPEG-CENC AES-CTR. IV is per-sample, stored in each "
                    "fragment's senc box (moof/traf/senc). counter length = 128 bits."
                )
                key_block["decrypt_note"] = (
                    "For each fragment: parse moof -> traf -> senc to get per-sample IVs "
                    "and moof -> traf -> trun to get per-sample sizes, then AES-CTR "
                    "each sample independently with its senc IV. Equivalent to "
                    "pycenc.decrypt_bytes(key, init + segment)."
                )
            elif protocol == "cbc-encrypted-hls":
                key_block["decrypt_hint"] = (
                    f"AES-CBC with IV from EXT-X-KEY (iv={iv}). Strip PKCS#7."
                )

        return {"song": song, "stream": stream, "key": key_block}

    except HTTPException:
        raise
    except Exception:
        raise HTTPException(500, f"response-build failed:\n{traceback.format_exc()}")


# --- /tracks/{id}/download -----------------------------------------------

def _safe_name(s: str) -> str:
    s = re.sub(r'[\\/:*?"<>|]', "_", s or "")
    return s.strip() or "track"


def _download_and_decrypt(track_id: str, quality: str, nodrm: bool, output_dir: Path):
    """Download + decrypt + remux, using _resolve_for_stream for steps 1-5."""
    resolved = _resolve_for_stream(track_id, quality, nodrm)

    is_drm = resolved["is_drm"]
    protocol = resolved["protocol"]
    content_key = resolved["content_key"]  # already normalized hex str or None
    iv_hex = resolved["iv"]
    init_url = resolved["init_url"]
    pairs = resolved["pairs"]
    title = resolved["title"]
    artist = resolved["artist"]
    info = resolved.get("info") or {}
    artwork_url = info.get("artwork_url")
    if not artwork_url and info.get("artwork_url_template"):
        artwork_url = info["artwork_url_template"].replace("{size}", "t500x500")

    with _sync_client() as client:
        temp_dir = Path(tempfile.mkdtemp(prefix=f"sc_{track_id}_"))
        try:
            init_resp = client.get(init_url)
            init_resp.raise_for_status()
            init_data = init_resp.content
            (temp_dir / "init.m4s").write_bytes(init_data)

            def _fetch_decrypt_segment(item):
                num, seg_url = item
                last_error = None
                for attempt in range(3):
                    try:
                        sr = client.get(seg_url)
                        sr.raise_for_status()
                        if len(sr.content) < 100:
                            raise ValueError("segment response is unexpectedly small")
                        if is_drm and content_key:
                            if protocol == "cbc-encrypted-hls":
                                if not PYCRYPTODOME_OK:
                                    raise HTTPException(503, "pycryptodome required for cbc-encrypted-hls")
                                key = bytes.fromhex(content_key)
                                iv = bytes.fromhex(iv_hex) if iv_hex else b"\x00" * 16
                                plain = _AES.new(key, _AES.MODE_CBC, iv).decrypt(sr.content)
                                pad = plain[-1] if plain else 0
                                if 1 <= pad <= 16:
                                    plain = plain[:-pad]
                                decrypted = plain
                            else:
                                decrypted = decrypt_bytes(bytes.fromhex(content_key), init_data + sr.content)
                        else:
                            decrypted = sr.content
                        dec_path = temp_dir / f"dec_{num:04d}.m4s"
                        dec_path.write_bytes(decrypted)
                        return dec_path
                    except HTTPException:
                        raise
                    except Exception as exc:
                        last_error = exc
                        if attempt < 2:
                            time.sleep(0.35 * (attempt + 1))
                raise HTTPException(502, f"Failed to download segment {num}: {type(last_error).__name__ if last_error else 'unknown error'}")

            # HLS fragments are independent HTTP requests; download/decrypt in
            # parallel, then concatenate by sequence number to preserve audio.
            workers = max(1, min(12, int(os.getenv("SOUNDCLOUD_DOWNLOAD_CONCURRENCY", "6"))))
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
                dec_files = list(executor.map(_fetch_decrypt_segment, pairs))

            combined_mp4 = temp_dir / "combined.mp4"
            # HLS fMP4 is not a standalone MP4 per media segment: the init
            # segment carries the track/moov metadata required by every m4s
            # fragment. It must be prepended before the ordered media fragments.
            with open(combined_mp4, "wb") as out:
                out.write(init_data)
                for df in sorted(dec_files, key=lambda p: int(re.search(r"dec_(\d+)", p.name).group(1))):
                    with open(df, "rb") as inf:
                        shutil.copyfileobj(inf, out)

            ffmpeg = shutil.which("ffmpeg")
            if not ffmpeg:
                raise HTTPException(503, "ffmpeg is required for remuxing")
            quality_tag = quality.upper() + ("_DRM" if is_drm else "_NODRM")
            final_name = f"{_safe_name(artist)} - {_safe_name(title)} [{quality_tag}].m4a"
            final_path = output_dir / final_name

            # Embed SoundCloud artwork as an attached-picture stream in the M4A.
            # Keep audio bit-for-bit; only the cover image is re-encoded to JPEG.
            cover_path = None
            if artwork_url:
                try:
                    cover_response = client.get(artwork_url, timeout=20.0)
                    cover_response.raise_for_status()
                    if cover_response.content:
                        cover_path = temp_dir / "cover.jpg"
                        cover_path.write_bytes(cover_response.content)
                except Exception:
                    cover_path = None

            cmd = [ffmpeg, "-y", "-i", str(combined_mp4)]
            if cover_path:
                cmd += ["-i", str(cover_path), "-map", "0:a:0", "-map", "1:v:0",
                        "-c:a", "copy", "-c:v", "mjpeg", "-disposition:v:0", "attached_pic",
                        "-metadata:s:v:0", "title=Album cover", "-metadata:s:v:0", "comment=Cover (front)"]
            else:
                cmd += ["-map", "0:a:0", "-c:a", "copy"]
            cmd += ["-metadata", f"title={title}", "-metadata", f"artist={artist}",
                    "-movflags", "+faststart", str(final_path)]
            proc = subprocess.run(cmd, capture_output=True, text=True)
            if proc.returncode != 0 or not final_path.exists():
                raise HTTPException(502, f"ffmpeg remux failed: {proc.stderr[-300:]}")
            return final_path
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)


@router.get("/tracks/{track_id}/download")
async def download_track(
    track_id: str,
    background_tasks: BackgroundTasks,
    quality: str = Query("hq", pattern="^(sq|hq)$"),
    nodrm: bool = Query(False, description="Skip DRM; use only plain HLS"),
):
    """Download a track. DRM variants are Widevine-licensed and pycenc-decrypted."""
    normalized = _track_id_from_value(track_id)
    if not normalized:
        raise HTTPException(400, "Invalid SoundCloud track id")

    import asyncio
    output_dir = Path(tempfile.mkdtemp(prefix="sc_dl_"))
    try:
        final_path = await asyncio.to_thread(
            _download_and_decrypt, normalized, quality, nodrm, output_dir
        )
    except HTTPException:
        shutil.rmtree(output_dir, ignore_errors=True)
        raise
    except Exception as exc:
        shutil.rmtree(output_dir, ignore_errors=True)
        raise HTTPException(502, f"SoundCloud download failed: {exc}") from exc

    background_tasks.add_task(shutil.rmtree, output_dir, ignore_errors=True)
    return FileResponse(final_path, filename=final_path.name,
                        media_type="audio/mp4")