"""Commerce7 REST client, shared by the whole ERP (shipping now, case goods/QBO later).

One C7 app serves the ERP, so the client lives here rather than inside
`shipping/`: a second module that needs C7 must not grow a second client with
its own auth and rate-limit handling.

API facts this relies on (developer.commerce7.com):
    Base URL     https://api.commerce7.com/v1/{endpoint}
    Auth         HTTP Basic -- username = C7_APP_ID, password = C7_APP_SECRET --
                 plus a `tenant: <C7_TENANT_ID>` header on every request.
    Pagination   cursor-based on /order, /customer, /club-membership ...:
                 start with ?cursor=start, follow `cursor` until it is absent.
                 Cursor paging is exempt from the 100 req/min tenant limit, which
                 is why we use it instead of ?page=n.
    Order filters  orderPaidDate=gte:YYYY-MM-DD, fulfillmentStatus=Not Fulfilled,
                 complianceStatus=Compliant, orderDeliveryMethod=Ship, channel=Club

Credentials come only from settings.SHIPPING["C7"] (i.e. the environment).
"""
from __future__ import annotations

import logging
import time

import requests
from django.conf import settings

log = logging.getLogger(__name__)

BASE_URL = "https://api.commerce7.com/v1"
TIMEOUT_S = 30
MAX_RETRIES_429 = 3


class C7Error(Exception):
    def __init__(self, message, status_code=None, body=None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class C7ConfigError(C7Error):
    pass


class Commerce7Client:
    def __init__(self, *, app_id, app_secret, tenant_id, session=None, base_url=BASE_URL):
        missing = [n for n, v in (("C7_APP_ID", app_id), ("C7_APP_SECRET", app_secret),
                                  ("C7_TENANT_ID", tenant_id)) if not v]
        if missing:
            raise C7ConfigError(f"Missing in .env / config vars: {', '.join(missing)}")
        self.base_url = base_url.rstrip("/")
        self.session = session or requests.Session()
        self.session.auth = (app_id, app_secret)
        self.session.headers.update({"tenant": tenant_id, "Accept": "application/json"})

    @classmethod
    def from_settings(cls, session=None):
        c = settings.SHIPPING["C7"]
        return cls(app_id=c["APP_ID"], app_secret=c["APP_SECRET"], tenant_id=c["TENANT_ID"],
                   session=session)

    def request(self, method, path, *, params=None, json=None):
        url = f"{self.base_url}/{path.lstrip('/')}"
        for attempt in range(MAX_RETRIES_429 + 1):
            resp = self.session.request(method, url, params=params, json=json, timeout=TIMEOUT_S)
            if resp.status_code == 429 and attempt < MAX_RETRIES_429:
                # Only page-based calls are rate limited; back off and retry.
                time.sleep(2 ** attempt * 5)
                continue
            break
        if resp.status_code >= 400:
            try:
                body = resp.json()
            except ValueError:
                body = resp.text[:500]
            hint = {401: " (check C7_APP_ID / C7_APP_SECRET)",
                    403: " (app not installed on this tenant, or C7_TENANT_ID wrong)"}.get(
                resp.status_code, "")
            raise C7Error(f"C7 {method} /{path.lstrip('/')} -> {resp.status_code}{hint}",
                          resp.status_code, body)
        return resp.json() if resp.text else {}

    def get(self, path, params=None):
        return self.request("GET", path, params=params)

    def iter_pages(self, path, collection_key, params=None, max_records=None):
        """Yield records across cursor pages. Stops at max_records if given."""
        params = dict(params or {})
        params["cursor"] = "start"
        seen = 0
        while True:
            data = self.get(path, dict(params))  # copy: each call keeps its own cursor
            for rec in data.get(collection_key) or []:
                yield rec
                seen += 1
                if max_records is not None and seen >= max_records:
                    return
            cursor = data.get("cursor")
            if not cursor:
                return
            params["cursor"] = cursor
