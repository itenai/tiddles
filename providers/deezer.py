#!/usr/bin/env python3
"""Simple synchronous Deezer API: search, metadata, and direct file downloads."""

import json
import re
import base64
import os
import shutil
import tempfile
import threading
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
from fastapi import APIRouter, HTTPException, Query, BackgroundTasks, Header
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from . import deezload
from .deezload import AudioQuality, DeezerDownloader, extract_id_and_type, resolve_deezer_url, sanitise_filename

DZ_API = "https://api.deezer.com"
KINDS = ("track", "album", "artist", "playlist")
CONFIG_DIR = Path(os.environ.get("CONFIG_DIR") or (Path.cwd() / "config")).resolve()
SETTINGS_FILE = CONFIG_DIR / "settings.json"
DEFAULTS = {"arl": "", "quality": "FLAC", "min_quality": "", "retries": 3, "lyrics": True}
_settings_lock = threading.Lock()


def load_settings() -> Dict[str, Any]:
    data = dict(DEFAULTS)
    with _settings_lock:
        if SETTINGS_FILE.exists():
            try:
                data.update(json.loads(SETTINGS_FILE.read_text()))
            except Exception:
                pass
    data["arl"] = data.get("arl") or os.environ.get("DEEZLOAD_ARL", "")
    return data


def save_settings(data: Dict[str, Any]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with _settings_lock:
        tmp = SETTINGS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        tmp.replace(SETTINGS_FILE)


class AuthError(Exception):
    pass


class Manager:
    def __init__(self):
        self.lock = threading.Lock()
        self.cache: Dict[tuple, DeezerDownloader] = {}

    def get(self, quality: Optional[str] = None) -> DeezerDownloader:
        s = load_settings()
        if not s["arl"]:
            raise AuthError("DEEZLOAD_ARL is not configured.")
        q = quality or s["quality"]
        if q not in [v.value for v in AudioQuality]:
            raise ValueError("quality must be FLAC, MP3_320 or MP3_128")
        key = (s["arl"], q, s.get("min_quality", ""), int(s.get("retries", 3)), bool(s.get("lyrics", True)))
        with self.lock:
            if key in self.cache:
                return self.cache[key]
            dl = DeezerDownloader(
                s["arl"], AudioQuality(q),
                concurrency=max(1, min(8, int(os.getenv("DEEZER_DOWNLOAD_CONCURRENCY", "4")))),
                min_quality=AudioQuality(s["min_quality"]) if s.get("min_quality") else None,
                max_retries=int(s.get("retries", 3)), fetch_lyrics=bool(s.get("lyrics", True)),
            )
            if not dl.authenticate():
                raise AuthError("Deezer authentication failed. Check DEEZLOAD_ARL.")
            self.cache = {key: dl}
            return dl

    def reset(self):
        with self.lock:
            self.cache.clear()


mgr = Manager()


def dz(path: str, **params: Any) -> Dict[str, Any]:
    url = path if path.startswith("http") else f"{DZ_API}/{path.lstrip('/')}"
    r = requests.get(url, params=params or None, timeout=(10, 25))
    r.raise_for_status()
    data = r.json()
    if isinstance(data, dict) and data.get("error"):
        raise ValueError(data["error"].get("message") or str(data["error"]))
    return data


def dz_paged(path: str, limit: int = 100, max_items: int = 5000) -> List[Dict[str, Any]]:
    url = f"{DZ_API}/{path.lstrip('/')}?limit={limit}"
    result: List[Dict[str, Any]] = []
    while url and len(result) < max_items:
        data = dz(url)
        result.extend(data.get("data", []))
        url = data.get("next")
    return result[:max_items]


def fmt_duration(sec: Any) -> str:
    try:
        sec = int(sec)
        return f"{sec // 60}:{sec % 60:02d}"
    except (TypeError, ValueError):
        return ""


def normalise(o: Dict[str, Any], kind: str) -> Dict[str, Any]:
    if kind == "track":
        album = o.get("album") or {}
        return {"id": str(o.get("id")), "type": kind, "title": o.get("title") or "Unknown",
                "artist": (o.get("artist") or {}).get("name", ""), "album": album.get("title", ""),
                "duration": fmt_duration(o.get("duration")), "cover": album.get("cover_xl") or album.get("cover_big") or album.get("cover_medium") or album.get("cover")}
    if kind == "album":
        return {"id": str(o.get("id")), "type": kind, "title": o.get("title") or "Unknown",
                "artist": (o.get("artist") or {}).get("name", ""), "tracks": o.get("nb_tracks"),
                "cover": o.get("cover_xl") or o.get("cover_big") or o.get("cover_medium") or o.get("cover")}
    if kind == "artist":
        return {"id": str(o.get("id")), "type": kind, "title": o.get("name") or "Unknown",
                "albums": o.get("nb_album"), "cover": o.get("picture_xl") or o.get("picture_big") or o.get("picture_medium")}
    return {"id": str(o.get("id")), "type": "playlist", "title": o.get("title") or "Unknown",
            "tracks": o.get("nb_tracks"), "cover": o.get("picture_xl") or o.get("picture_big") or o.get("picture_medium")}


router = APIRouter(tags=["Deezer"])


class SettingsUpdate(BaseModel):
    arl: Optional[str] = None
    quality: Optional[str] = None
    min_quality: Optional[str] = None
    retries: Optional[int] = None
    lyrics: Optional[bool] = None


def _resolve_input(item: str, kind: Optional[str]) -> tuple[str, str]:
    raw = item.strip()
    if raw.startswith("http"):
        raw = resolve_deezer_url(raw)
        item_id, detected = extract_id_and_type(raw)
        if not item_id or not detected:
            raise HTTPException(400, "Invalid Deezer URL")
        return str(item_id), kind or detected
    if not raw.isdigit():
        raise HTTPException(400, "Provide a Deezer ID or URL")
    return raw, kind or "track"


def _normalise_quality(quality: Optional[str]) -> Optional[str]:
    if quality is None or not str(quality).strip():
        return None
    value = str(quality).strip().upper().replace("-", "_")
    aliases = {"128": "MP3_128", "MP3": "MP3_320", "320": "MP3_320", "HQ": "MP3_320", "HIRES": "FLAC", "LOSSLESS": "FLAC"}
    value = aliases.get(value, value)
    if value not in {q.value for q in AudioQuality}:
        raise HTTPException(400, "quality must be FLAC, MP3_320 or MP3_128")
    return value


def _get_stream_details(track_id: str, quality: Optional[str] = None) -> Dict[str, Any]:
    """Resolve a Deezer CDN URL and its per-track Blowfish key for browser clients."""
    try:
        dl = mgr.get(_normalise_quality(quality))
    except (AuthError, ValueError) as exc:
        raise HTTPException(503, str(exc))
    info = dl.get_track_info(str(track_id))
    if not info:
        raise HTTPException(404, "Deezer track metadata could not be resolved")
    try:
        url, resolved_quality = dl.get_download_url(str(track_id), track_info=info)
    except Exception as exc:
        raise HTTPException(502, f"Could not resolve Deezer stream URL: {exc}")
    if not url or not str(url).startswith(("http://", "https://")):
        raise HTTPException(502, "Deezer did not return a valid HTTP(S) stream URL")
    key = dl._generate_blowfish_key(str(track_id))
    return {
        "id": str(track_id),
        "url": url,
        "raw_url": url,
        "proxy_url": f"/deezer/stream/proxy?id={track_id}" + (f"&quality={_normalise_quality(quality)}" if quality else ""),
        "blowfish_key": base64.b64encode(key).decode("ascii"),
        "blowfish_key_hex": key.hex(),
        "key_encoding": "base64",
        "quality": resolved_quality.value if resolved_quality else None,
        "format": "flac" if resolved_quality == AudioQuality.FLAC else "mp3",
        "encrypted": True,
        "cipher": "Blowfish-CBC",
        "iv_hex": "0001020304050607",
        "decrypt_rule": "Decrypt each 2048-byte block where block_index % 3 == 0; block_index starts at 0. Leave other blocks unchanged; trailing partial block is not decrypted.",
    }


def _download_file(item_id: str, kind: str, quality: Optional[str], background_tasks: BackgroundTasks) -> FileResponse:
    if kind not in KINDS:
        raise HTTPException(400, "type must be track, album, playlist or artist")
    quality = _normalise_quality(quality)
    try:
        dl = mgr.get(quality)
    except (AuthError, ValueError) as e:
        raise HTTPException(503, str(e))

    temp_dir = Path(tempfile.mkdtemp(prefix="deezer_"))
    try:
        if kind == "track":
            file_path = dl.download_track(item_id, str(temp_dir))
            if not file_path or not Path(file_path).is_file():
                raise HTTPException(502, "Deezer could not provide this track at the requested quality")
            path = Path(file_path)
            background_tasks.add_task(shutil.rmtree, temp_dir, ignore_errors=True)
            return FileResponse(path, filename=path.name, media_type="application/octet-stream")

        # Collections are downloaded in the same request and returned as one ZIP.
        if kind == "album":
            meta = dz(f"album/{item_id}")
            tracks = dz_paged(f"album/{item_id}/tracks")
            folder_name = f"{meta.get('artist', {}).get('name', 'Unknown Artist')} - {meta.get('title', 'Album')}"
            out_dir = temp_dir / sanitise_filename(folder_name)
            out_dir.mkdir(parents=True, exist_ok=True)
            track_nums = [track.get("track_position") or index for index, track in enumerate(tracks, 1)]
            disc_nums = [track.get("disk_number") or 1 for track in tracks]
            dl._download_track_list(tracks, str(out_dir), track_nums=track_nums, disc_nums=disc_nums)
        elif kind == "playlist":
            meta = dz(f"playlist/{item_id}")
            tracks = dz_paged(f"playlist/{item_id}/tracks")
            out_dir = temp_dir / sanitise_filename(meta.get("title") or f"Playlist {item_id}")
            out_dir.mkdir(parents=True, exist_ok=True)
            dl._download_track_list(tracks, str(out_dir))
        else:
            meta = dz(f"artist/{item_id}")
            albums = dz_paged(f"artist/{item_id}/albums")
            for album in albums:
                album_id = str(album["id"])
                tracks = dz_paged(f"album/{album_id}/tracks")
                artist_dir = temp_dir / sanitise_filename(meta.get("name") or f"Artist {item_id}")
                album_dir = artist_dir / sanitise_filename(album.get("title") or album_id)
                album_dir.mkdir(parents=True, exist_ok=True)
                track_nums = [track.get("track_position") or index for index, track in enumerate(tracks, 1)]
                disc_nums = [track.get("disk_number") or 1 for track in tracks]
                dl._download_track_list(tracks, str(album_dir), track_nums=track_nums, disc_nums=disc_nums)

        zip_path = temp_dir / f"{sanitise_filename(kind + '_' + item_id)}.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
            for file in temp_dir.rglob("*"):
                if file.is_file() and file != zip_path and not file.name.endswith(".part"):
                    archive.write(file, file.relative_to(temp_dir))
        if not any(f.is_file() and f.suffix.lower() in (".flac", ".mp3") for f in temp_dir.rglob("*")):
            raise HTTPException(502, "No tracks could be downloaded")
        background_tasks.add_task(shutil.rmtree, temp_dir, ignore_errors=True)
        return FileResponse(zip_path, filename=zip_path.name, media_type="application/zip")
    except HTTPException:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise
    except Exception as e:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise HTTPException(502, f"Deezer download failed: {e}")


@router.get("/health")
def health():
    return {"ok": True, "configured": bool(load_settings().get("arl"))}


@router.get("/status")
def status():
    return {"configured": bool(load_settings().get("arl"))}


@router.get("/settings")
def get_settings():
    s = load_settings()
    arl = s.pop("arl", "")
    s["arl_set"] = bool(arl)
    s["arl_hint"] = f"••••{arl[-4:]}" if len(arl) >= 8 else ""
    return s


@router.put("/settings")
def put_settings(body: SettingsUpdate):
    s = load_settings()
    if body.quality is not None:
        if body.quality not in [q.value for q in AudioQuality]:
            raise HTTPException(400, "Invalid quality")
        s["quality"] = body.quality
    if body.min_quality is not None:
        if body.min_quality and body.min_quality not in [q.value for q in AudioQuality]:
            raise HTTPException(400, "Invalid minimum quality")
        s["min_quality"] = body.min_quality
    if body.retries is not None:
        s["retries"] = max(1, min(10, body.retries))
    if body.lyrics is not None:
        s["lyrics"] = body.lyrics
    if body.arl is not None and body.arl.strip():
        s["arl"] = body.arl.strip()
    save_settings(s)
    mgr.reset()
    return get_settings()


@router.get("/search")
def search(q: str = Query(..., min_length=1), type: str = Query("track"),
          limit: int = Query(25, ge=1, le=100), raw: bool = Query(True)):
    """Search Deezer. Returns the original Deezer response by default; raw=false gives compact cards."""
    if type not in KINDS:
        raise HTTPException(400, f"type must be one of: {', '.join(KINDS)}")
    try:
        data = dz(f"search/{type}", q=q, limit=limit)
        return data if raw else [normalise(o, type) for o in data.get("data", [])]
    except Exception as e:
        raise HTTPException(502, f"Deezer search failed: {e}")


@router.get("/track/{track_id}")
def track(track_id: str, raw: bool = Query(True)):
    """Full Deezer track metadata. raw=false returns the compact player shape."""
    try:
        data = dz(f"track/{track_id}")
        return data if raw else normalise(data, "track")
    except Exception as e:
        raise HTTPException(404, f"Track not found: {e}")


# Raw Deezer catalog endpoints. These preserve the upstream fields instead of
# reducing everything to a small search-card object.
@router.get("/album/{album_id}")
def album(album_id: str):
    return dz(f"album/{album_id}")


@router.get("/album/{album_id}/tracks")
def album_tracks(album_id: str, limit: int = Query(100, ge=1, le=5000)):
    return {"data": dz_paged(f"album/{album_id}/tracks", max_items=limit)}


@router.get("/artist/{artist_id}")
def artist(artist_id: str):
    return dz(f"artist/{artist_id}")


@router.get("/artist/{artist_id}/albums")
def artist_albums(artist_id: str, limit: int = Query(100, ge=1, le=5000)):
    return {"data": dz_paged(f"artist/{artist_id}/albums", max_items=limit)}


@router.get("/artist/{artist_id}/top")
def artist_top(artist_id: str, limit: int = Query(50, ge=1, le=100)):
    return dz(f"artist/{artist_id}/top", limit=limit)


@router.get("/artist/{artist_id}/related")
def artist_related(artist_id: str, limit: int = Query(25, ge=1, le=50)):
    return dz(f"artist/{artist_id}/related", limit=limit)


@router.get("/playlist/{playlist_id}")
def playlist(playlist_id: str):
    return dz(f"playlist/{playlist_id}")


@router.get("/playlist/{playlist_id}/tracks")
def playlist_tracks(playlist_id: str, limit: int = Query(100, ge=1, le=5000)):
    return {"data": dz_paged(f"playlist/{playlist_id}/tracks", max_items=limit)}


@router.get("/track/{track_id}/lyrics")
@router.get("/lyrics/{track_id}")
def track_lyrics(track_id: str):
    """Separate lyrics endpoint; returns plain and synced lyrics when available."""
    try:
        dl = mgr.get()
        data = dl._fetch_lyrics(str(track_id)) or {}
        return {"id": str(track_id), "plain": data.get("plain"),
                "synced": data.get("synced"),
                "available": bool(data.get("plain") or data.get("synced"))}
    except (AuthError, ValueError) as e:
        raise HTTPException(503, str(e))
    except Exception as e:
        raise HTTPException(502, f"Could not fetch Deezer lyrics: {e}")


def _deezer_user_id() -> str:
    try:
        dl = mgr.get()
    except (AuthError, ValueError) as e:
        raise HTTPException(503, str(e))
    user_id = getattr(dl, "user_id", None)
    if not user_id:
        current = getattr(getattr(dl, "client", None), "current_user", None) or {}
        user_id = current.get("id") if isinstance(current, dict) else None
    if not user_id:
        raise HTTPException(502, "Could not read the account ID from the authenticated Deezer session")
    return str(user_id)


@router.get("/library")
def account_library(limit: int = Query(100, ge=1, le=5000)):
    """Authenticated Deezer user's saved tracks, albums, artists and playlists."""
    uid = _deezer_user_id()
    return {
        "user_id": uid,
        "user": dz(f"user/{uid}"),
        "tracks": {"data": dz_paged(f"user/{uid}/tracks", max_items=limit)},
        "albums": {"data": dz_paged(f"user/{uid}/albums", max_items=limit)},
        "artists": {"data": dz_paged(f"user/{uid}/artists", max_items=limit)},
        "playlists": {"data": dz_paged(f"user/{uid}/playlists", max_items=limit)},
    }


@router.get("/library/{kind}")
def account_library_kind(kind: str, limit: int = Query(100, ge=1, le=5000)):
    if kind not in ("tracks", "albums", "artists", "playlists"):
        raise HTTPException(400, "kind must be tracks, albums, artists or playlists")
    uid = _deezer_user_id()
    return {"data": dz_paged(f"user/{uid}/{kind}", max_items=limit)}


@router.get("/resolve")
def resolve(url: str = Query(..., description="Deezer URL or numeric ID"), type: Optional[str] = None):
    item_id, kind = _resolve_input(url, type)
    if kind not in KINDS:
        raise HTTPException(400, "Invalid Deezer item type")
    try:
        data = dz(f"{kind}/{item_id}")
        return {"id": item_id, "type": kind, "url": f"https://www.deezer.com/{kind}/{item_id}",
                "data": data}
    except Exception as e:
        raise HTTPException(404, f"Deezer item not found: {e}")


@router.get("/stream")
def stream(
    url: Optional[str] = Query(None, description="Deezer track URL or numeric ID"),
    id: Optional[str] = Query(None, description="Deezer track ID (alias for url)"),
    track_id: Optional[str] = Query(None, description="Deezer track ID (alias for url)"),
    quality: Optional[str] = Query(None),
):
    """Return the raw Deezer CDN URL and the per-track Blowfish key.

    This is intended for trusted clients that implement Deezer's encrypted
    chunk decryption. The CDN URL may still enforce its own CORS policy;
    use /download when the browser must receive a same-origin file response.
    """
    item = url or id or track_id
    if not item:
        raise HTTPException(400, "Provide url, id or track_id")
    item_id, kind = _resolve_input(item, "track")
    if kind != "track":
        raise HTTPException(400, "/stream only supports tracks")
    return _get_stream_details(item_id, quality)


@router.get("/stream/proxy")
def stream_proxy(
    id: str = Query(..., description="Deezer track ID"),
    quality: Optional[str] = Query(None),
    range_header: Optional[str] = Header(None, alias="Range"),
):
    """Progressive Blowfish-decrypted audio with byte-accurate HTTP seeking."""
    details = _get_stream_details(id, quality)
    try:
        dl = mgr.get(details["quality"])
    except (AuthError, ValueError) as exc:
        raise HTTPException(503, str(exc))

    block_size = 2048
    # Probe with GET Range instead of HEAD: some Deezer CDN edges reject HEAD.
    probe = None
    try:
        probe = dl.session.get(details["url"], headers={"Range": "bytes=0-0"},
                              stream=True, timeout=(8, 25))
        if probe.status_code not in (200, 206):
            probe.raise_for_status()
        total = 0
        cr = probe.headers.get("Content-Range", "")
        cm = re.fullmatch(r"bytes\s+\d+-\d+/(\d+)", cr.strip(), re.I)
        if cm:
            total = int(cm.group(1))
        elif probe.status_code == 200:
            total = int(probe.headers.get("Content-Length", "0"))
        probe.close()
        probe = None
    except Exception as exc:
        if probe is not None:
            probe.close()
        raise HTTPException(502, f"Could not determine stream length for seeking: {exc}")

    partial = bool(range_header)
    start, end = 0, total - 1 if total else None
    if partial:
        m = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header.strip())
        if not m or (not m.group(1) and not m.group(2)):
            raise HTTPException(416, "Invalid Range header")
        if not total:
            raise HTTPException(503, "CDN did not provide a stream length; seeking is unavailable")
        if m.group(1):
            start = int(m.group(1))
            end = int(m.group(2)) if m.group(2) else total - 1
        else:
            suffix = int(m.group(2))
            if suffix <= 0:
                raise HTTPException(416, "Invalid suffix range")
            start, end = max(0, total - suffix), total - 1
        if start >= total or start > end:
            raise HTTPException(416, "Requested range not satisfiable",
                                headers={"Content-Range": f"bytes */{total}"})
        end = min(end, total - 1)

    aligned_start = (start // block_size) * block_size
    upstream = None
    try:
        upstream = dl.session.get(
            details["url"],
            headers={"Range": f"bytes={aligned_start}-"} if partial else {},
            stream=True, timeout=(8, 45),
        )
        if upstream.status_code not in (200, 206):
            upstream.raise_for_status()
        source_start = 0
        if upstream.status_code == 206:
            cm = re.fullmatch(r"bytes\s+(\d+)-(\d+)/(\d+|\*)",
                              upstream.headers.get("Content-Range", "").strip(), re.I)
            if not cm:
                raise HTTPException(502, "CDN returned invalid Content-Range")
            source_start = int(cm.group(1))
            if source_start != aligned_start:
                raise HTTPException(502, "CDN returned a mismatched byte range")
        key = dl._generate_blowfish_key(str(id))
        block_index = source_start // block_size
        discard = max(0, start - source_start) if partial else 0
        response_length = end - start + 1 if partial else total or None
    except HTTPException:
        if upstream is not None: upstream.close()
        raise
    except Exception as exc:
        if upstream is not None: upstream.close()
        raise HTTPException(502, f"Could not open Deezer stream: {exc}")

    def generate():
        pending = bytearray()
        index, skipped, sent = block_index, 0, 0
        try:
            for chunk in upstream.iter_content(chunk_size=128 * 1024):
                if not chunk: continue
                pending.extend(chunk)
                while len(pending) >= block_size:
                    block = bytes(pending[:block_size])
                    del pending[:block_size]
                    if index % 3 == 0:
                        block = dl._decrypt_chunk(key, block)
                    index += 1
                    if skipped < discard:
                        cut = min(len(block), discard - skipped)
                        block = block[cut:]
                        skipped += cut
                    if not block: continue
                    if response_length is not None:
                        block = block[:max(0, response_length - sent)]
                    if block:
                        sent += len(block)
                        yield block
                    if response_length is not None and sent >= response_length:
                        return
            if pending and (response_length is None or sent < response_length):
                block = bytes(pending)
                if skipped < discard:
                    cut = min(len(block), discard - skipped)
                    block = block[cut:]
                    skipped += cut
                if block:
                    if response_length is not None:
                        block = block[:response_length - sent]
                    if block: yield block
        finally:
            upstream.close()

    headers = {
        "Accept-Ranges": "bytes",
        "Cache-Control": "no-store",
        "Access-Control-Expose-Headers": "Accept-Ranges, Content-Range, Content-Length",
    }
    status = 206 if partial else 200
    if partial:
        headers["Content-Range"] = f"bytes {start}-{end}/{total}"
        headers["Content-Length"] = str(end - start + 1)
    elif total:
        headers["Content-Length"] = str(total)
    return StreamingResponse(generate(), status_code=status,
        media_type="audio/flac" if details["format"] == "flac" else "audio/mpeg",
        headers=headers)


@router.get("/track/{track_id}/stream")
def track_stream(track_id: str, quality: Optional[str] = Query(None)):
    """Path-style alias for /stream?id=..."""
    return _get_stream_details(track_id, quality)


@router.get("/download")
def download(
    background_tasks: BackgroundTasks,
    url: Optional[str] = Query(None, description="Deezer track/album/artist/playlist URL or ID"),
    id: Optional[str] = Query(None, description="Deezer item ID (alias for url)"),
    track_id: Optional[str] = Query(None, description="Deezer track ID (alias for url)"),
    type: Optional[str] = Query(None),
    quality: Optional[str] = Query(None),
):
    item = url or id or track_id
    if not item:
        raise HTTPException(400, "Provide url, id or track_id")
    item_id, kind = _resolve_input(item, type)
    return _download_file(item_id, kind, quality, background_tasks)


@router.get("/download/{item_id}")
def download_by_id(item_id: str, background_tasks: BackgroundTasks,
                   type: str = Query("track"), quality: Optional[str] = Query(None)):
    return _download_file(item_id, type, quality, background_tasks)


@router.get("/download/{kind}/{item_id}")
def download_typed(kind: str, item_id: str, background_tasks: BackgroundTasks,
                   quality: Optional[str] = Query(None)):
    """Direct download by explicit kind and ID; response is audio or a ZIP."""
    return _download_file(item_id, kind, quality, background_tasks)
