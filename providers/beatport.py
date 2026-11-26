"""Beatport catalog client with server-side login and automatic token refresh."""
import os, threading
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse, parse_qs
import requests

class BeatportError(Exception): pass

class BeatportClient:
    def __init__(self):
        self.api_url = os.getenv('BEATPORT_API_URL', 'https://api.beatport.com/v4/').rstrip('/') + '/'
        # Defaults retained from the original working Beatport integration.
        # Environment variables are optional overrides, not required HF Secrets.
        self.client_id = os.getenv(
            'BEATPORT_CLIENT_ID', 'Zy2K9Wvy6DkUds7g8s1GNMHfk17E5Ch2BWHlyaGY'
        ).strip()
        self.redirect_uri = os.getenv(
            'BEATPORT_REDIRECT_URI', 'seratodjlite://beatport'
        ).strip()
        self.access_token = self.refresh_token = self.expires = None
        self.session = requests.Session()
        self.session.headers.update({'User-Agent': os.getenv('BEATPORT_USER_AGENT', 'BeatportAPI/1.0')})
        self.lock = threading.RLock()

    def login(self, username, password):
        params = {'client_id': self.client_id, 'response_type': 'code', 'redirect_uri': self.redirect_uri}
        headers = {'User-Agent': os.getenv('BEATPORT_LOGIN_USER_AGENT', 'Mozilla/5.0')}
        r = self.session.get(self.api_url+'auth/o/authorize/', params=params, headers=headers, allow_redirects=False, timeout=30)
        if r.status_code != 302: raise RuntimeError(f'Beatport authorize failed: HTTP {r.status_code}')
        referer = r.url.rsplit('/', 1)[0] + r.headers.get('location', '')
        r = self.session.post(self.api_url+'auth/login/', json={'username': username, 'password': password}, headers={**headers, 'Referer': referer}, timeout=30)
        if r.status_code != 200: raise RuntimeError(f'Beatport login failed: HTTP {r.status_code}')
        r = self.session.get(self.api_url+'auth/o/authorize/', params=params, headers=headers, allow_redirects=False, timeout=30)
        if r.status_code != 302: raise RuntimeError(f'Beatport authorization-code request failed: HTTP {r.status_code}')
        code = parse_qs(urlparse(r.headers.get('location', '')).query).get('code', [None])[0]
        if not code: raise RuntimeError('Beatport did not return an authorization code')
        r = self.session.post(self.api_url+'auth/o/token/', data={'client_id': self.client_id, 'code': code, 'grant_type': 'authorization_code', 'redirect_uri': self.redirect_uri}, timeout=30)
        if r.status_code != 200: raise RuntimeError(f'Beatport token exchange failed: HTTP {r.status_code}')
        data = r.json(); self._set_tokens(data); return data

    def _set_tokens(self, data):
        self.access_token = data.get('access_token', self.access_token)
        self.refresh_token = data.get('refresh_token', self.refresh_token)
        if data.get('expires_in'): self.expires = datetime.now(timezone.utc) + timedelta(seconds=int(data['expires_in']))

    def refresh(self):
        if not self.refresh_token: raise RuntimeError('No Beatport refresh token; login again')
        r = self.session.post(self.api_url+'auth/o/token/', data={'client_id': self.client_id, 'refresh_token': self.refresh_token, 'grant_type': 'refresh_token'}, timeout=30)
        if r.status_code != 200: raise RuntimeError(f'Beatport refresh failed: HTTP {r.status_code}')
        data = r.json(); self._set_tokens(data); return data

    def request(self, endpoint, params=None):
        with self.lock:
            if not self.access_token: raise RuntimeError('Beatport is not logged in. Use POST /beatport/auth/login.')
            if self.expires and datetime.now(timezone.utc) >= self.expires: self.refresh()
            url = self.api_url + endpoint.lstrip('/')
            r = self.session.get(url, params=params or {}, headers={'Authorization': f'Bearer {self.access_token}'}, timeout=30)
            if r.status_code == 401:
                self.refresh()
                r = self.session.get(url, params=params or {}, headers={'Authorization': f'Bearer {self.access_token}'}, timeout=30)
            if r.status_code == 403 and 'Territory' in r.text: raise BeatportError('Track or release is territory restricted')
            if r.status_code not in (200, 201, 202): raise RuntimeError(f'Beatport API returned HTTP {r.status_code}: {r.text[:300]}')
            return r.json()

    def status(self):
        return {'authenticated': bool(self.access_token), 'expires_at': self.expires.isoformat() if self.expires else None, 'refresh_available': bool(self.refresh_token)}

beatport = BeatportClient()
