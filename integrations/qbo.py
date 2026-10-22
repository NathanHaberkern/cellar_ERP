"""QuickBooks Online API client, shared by the whole ERP (shipping now; deposits,
costing exports later -- one client, one token store, like commerce7.py).

API facts this relies on (developer.intuit.com):
    Authorize   https://appcenter.intuit.com/connect/oauth2  (scope com.intuit.quickbooks.accounting)
    Token       POST https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer
                HTTP Basic client_id:client_secret; grant_type=authorization_code | refresh_token
                Access token ~1 h. Refresh token ~100 days, and a NEW one comes back on
                every refresh -- always store the latest (QBOConnection).
    API         https://quickbooks.api.intuit.com/v3/company/{realmId}/...   (sandbox:
                https://sandbox-quickbooks.api.intuit.com). Query: GET query?query=SELECT...
                paged with STARTPOSITION / MAXRESULTS (max 1000).
                Sparse update: POST invoice {"Id", "SyncToken", "sparse": true, ...}.
    Revoke      POST https://developer.api.intuit.com/v2/oauth2/tokens/revoke {"token": ...}
    Errors      every response carries an `intuit_tid` header; it goes into each error
                and log line so Intuit support can trace a failed call. 429 (throttled)
                and 500/502/503/504 are retried with backoff; a write with a stale
                SyncToken fails rather than applying twice, so retrying writes is safe.

App credentials come only from settings.QBO (the environment). The Connect page
(integrations/views.py) does the one-time consent and saves the first tokens.
"""
from __future__ import annotations

import logging
import secrets
import time
from datetime import timedelta
from urllib.parse import urlencode

import requests
from django.conf import settings
from django.db import transaction
from django.utils import timezone

log = logging.getLogger(__name__)

AUTHORIZE_URL = "https://appcenter.intuit.com/connect/oauth2"
TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
REVOKE_URL = "https://developer.api.intuit.com/v2/oauth2/tokens/revoke"
API_BASE = {"sandbox": "https://sandbox-quickbooks.api.intuit.com",
            "production": "https://quickbooks.api.intuit.com"}
SCOPE = "com.intuit.quickbooks.accounting"
MINOR_VERSION = "75"
TIMEOUT_S = 30
PAGE = 1000
RETRY_STATUSES = {429, 500, 502, 503, 504}
RETRY_DELAYS_S = (2, 5, 15)
_sleep = time.sleep                      # patched in tests


class QBOError(Exception):
    def __init__(self, message, status_code=None, body=None, intuit_tid=""):
        super().__init__(message)
        self.status_code = status_code
        self.body = body
        self.intuit_tid = intuit_tid


class QBOConfigError(QBOError):
    """App keys missing, or QuickBooks not connected yet / connection expired."""


def _conf():
    c = settings.QBO
    missing = [n for n, v in (("QBO_CLIENT_ID", c["CLIENT_ID"]),
                              ("QBO_CLIENT_SECRET", c["CLIENT_SECRET"])) if not v]
    if missing:
        raise QBOConfigError(f"Missing in .env / config vars: {', '.join(missing)}")
    if c["ENV"] not in API_BASE:
        raise QBOConfigError(f"QBO_ENV must be sandbox or production, not {c['ENV']!r}")
    return c


# --- one-time connect (OAuth authorization code) ------------------------------------

def authorize_url(state: str | None = None) -> tuple[str, str]:
    c = _conf()
    state = state or secrets.token_urlsafe(24)
    q = urlencode({"client_id": c["CLIENT_ID"], "response_type": "code", "scope": SCOPE,
                   "redirect_uri": c["REDIRECT_URI"], "state": state})
    return f"{AUTHORIZE_URL}?{q}", state


def _token_call(data, session=None):
    c = _conf()
    resp = (session or requests).post(
        TOKEN_URL, data=data, auth=(c["CLIENT_ID"], c["CLIENT_SECRET"]), timeout=TIMEOUT_S,
        headers={"Accept": "application/json",
                 "Content-Type": "application/x-www-form-urlencoded"})
    try:
        body = resp.json()
    except ValueError:
        body = {"error": resp.text[:300]}
    if resp.status_code != 200 or "access_token" not in body:
        err = body.get("error_description") or body.get("error") or f"HTTP {resp.status_code}"
        if body.get("error") == "invalid_grant":
            raise QBOConfigError("QuickBooks connection expired or was revoked -- reconnect "
                                 "on the QuickBooks connection page.", resp.status_code, body)
        raise QBOConfigError(f"QuickBooks token request failed: {err}", resp.status_code, body)
    return body


def _save_tokens(conn, body):
    now = timezone.now()
    conn.access_token = body["access_token"]
    conn.access_expires_at = now + timedelta(seconds=int(body.get("expires_in", 3600)) - 120)
    conn.refresh_token = body.get("refresh_token") or conn.refresh_token
    x = body.get("x_refresh_token_expires_in")
    if x:
        conn.refresh_expires_at = now + timedelta(seconds=int(x))
    conn.save()
    return conn


def complete_connect(code: str, realm_id: str, session=None):
    """Exchange the callback's code for tokens and store them (replaces any old connection)."""
    from integrations.models import QBOConnection
    c = _conf()
    body = _token_call({"grant_type": "authorization_code", "code": code,
                        "redirect_uri": c["REDIRECT_URI"]}, session=session)
    conn, _ = QBOConnection.objects.get_or_create(
        environment=c["ENV"], defaults={"realm_id": realm_id, "refresh_token": ""})
    conn.realm_id = realm_id
    _save_tokens(conn, body)
    try:
        info = QBOClient(session=session).get(f"companyinfo/{realm_id}")
        conn.company_name = ((info.get("CompanyInfo") or {}).get("CompanyName") or "")[:120]
        conn.save(update_fields=["company_name", "updated_at"])
    except QBOError:
        log.warning("Connected to QBO but couldn't read the company name")
    return conn


def disconnect(session=None) -> bool:
    """Revoke the tokens at Intuit and forget the connection. Returns True if
    Intuit confirmed the revoke; the local row is deleted either way."""
    conn = connection()
    if conn is None:
        return False
    c = _conf()
    revoked = False
    try:
        resp = (session or requests).post(
            REVOKE_URL, json={"token": conn.refresh_token}, timeout=TIMEOUT_S,
            auth=(c["CLIENT_ID"], c["CLIENT_SECRET"]),
            headers={"Accept": "application/json", "Content-Type": "application/json"})
        revoked = resp.status_code == 200
        if not revoked:
            log.warning("QBO revoke returned HTTP %s (intuit_tid %s)", resp.status_code,
                        _tid(resp))
    except requests.RequestException as e:
        log.warning("QBO revoke failed: %s", e)
    conn.delete()
    return revoked


def _tid(resp) -> str:
    return (getattr(resp, "headers", None) or {}).get("intuit_tid", "")


def connection():
    from integrations.models import QBOConnection
    return QBOConnection.objects.filter(environment=settings.QBO["ENV"]).first()


# --- client ---------------------------------------------------------------------------

class QBOClient:
    def __init__(self, session=None):
        _conf()
        self.session = session or requests.Session()
        conn = connection()
        if conn is None:
            raise QBOConfigError("QuickBooks isn't connected yet -- an admin connects it once "
                                 "at /integrations/qbo/.")
        self.realm_id = conn.realm_id
        self.base = f"{API_BASE[settings.QBO['ENV']]}/v3/company/{self.realm_id}"

    def _access_token(self, force=False):
        from integrations.models import QBOConnection
        with transaction.atomic():
            # Row lock: two refreshes at once would each get a new refresh token
            # and the loser's save would store a dead one.
            conn = QBOConnection.objects.select_for_update().get(
                environment=settings.QBO["ENV"])
            if (not force and conn.access_token and conn.access_expires_at
                    and conn.access_expires_at > timezone.now()):
                return conn.access_token
            body = _token_call({"grant_type": "refresh_token",
                                "refresh_token": conn.refresh_token}, session=self.session)
            _save_tokens(conn, body)
            return conn.access_token

    def request(self, method, path, *, params=None, json=None):
        params = dict(params or {}, minorversion=MINOR_VERSION)
        refreshed, delays = False, iter(RETRY_DELAYS_S)
        while True:
            resp = self.session.request(
                method, f"{self.base}/{path.lstrip('/')}", params=params, json=json,
                timeout=TIMEOUT_S,
                headers={"Authorization": f"Bearer {self._access_token(force=refreshed)}",
                         "Accept": "application/json", "Content-Type": "application/json"})
            if resp.status_code == 401 and not refreshed:
                refreshed = True             # token revoked early: refresh once and retry
                continue
            if resp.status_code in RETRY_STATUSES:
                delay = next(delays, None)
                if delay is not None:
                    log.warning("QBO %s %s -> %s (intuit_tid %s), retrying in %ss", method,
                                path, resp.status_code, _tid(resp), delay)
                    _sleep(delay)
                    continue
            break
        try:
            body = resp.json()
        except ValueError:
            body = {"text": resp.text[:500]}
        if resp.status_code >= 400 or body.get("Fault"):
            errs = ((body.get("Fault") or {}).get("Error") or [{}])
            msg = errs[0].get("Detail") or errs[0].get("Message") or f"HTTP {resp.status_code}"
            tid = _tid(resp)
            log.error("QBO %s %s -> %s: %s (intuit_tid %s)", method, path, resp.status_code,
                      msg, tid)
            raise QBOError(f"QBO {method} {path} -> {resp.status_code}: {msg}"
                           + (f" (intuit_tid {tid})" if tid else ""),
                           resp.status_code, body, intuit_tid=tid)
        return body

    def get(self, path, params=None):
        return self.request("GET", path, params=params)

    def query(self, select: str, entity: str):
        """Yield every row of `SELECT * FROM <entity> ...`, following STARTPOSITION pages."""
        start = 1
        while True:
            q = f"{select} STARTPOSITION {start} MAXRESULTS {PAGE}"
            rows = (self.get("query", {"query": q}).get("QueryResponse") or {}).get(entity) or []
            yield from rows
            if len(rows) < PAGE:
                return
            start += PAGE

    def read(self, entity: str, txn_id: str) -> dict:
        return self.get(f"{entity.lower()}/{txn_id}").get(entity) or {}

    def sparse_update(self, entity: str, txn_id: str, sync_token: str, fields: dict) -> dict:
        body = {"Id": txn_id, "SyncToken": sync_token, "sparse": True, **fields}
        return self.request("POST", entity.lower(), json=body).get(entity) or {}
