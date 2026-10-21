"""Test doubles for the carrier HTTP layer.

FakeSession stands in for requests.Session: it records every call and replays
canned responses shaped like FedEx's real sandbox output, so the adapter's
request building and response parsing are tested without network or keys.
"""
import base64
import json

PDF_BYTES = b"%PDF-1.4 fake label\n%%EOF"
PDF_B64 = base64.b64encode(PDF_BYTES).decode()


class FakeResponse:
    def __init__(self, status_code=200, body=None, text=None):
        self.status_code = status_code
        self._body = body
        self.text = text if text is not None else json.dumps(body or {})

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class FakeSession:
    def __init__(self, responses=None):
        self.calls = []
        self.responses = dict(responses or {})

    def _reply(self, method, url):
        for (m, suffix), resp in self.responses.items():
            if m == method and url.endswith(suffix):
                return resp() if callable(resp) else resp
        raise AssertionError(f"unexpected {method} {url}")

    def post(self, url, **kw):
        self.calls.append(("POST", url, kw))
        return self._reply("POST", url)

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        return self._reply(method, url)


def token_ok():
    return FakeResponse(200, {"access_token": "tok-123", "token_type": "bearer",
                              "expires_in": 3599, "scope": "CXS"})


def ship_ok(tracking="794600000001", docs=1):
    piece_docs = [{"contentType": "LABEL", "copiesToPrint": 1, "encodedLabel": PDF_B64}
                  for _ in range(docs)]
    return FakeResponse(200, {
        "transactionId": "t-1",
        "output": {"transactionShipments": [{
            "serviceType": "FEDEX_GROUND",
            "masterTrackingNumber": tracking,
            "pieceResponses": [{"trackingNumber": tracking, "packageDocuments": piece_docs,
                                "netChargeAmount": 14.2, "currency": "USD"}],
            "completedShipmentDetail": {"shipmentRating": {"shipmentRateDetails": [
                {"totalNetCharge": 13.87, "currency": "USD"}]}},
        }]},
    })


def ship_error(code="SHIPMENT.ALCOHOL.NOTALLOWED", message="Alcohol not permitted"):
    return FakeResponse(400, {"transactionId": "t-2",
                              "errors": [{"code": code, "message": message}]})


def cancel_ok():
    return FakeResponse(200, {"output": {"cancelledShipment": True, "successMessage": "ok"}})


def fedex_session(overrides=None):
    responses = {("POST", "/oauth/token"): token_ok,
                 ("POST", "/ship/v1/shipments"): ship_ok,
                 ("PUT", "/ship/v1/shipments/cancel"): cancel_ok}
    responses.update(overrides or {})
    return FakeSession(responses)
