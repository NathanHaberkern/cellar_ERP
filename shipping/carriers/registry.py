"""Build a configured adapter from settings.SHIPPING.

The one place that reads carrier credentials. Tests override settings or pass
their own adapter into the services; nothing else should construct adapters.
"""
from django.conf import settings

from .base import CarrierConfigError
from .fedex import FedExAdapter
from .gls import GLSAdapter
from .ups import UPSAdapter


def get_adapter(carrier: str):
    conf = settings.SHIPPING
    if carrier == "fedex":
        c = conf["FEDEX"]
        return FedExAdapter(
            environment=c["ENV"], client_id=c["CLIENT_ID"], client_secret=c["CLIENT_SECRET"],
            # FedEx prints account numbers as 1234-5678-9; the API wants digits only.
            account_number="".join(ch for ch in c["ACCOUNT_NUMBER"] if ch.isdigit()),
            alcohol_shipper_agreement=c.get("ALCOHOL_SHIPPER_AGREEMENT", ""),
        )
    if carrier == "ups":
        c = conf["UPS"]
        return UPSAdapter(
            environment=c["ENV"], client_id=c["CLIENT_ID"], client_secret=c["CLIENT_SECRET"],
            # UPS shipper numbers are 6 alphanumerics; strip spaces/dashes people paste.
            account_number="".join(ch for ch in c["ACCOUNT_NUMBER"] if ch.isalnum()).upper(),
        )
    if carrier == "gls":
        c = conf["GLS"]
        return GLSAdapter(environment=c["ENV"], account_number=c["ACCOUNT_NUMBER"].strip(),
                          username=c["USERNAME"], password=c["PASSWORD"],
                          base_url=c.get("BASE_URL", ""))
    raise CarrierConfigError(f"Unknown carrier {carrier!r}")
