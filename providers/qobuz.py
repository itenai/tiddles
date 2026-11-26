"""Single-file Qobuz API wrapper.

Install: pip install fastapi uvicorn requests
Run:     uvicorn qobuz_api:app --host 127.0.0.1 --port 8000

Configure QOBUZ_EMAIL, QOBUZ_PASSWORD, QOBUZ_APP_ID, QOBUZ_APP_SECRETS
(comma-separated authorized values) and optionally API_KEY in the environment.
A local .env file is loaded if python-dotenv is installed; otherwise a minimal
.env reader is used. Do not commit credentials or expose this API publicly without
TLS and a strong API_KEY.
"""
from __future__ import annotations

import os, time, hashlib, logging, json
from typing import Optional

import requests
from fastapi import APIRouter, HTTPException, Query

# Single-string JSON configuration secret.
# In Hugging Face Space Settings -> Variables and secrets, create ONE Secret:
#   Name: QOBUZ_CONFIG_JSON
#   Value: {"QOBUZ_EMAIL":"...", ...}
# Every value can be a string. QOBUZ_APP_SECRETS is a comma-separated string.
# Individual environment variables override values from the JSON secret.
_JSON_SECRET_NAME = None
_JSON_SECRET = ""
for _candidate in ("QOBUZ_CONFIG_JSON", "QOBUZ_ENV_CONTENTS", "ENV_CONTENTS"):
    _candidate_value = os.getenv(_candidate)
    if _candidate_value and _candidate_value.strip():
        _JSON_SECRET_NAME, _JSON_SECRET = _candidate, _candidate_value.strip()
        break

_JSON_CONFIG_KEYS = {
    "QOBUZ_EMAIL", "QOBUZ_PASSWORD", "QOBUZ_APP_ID",
    "QOBUZ_APP_SECRETS", "CORS_ORIGINS", "LOG_LEVEL",
}
_JSON_CONFIG_OK = False
_JSON_CONFIG_ERROR = None
if _JSON_SECRET:
    try:
        # Allow a pasted Markdown code fence, but store the JSON itself as one
        # string in the Space Secret. Never log the secret or parsed values.
        if _JSON_SECRET.startswith("```"):
            _lines = _JSON_SECRET.splitlines()
            if _lines and _lines[0].strip().startswith("```"):
                _lines = _lines[1:]
            if _lines and _lines[-1].strip() == "```":
                _lines = _lines[:-1]
            _JSON_SECRET = "\n".join(_lines).strip()
        _parsed_config = json.loads(_JSON_SECRET.lstrip("\ufeff"))
        if not isinstance(_parsed_config, dict):
            raise ValueError("JSON root must be an object")
        for _key, _value in _parsed_config.items():
            if _key not in _JSON_CONFIG_KEYS or _value is None:
                continue
            if _key == "QOBUZ_APP_SECRETS" and isinstance(_value, list):
                _value = ",".join(str(item).strip() for item in _value if str(item).strip())
            elif isinstance(_value, (dict, list)):
                logging.getLogger("qobuz-api").warning(
                    "Invalid JSON config value type for %s; expected a string%s",
                    _key, " or array" if _key == "QOBUZ_APP_SECRETS" else "")
                continue
            else:
                _value = str(_value).strip() if _key != "QOBUZ_PASSWORD" else str(_value)
            # An explicitly configured non-empty Space variable has priority.
            if not os.getenv(_key, "").strip():
                os.environ[_key] = _value
        _JSON_CONFIG_OK = True
    except (json.JSONDecodeError, ValueError):
        _JSON_CONFIG_ERROR = "invalid_json"
        logging.getLogger("qobuz-api").warning(
            "Secret %s is not valid JSON. Paste one raw JSON object as its value; "
            "secret contents are not logged.", _JSON_SECRET_NAME or "QOBUZ_CONFIG_JSON")
elif not _JSON_SECRET_NAME:
    _JSON_CONFIG_ERROR = "missing_secret"

# Load .env without requiring another dependency.
def _load_env_file(path: str = ".env") -> None:
    if not os.path.isfile(path):
        return
    try:
        from dotenv import load_dotenv
        load_dotenv(path)
        return
    except ImportError:
        pass
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key, value = key.strip(), value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                if key and key not in os.environ:
                    os.environ[key] = value
    except OSError:
        pass

_load_env_file()
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("qobuz-api")

class AuthenticationError(Exception): pass
class IneligibleError(Exception): pass
class InvalidAppIdError(Exception): pass
class InvalidAppSecretError(Exception): pass
class InvalidQuality(Exception): pass
class NonStreamable(Exception): pass

RESET = "Check your Qobuz credentials and authorized app configuration."

class QobuzClient:
    """Compact, self-contained version of the upstream project's API client."""
    def __init__(self, email: str, password: str, app_id: str, secrets: list[str]):
        self.secrets = secrets
        self.id = str(app_id)
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:83.0) Gecko/20100101 Firefox/83.0",
            "X-App-Id": self.id,
            "Content-Type": "application/json;charset=UTF-8",
        })
        self.base = "https://www.qobuz.com/api.json/0.2/"
        self.sec = None
        self.auth(email, password)
        self.cfg_setup()

    def api_call(self, endpoint: str, **kwargs):
        if endpoint == "user/login":
            params = {"email": kwargs["email"], "password": kwargs["pwd"], "app_id": self.id}
        elif endpoint == "track/get": params = {"track_id": kwargs["id"]}
        elif endpoint == "album/get": params = {"album_id": kwargs["id"]}
        elif endpoint == "playlist/get":
            params = {"extra": "tracks", "playlist_id": kwargs["id"], "limit": 500, "offset": kwargs["offset"]}
        elif endpoint == "artist/get":
            params = {"app_id": self.id, "artist_id": kwargs["id"], "limit": 500, "offset": kwargs["offset"], "extra": "albums"}
        elif endpoint == "label/get":
            params = {"label_id": kwargs["id"], "limit": 500, "offset": kwargs["offset"], "extra": "albums"}
        elif endpoint == "favorite/getUserFavorites":
            unix = time.time()
            sig = "favoritegetUserFavorites" + str(unix) + kwargs["sec"]
            params = {"app_id": self.id, "user_auth_token": self.uat,
                      "type": kwargs.get("type", "albums"), "offset": kwargs.get("offset", 0),
                      "limit": kwargs.get("limit", 50), "request_ts": unix,
                      "request_sig": hashlib.md5(sig.encode("utf-8")).hexdigest()}
        elif endpoint == "track/getFileUrl":
            unix, track_id, fmt_id = time.time(), kwargs["id"], kwargs["fmt_id"]
            if int(fmt_id) not in (5, 6, 7, 27):
                raise InvalidQuality("format_id must be 5, 6, 7, or 27")
            sig = "trackgetFileUrlformat_id{}intentstreamtrack_id{}{}{}".format(
                fmt_id, track_id, unix, kwargs.get("sec", self.sec))
            params = {"request_ts": unix, "request_sig": hashlib.md5(sig.encode("utf-8")).hexdigest(),
                      "track_id": track_id, "format_id": fmt_id, "intent": "stream"}
        else:
            params = kwargs
        try:
            response = self.session.get(self.base + endpoint, params=params, timeout=30)
        except requests.RequestException as exc:
            raise RuntimeError("Unable to reach Qobuz API") from exc
        if endpoint == "user/login":
            if response.status_code == 401: raise AuthenticationError("Invalid Qobuz credentials")
            if response.status_code == 400: raise InvalidAppIdError("Invalid app ID")
        elif endpoint in ("track/getFileUrl", "favorite/getUserFavorites") and response.status_code == 400:
            raise InvalidAppSecretError("Qobuz rejected the app signature/secret")
        response.raise_for_status()
        try: return response.json()
        except ValueError as exc: raise RuntimeError("Qobuz returned invalid JSON") from exc

    def auth(self, email, password):
        data = self.api_call("user/login", email=email, pwd=password)
        user = data.get("user", {})
        credential = user.get("credential", {})
        parameters = credential.get("parameters") or {}
        if not parameters:
            raise IneligibleError("The configured account is not eligible for this operation")
        self.uat = data["user_auth_token"]
        self.session.headers.update({"X-User-Auth-Token": self.uat})
        self.label = parameters.get("short_label", "unknown")

    def cfg_setup(self):
        # Tests only the explicitly supplied app secrets; it does not discover secrets.
        for secret in self.secrets:
            if not secret: continue
            try:
                self.api_call("track/getFileUrl", id=5966783, fmt_id=5, sec=secret)
                self.sec = secret
                break
            except InvalidAppSecretError:
                continue
            except requests.HTTPError:
                # A different upstream failure doesn't prove that the secret is invalid.
                continue
        if self.sec is None:
            raise InvalidAppSecretError("None of the configured QOBUZ_APP_SECRETS values was accepted")

    def multi_meta(self, endpoint, key, item_id, item_type=None):
        offset, remaining = 0, 1
        while remaining > 0:
            if item_type in ("tracks", "albums"):
                data = self.api_call(endpoint, id=item_id, offset=offset, type=item_type)[item_type]
            else:
                data = self.api_call(endpoint, id=item_id, offset=offset)
            yield data
            if offset == 0:
                remaining = max(0, int(data.get(key, 0)) - 500)
            else:
                remaining -= 500
            offset += 500

    def get_album_meta(self, id): return self.api_call("album/get", id=id)
    def get_track_meta(self, id): return self.api_call("track/get", id=id)
    def get_track_url(self, id, fmt_id): return self.api_call("track/getFileUrl", id=id, fmt_id=fmt_id)

    def get_track_lyrics(self, track_id):
        """Fetch Qobuz's signed lyrics URL, then resolve the actual lyrics JSON.

        Qobuz's lyricsUrl endpoint is signed independently from getFileUrl. The
        returned CloudFront URL is short-lived, so resolve it server-side rather
        than caching it in the browser.
        """
        if not self.sec:
            raise InvalidAppSecretError("No accepted Qobuz app secret is configured")
        try:
            track_id = int(track_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("track_id must be an integer") from exc

        request_ts = int(time.time())
        signature_source = f"tracklyricsUrltrack_id{track_id}{request_ts}{self.sec}"
        request_sig = hashlib.md5(signature_source.encode("utf-8")).hexdigest()
        headers = {
            "Authorization": f"Bearer {self.uat}",
            "X-Device-Platform": "android",
            "X-Device-Model": "ChatGPT-HiFi-Player",
            "X-Device-OS-Version": "16",
            "X-App-Version": "10.1.2.0",
            "User-Agent": "Dalvik/2.1.0 (Linux; U; Android 16) QobuzMobileAndroid/10.1.2.0",
            "Accept-Encoding": "gzip",
        }
        params = {
            "track_id": track_id,
            "app_id": self.id,
            "request_ts": request_ts,
            "request_sig": request_sig,
        }
        try:
            response = self.session.get(self.base + "track/lyricsUrl", params=params,
                                        headers=headers, timeout=25)
            response.raise_for_status()
            url_data = response.json()
            lyrics_url = url_data.get("lyrics_url") if isinstance(url_data, dict) else None
            if not lyrics_url:
                raise RuntimeError("Qobuz did not return lyrics_url")
            lyrics_response = requests.get(lyrics_url, headers={
                "User-Agent": headers["User-Agent"], "Accept-Encoding": "gzip",
            }, timeout=25)
            lyrics_response.raise_for_status()
            lyrics_data = lyrics_response.json()
        except requests.RequestException as exc:
            raise RuntimeError("Unable to retrieve Qobuz lyrics") from exc
        except ValueError as exc:
            raise RuntimeError("Qobuz returned invalid lyrics JSON") from exc

        original = lyrics_data.get("original") or {}
        source_lines = original.get("lines") or []
        synced = []
        plain_lines = []
        lrc_lines = []
        for line in source_lines:
            if not isinstance(line, dict):
                continue
            text = str(line.get("line") or "").strip()
            if not text:
                # Keep paragraph breaks in plain text but don't create timed blanks.
                if plain_lines and plain_lines[-1] != "":
                    plain_lines.append("")
                continue
            plain_lines.append(text)
            start = line.get("start")
            if start is None:
                continue
            try:
                seconds = max(0.0, float(start) / 1000.0)
            except (TypeError, ValueError):
                continue
            synced.append({"t": seconds, "text": text})
            minutes = int(seconds // 60)
            remainder = seconds - minutes * 60
            lrc_lines.append(f"[{minutes:02d}:{remainder:05.2f}]{text}")
        plain_text = "\n".join(plain_lines).strip()
        lrc_text = "\n".join(lrc_lines)
        return {
            "track_id": lyrics_data.get("track_id", track_id),
            "album_id": lyrics_data.get("album_id"),
            "language": original.get("lang"),
            "type": original.get("type"),
            "lyrics": plain_text or None,
            "subtitles": lrc_text or None,
            "synced": synced or None,
            "original": original,
            "translation_langs": lyrics_data.get("translation_langs", []),
            "publishers": lyrics_data.get("publishers", []),
            "writers": lyrics_data.get("writers"),
        }

    def get_artist_meta(self, id): return self.multi_meta("artist/get", "albums_count", id)
    def get_plist_meta(self, id): return self.multi_meta("playlist/get", "tracks_count", id)
    def get_label_meta(self, id): return self.multi_meta("label/get", "albums_count", id)
    def search_albums(self, query, limit): return self.api_call("album/search", query=query, limit=limit)
    def search_artists(self, query, limit): return self.api_call("artist/search", query=query, limit=limit)
    def search_playlists(self, query, limit): return self.api_call("playlist/search", query=query, limit=limit)
    def search_tracks(self, query, limit): return self.api_call("track/search", query=query, limit=limit)
    def get_favorite_albums(self, offset, limit): return self.api_call("favorite/getUserFavorites", type="albums", offset=offset, limit=limit, sec=self.sec)
    def get_favorite_tracks(self, offset, limit): return self.api_call("favorite/getUserFavorites", type="tracks", offset=offset, limit=limit, sec=self.sec)
    def get_favorite_artists(self, offset, limit): return self.api_call("favorite/getUserFavorites", type="artists", offset=offset, limit=limit, sec=self.sec)
    def get_user_playlists(self, limit): return self.api_call("playlist/getUserPlaylists", limit=limit)

router = APIRouter(tags=["Qobuz"])
_client: Optional[QobuzClient] = None

def get_client() -> QobuzClient:
    global _client
    if _client is not None: return _client
    email, password = os.getenv("QOBUZ_EMAIL", "").strip(), os.getenv("QOBUZ_PASSWORD", "")
    app_id = os.getenv("QOBUZ_APP_ID", "").strip()
    secrets = [s.strip() for s in os.getenv("QOBUZ_APP_SECRETS", "").split(",") if s.strip()]
    missing = [k for k, v in {"QOBUZ_EMAIL": email, "QOBUZ_PASSWORD": password,
                              "QOBUZ_APP_ID": app_id, "QOBUZ_APP_SECRETS": secrets}.items() if not v]
    if missing: raise HTTPException(status_code=503, detail="Missing environment configuration: " + ", ".join(missing))
    try:
        _client = QobuzClient(email, password, app_id, secrets)
        return _client
    except (AuthenticationError, IneligibleError, InvalidAppIdError, InvalidAppSecretError) as exc:
        log.warning("Client initialization failed: %s", type(exc).__name__)
        raise HTTPException(status_code=502, detail=f"Qobuz client initialization failed: {type(exc).__name__}") from exc
    except Exception as exc:
        log.warning("Client initialization error: %s", type(exc).__name__)
        raise HTTPException(status_code=502, detail="Qobuz initialization failed; check credentials and upstream availability") from exc

def call(fn, *args, **kwargs):
    try: return fn(*args, **kwargs)
    except HTTPException: raise
    except InvalidQuality as exc: raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (AuthenticationError, IneligibleError, InvalidAppIdError, InvalidAppSecretError) as exc:
        raise HTTPException(status_code=502, detail=f"Qobuz request failed: {type(exc).__name__}") from exc
    except Exception as exc:
        log.warning("Upstream request failed: %s", type(exc).__name__)
        raise HTTPException(status_code=502, detail="Qobuz request failed; check IDs, access rights, and upstream response") from exc

@router.get("/")
@router.get("/api")
def root(): return {"name": "Qobuz REST API", "version": "2.1.0", "docs": "/docs", "health": "/qobuz/health", "endpoints": {"search": "/qobuz/search/{kind}?q=...&limit=20", "track": "/qobuz/tracks/{track_id}", "track_file_url": "/qobuz/tracks/{track_id}/file-url?format_id=7", "lyrics": "/qobuz/tracks/{track_id}/lyrics", "album": "/qobuz/albums/{album_id}", "artist_albums": "/qobuz/artists/{artist_id}/albums", "playlist": "/qobuz/playlists/{playlist_id}", "user_playlists": "/qobuz/playlists", "label_albums": "/qobuz/labels/{label_id}/albums", "favorites": "/qobuz/favorites/{kind}"}}
@router.get("/health")
def health():
    return {
        "status": "ok",
        "client_initialized": _client is not None,
        "json_secret_configured": bool(_JSON_SECRET_NAME),
        "json_secret_parsed": _JSON_CONFIG_OK,
        "json_secret_error": _JSON_CONFIG_ERROR,
        "required_config_present": {
            "QOBUZ_EMAIL": bool(os.getenv("QOBUZ_EMAIL", "").strip()),
            "QOBUZ_PASSWORD": bool(os.getenv("QOBUZ_PASSWORD", "")),
            "QOBUZ_APP_ID": bool(os.getenv("QOBUZ_APP_ID", "").strip()),
            "QOBUZ_APP_SECRETS": bool(os.getenv("QOBUZ_APP_SECRETS", "").strip()),
        },
    }
@router.get("/search/{kind}")
def search(kind: str, q: str = Query(min_length=1, max_length=300), limit: int = Query(20, ge=1, le=50)):
    c = get_client(); methods = {"tracks": c.search_tracks, "albums": c.search_albums, "artists": c.search_artists, "playlists": c.search_playlists}
    if kind not in methods: raise HTTPException(status_code=404, detail="kind must be tracks, albums, artists, or playlists")
    return call(methods[kind], q, limit)
@router.get("/tracks/{track_id}")
def track_metadata(track_id: str): return call(get_client().get_track_meta, track_id)
@router.get("/albums/{album_id}")
def album_metadata(album_id: str): return call(get_client().get_album_meta, album_id)
@router.get("/tracks/{track_id}/file-url")
def track_file_url(track_id: str, format_id: int = Query(7, description="Qobuz format ID: 5, 6, 7, or 27")):
    if format_id not in (5, 6, 7, 27): raise HTTPException(status_code=422, detail="format_id must be one of 5, 6, 7, 27")
    return call(get_client().get_track_url, track_id, format_id)
@router.get("/tracks/{track_id}/lyrics")
def track_lyrics(track_id: str):
    """Return Qobuz synced and plain lyrics for playback and file tagging."""
    try:
        return call(get_client().get_track_lyrics, track_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
@router.get("/artists/{artist_id}/albums")
def artist_albums(artist_id: str): return {"pages": call(lambda: list(get_client().get_artist_meta(artist_id)))}
@router.get("/playlists/{playlist_id}")
def playlist(playlist_id: str): return {"pages": call(lambda: list(get_client().get_plist_meta(playlist_id)))}
@router.get("/labels/{label_id}/albums")
def label_albums(label_id: str): return {"pages": call(lambda: list(get_client().get_label_meta(label_id)))}
@router.get("/favorites/{kind}")
def favorites(kind: str, offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=500)):
    c = get_client(); methods = {"albums": c.get_favorite_albums, "tracks": c.get_favorite_tracks, "artists": c.get_favorite_artists}
    if kind not in methods: raise HTTPException(status_code=404, detail="kind must be albums, tracks, or artists")
    return call(methods[kind], offset, limit)
@router.get("/playlists")
def user_playlists(limit: int = Query(50, ge=1, le=500)): return call(get_client().get_user_playlists, limit)

