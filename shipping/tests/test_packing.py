"""Packing rules (services/packing.py) -- pure, no database."""
from datetime import date
from decimal import Decimal

from django.test import SimpleTestCase

from shipping.services.packing import (Box, LineData, OrderData, PackingError, address_key,
                                       build_shipments, is_ups_store, order_from_c7,
                                       plan_boxes, plan_pools, route)

RESTRICTED = ["WV", "VT", "SC", "OR", "ND", "NH", "MO", "MD", "ME", "IN", "ID", "CO"]
DEFAULT = ["23RTEMP", "24SOUZ", "24IRED", "24TN"]


def boxes():
    spec = [("1 Bottle", 1, "0.6", 4), ("2 Bottle", 2, "1.1", 8), ("3 Bottle", 3, "1.9", 12),
            ("4 Bottle", 4, "1.9", 17), ("6 Bottle", 6, "2.3", 22), ("12 Bottle", 12, "4.9", 42)]
    out = [Box(name=n, use="standard", max_bottles=m, tare_lb=Decimal(t),
               max_weight_lb=Decimal(w), pk=i) for i, (n, m, t, w) in enumerate(spec, 1)]
    out.append(Box(name="Port", use="port_club", max_bottles=1, tare_lb=Decimal("2.0"),
                   max_weight_lb=Decimal("8"), pk=7))
    out.append(Box(name="Magnum", use="magnum", max_bottles=1, tare_lb=Decimal("0.7"),
                   max_weight_lb=Decimal("10"), pk=8))
    return out


def weight(ml):
    return {750: Decimal("3.0"), 500: Decimal("2.16"), 1500: Decimal("7.5")}[ml]


def order(number, n=None, *, state="CA", name="Pat Lee", street="1 Main St", zip_="95240",
          lines=None, **kw):
    lines = lines or [LineData(sku="23MFZ", title="2023 Zinfandel", quantity=n, ml=750)]
    return OrderData(key=str(number), number=str(number), name=name, street1=street,
                     city="Lodi", state=state, postal_code=zip_, lines=lines, **kw)


def default_order(number, **kw):
    return order(number, lines=[LineData(sku=s, title=s, quantity=1, ml=750) for s in DEFAULT],
                 **kw)


class BoxFitTests(SimpleTestCase):
    def fit(self, *orders):
        return plan_boxes(list(orders), boxes(), weight)

    def test_closest_fit(self):
        for n, box in [(1, "1 Bottle"), (4, "4 Bottle"), (5, "6 Bottle"), (7, "12 Bottle"),
                       (12, "12 Bottle")]:
            pk = self.fit(order(1, n))
            self.assertEqual([p.box.name for p in pk], [box], n)

    def test_big_order_fills_twelves_then_closest(self):
        pk = self.fit(order(1, 42))
        self.assertEqual([p.box.name for p in pk], ["12 Bottle"] * 3 + ["6 Bottle"])
        self.assertEqual(sum(p.bottle_count for p in pk), 42)

    def test_weight_is_tare_plus_bottles_capped(self):
        (pk,) = self.fit(order(1, 4))
        self.assertEqual(pk.weight_lb, Decimal("13.9"))          # 1.9 + 4 x 3.0
        (pk,) = self.fit(order(1, 12))
        self.assertEqual(pk.weight_lb, Decimal("40.9"))          # 4.9 + 36 (under 42 cap)

    def test_magnum_alone_rest_boxed_normally(self):
        o = order(1, lines=[LineData("MAG", "Magnum", 1, 1500),
                            LineData("23MFZ", "Zin", 3, 750)])
        names = sorted(p.box.name for p in self.fit(o))
        self.assertEqual(names, ["3 Bottle", "Magnum"])

    def test_port_club_volume_uses_port_box_other_port_does_not(self):
        o = order(1, lines=[LineData("PCV12", "Port Club Volume 12", 1, 500),
                            LineData("NV-PORT", "Tawny Port", 1, 500)])
        names = sorted(p.box.name for p in self.fit(o))
        self.assertEqual(names, ["1 Bottle", "Port"])

    def test_unknown_size_raises(self):
        with self.assertRaisesMessage(PackingError, "bottle size unknown"):
            self.fit(order(1, lines=[LineData("X", "x", 1, None)]))

    def test_bottles_per_unit(self):
        o = order(1, lines=[LineData("3PK", "Three pack", 2, 750, bottles_per_unit=3)])
        (pk,) = self.fit(o)
        self.assertEqual((pk.box.name, pk.bottle_count), ("6 Bottle", 6))


class RoutingTests(SimpleTestCase):
    def test_restricted_state_goes_ups(self):
        self.assertEqual(route(order(1, 4, state="OR"), RESTRICTED)[0], "ups")
        self.assertEqual(route(order(1, 4, state="CA"), RESTRICTED), ("fedex", ""))

    def test_ups_store_goes_ups(self):
        o = order(1, 4, street="123 Main St", state="TX")
        o.company = "The UPS Store #1234"
        self.assertTrue(is_ups_store(o))
        self.assertEqual(route(o, RESTRICTED), ("ups", "UPS Store / mailbox address"))


class OverrideTests(SimpleTestCase):
    def test_pmb_address_goes_ups(self):
        o = order(1, 4, street="2425 W Kettleman Ln Ste 4 PMB 123")
        self.assertEqual(route(o, RESTRICTED), ("ups", "UPS Store / mailbox address"))

    def test_hand_override(self):
        o = order(1, 4)
        o.carrier_override = "ups"
        self.assertEqual(route(o, RESTRICTED)[0], "ups")
        o = order(2, 4, street="1 Main St PMB 9")
        o.carrier_override = "fedex"
        self.assertEqual(route(o, RESTRICTED)[0], "fedex")

    def test_fedex_never_forced_into_restricted_state(self):
        o = order(1, 4, state="CO")
        o.carrier_override = "fedex"
        self.assertEqual(route(o, RESTRICTED)[0], "ups")


class CombiningTests(SimpleTestCase):
    def ship(self, orders):
        shipments, problems = build_shipments(orders, boxes(), weight, RESTRICTED)
        self.assertEqual(problems, [])
        return shipments

    def test_same_name_same_address_share_a_box(self):
        a = order(1, 4)
        b = order(2, 6, street="1 Main Street")       # formatting differs, same place
        (s,) = self.ship([a, b])
        self.assertEqual([p.box.name for p in s.packages], ["12 Bottle"])
        self.assertEqual(s.numbers, ["1", "2"])
        self.assertIn("combined 2 orders", s.carrier_reason)

    def test_same_address_different_name_separate(self):
        a, b = order(1, 4), order(2, 6, name="Sam Lee")
        self.assertEqual(len(self.ship([a, b])), 2)

    def test_order_with_nothing_to_box_is_a_problem_not_a_crash(self):
        empty = order(9, street="9 Elm St", lines=[LineData("23MFZ", "Zin", 0, 750)])
        shipments, problems = build_shipments([empty, order(1, 4)], boxes(), weight, RESTRICTED)
        self.assertEqual(problems, [("9", "no shippable bottles")])
        self.assertEqual(len(plan_pools(shipments, default_skus=DEFAULT)), 1)

    def test_different_unit_number_not_combined(self):
        a = order(1, 4)
        b = order(2, 4)
        b.street2 = "Apt 2"
        self.assertEqual(len(self.ship([a, b])), 2)

    def test_address_key_ignores_case_suffix_and_zip4(self):
        a = order(1, 1, street="12 North Oak Avenue", zip_="95240-1234")
        b = order(2, 1, street="12 n oak ave", zip_="95240")
        self.assertEqual(address_key(a), address_key(b))


class PoolTests(SimpleTestCase):
    def pools(self, orders, **kw):
        shipments, _ = build_shipments(orders, boxes(), weight, RESTRICTED)
        return plan_pools(shipments, default_skus=DEFAULT, **kw)

    def test_carriers_never_mixed_and_default_pool(self):
        orders = ([default_order(i, street=f"{i} Elm St") for i in range(1, 31)]
                  + [default_order(100, state="OR", street="9 Pine St")]
                  + [order(200 + i, 12, street=f"{i} Oak St") for i in range(5)])
        pools = self.pools(orders)
        names = [p.name for p in pools]
        self.assertIn("FedEx Default", names)
        self.assertIn("UPS Default", names)
        fedex_default = next(p for p in pools if p.name == "FedEx Default")
        self.assertEqual(fedex_default.package_count, 30)      # all in one, whatever size
        for p in pools:
            self.assertEqual({s.carrier for s in p.shipments}, {p.carrier})

    def test_edited_default_package_not_in_default_pool(self):
        edited = order(1, lines=[LineData(s, s, 1, 750) for s in DEFAULT]
                       + [LineData("25VERD", "Verdelho", 2, 750)])
        (p,) = self.pools([edited])
        self.assertFalse(p.is_default)

    def test_pools_sized_in_cases_even_and_within_max(self):
        orders = [order(i, 12, street=f"{i} Elm St") for i in range(1, 46)]   # 45 cases
        pools = self.pools(orders)
        self.assertEqual([p.cases for p in pools], [15, 15, 15])
        small = [order(i, 4, street=f"{i} Oak St") for i in range(1, 61)]      # 60 x 4 = 20 cases
        (p,) = self.pools(small)
        self.assertEqual((p.cases, p.package_count), (20, 60))

    def test_same_address_different_names_same_pool(self):
        orders = [order(i, 12, street=f"{i} Elm St") for i in range(1, 40)]
        orders.append(order(999, 4, street="5 Elm St", name="Other Person"))
        pools = self.pools(orders)
        home = [p.name for p in pools for s in p.shipments if s.orders[0].street1 == "5 Elm St"]
        self.assertEqual(len(home), 2)
        self.assertEqual(len(set(home)), 1)

    def test_identical_boxes_sit_together(self):
        orders = [order(1, 4, street="1 A St"), order(2, 12, street="2 B St"),
                  order(3, 4, street="3 C St"), order(4, 12, street="4 D St")]
        (p,) = self.pools(orders)
        boxes_in_order = [s.packages[0].box.name for s in p.shipments]
        self.assertEqual(boxes_in_order, ["12 Bottle", "12 Bottle", "4 Bottle", "4 Bottle"])


class C7NormalizeTests(SimpleTestCase):
    def test_fields_from_probe_shape(self):
        raw = {
            "id": "abc", "orderNumber": 60719, "complianceStatus": "Quarantined",
            "customerId": "cust-1",
            "club": {"clubTitle": "St. Amant Wine Club (12 Bottle Default)",
                     "clubPackageTitle": "Fall 2026 (12-Bottle)"},
            "shipTo": {"firstName": "Pat", "lastName": "Lee", "address": "1 Main St",
                       "address2": "", "city": "Helena", "stateCode": "mt", "zipCode": "59601",
                       "countryCode": "US"},
            "selectedShippingOptions": {"requestedShipDate": "2026-10-20T00:00:00.000Z"},
            "items": [{"sku": "23MFZ", "productTitle": "2023 Zinfandel", "quantity": 10,
                       "volumeInML": 750, "hasShipping": True},
                      {"sku": "GIFTCARD", "productTitle": "Gift card", "quantity": 1,
                       "hasShipping": False}],
        }
        d = order_from_c7(raw)
        self.assertEqual((d.number, d.name, d.state), ("60719", "Pat Lee", "MT"))
        self.assertTrue(d.is_club)
        self.assertEqual(d.club_package, "Fall 2026 (12-Bottle)")
        self.assertEqual(d.requested_ship_date, date(2026, 10, 20))
        self.assertEqual([(x.sku, x.quantity, x.ml) for x in d.lines], [("23MFZ", 10, 750)])
