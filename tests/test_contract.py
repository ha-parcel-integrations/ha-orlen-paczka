"""The parcel contract's checks, run over every normalised sample."""

from datetime import UTC, date, datetime
from unittest.mock import MagicMock

import pytest

from custom_components.orlen_paczka.calendar import ORLENPaczkaDeliveriesCalendar
from custom_components.orlen_paczka.const import ParcelStatus
from custom_components.orlen_paczka.parcels import (
    CARRIER_TZ,
    is_pickup,
    normalize_parcel,
    planned_for_status,
    planned_window,
)
from custom_components.orlen_paczka.sensor import (
    ORLENPaczkaAwaitingPickupSensor,
    ORLENPaczkaEnRouteToPickupPointSensor,
    ORLENPaczkaNextDeliverySensor,
)

from .payloads import active_sample, delivered_sample, event, pickup_sample

CANONICAL_KEYS = [
    "carrier",
    "barcode",
    "sender",
    "receiver",
    "contents",
    "status",
    "raw_status",
    "delivered",
    "delivered_at",
    "planned_from",
    "planned_to",
    "pickup",
    "pickup_point",
    "url",
    "weight",
    "dimensions",
    "history",
    "raw",
]

SAMPLES = {
    "collected": delivered_sample,
    "active": active_sample,
    "at_pickup_point": pickup_sample,
    "bare": lambda: {"number": "2100000000009"},
}


@pytest.fixture(params=SAMPLES)
def parcel(request) -> dict:
    return normalize_parcel(SAMPLES[request.param]())


def test_keys_and_order(parcel):
    assert list(parcel) == CANONICAL_KEYS


def test_delivered_follows_status(parcel):
    assert parcel["delivered"] is (parcel["status"] is ParcelStatus.DELIVERED)


def test_delivered_at_only_when_delivered(parcel):
    if not parcel["delivered"]:
        assert parcel["delivered_at"] is None


def test_planned_is_empty_when_the_parcel_is_done_or_waiting(parcel):
    if parcel["status"] in (
        ParcelStatus.DELIVERED,
        ParcelStatus.AT_PICKUP_POINT,
        ParcelStatus.RETURNING,
    ):
        assert parcel["planned_from"] is None
        assert parcel["planned_to"] is None
    if parcel["planned_to"] is not None:
        assert parcel["planned_from"] is not None


def test_pickup_is_a_bool_and_the_point_needs_it(parcel):
    assert isinstance(parcel["pickup"], bool)
    if not parcel["pickup"]:
        assert parcel["pickup_point"] is None


def test_a_collected_pickup_parcel_stays_pickup():
    parcel = normalize_parcel(delivered_sample())
    assert parcel["status"] is ParcelStatus.DELIVERED
    assert parcel["delivered"] is True
    assert parcel["pickup"] is True
    assert parcel["pickup_point"] is None


def test_a_parcel_never_at_a_point_is_not_pickup():
    parcel = normalize_parcel(active_sample())
    assert parcel["pickup"] is False


def test_pickup_follows_the_route_flag_then_the_events():
    assert is_pickup(True, ParcelStatus.IN_TRANSIT) is True
    assert is_pickup(None, ParcelStatus.DELIVERED, [ParcelStatus.AT_PICKUP_POINT])
    assert is_pickup(None, ParcelStatus.DELIVERED, [None]) is False


def test_planned_window_shapes():
    start, end = planned_window("2099-04-29T13:00:00+02:00", None, zone=CARRIER_TZ)
    assert start == "2099-04-29T13:00:00+02:00"
    assert datetime.fromisoformat(end).time().isoformat() == "23:59:59"
    start, end = planned_window("2099-04-29", None, zone=CARRIER_TZ)
    assert datetime.fromisoformat(start).time().isoformat() == "00:00:00"
    assert datetime.fromisoformat(end).time().isoformat() == "23:59:59"
    assert planned_for_status(ParcelStatus.DELIVERED, start, end) == (None, None)
    assert planned_for_status(ParcelStatus.IN_TRANSIT, start, end) == (start, end)


def _coordinator(data):
    coordinator = MagicMock()
    coordinator.data = data
    return coordinator


def _entry():
    entry = MagicMock()
    entry.entry_id = "e1"
    return entry


def _p(barcode, status, **extra):
    return {
        "barcode": barcode,
        "status": status,
        "pickup": False,
        "planned_from": None,
        "planned_to": None,
        **extra,
    }


def test_pickup_sensors_share_no_parcel_and_skip_returning():
    data = [
        _p("A", ParcelStatus.OUT_FOR_DELIVERY, pickup=True),
        _p("B", ParcelStatus.AT_PICKUP_POINT, pickup=True),
        _p("C", ParcelStatus.RETURNING, pickup=True),
        _p("D", ParcelStatus.IN_TRANSIT),
    ]
    en_route = ORLENPaczkaEnRouteToPickupPointSensor(_coordinator(data), _entry())
    awaiting = ORLENPaczkaAwaitingPickupSensor(_coordinator(data), _entry())
    assert [p["barcode"] for p in en_route.extra_state_attributes["parcels"]] == ["A"]
    assert en_route.native_value == 1
    assert [p["barcode"] for p in awaiting.extra_state_attributes["parcels"]] == ["B"]


def test_next_delivery_skips_an_ended_window():
    data = [
        _p("OLD", ParcelStatus.IN_TRANSIT, planned_from="2000-01-01T10:00:00Z"),
        _p(
            "OLDWIN",
            ParcelStatus.IN_TRANSIT,
            planned_from="2000-01-01T10:00:00Z",
            planned_to="2000-01-01T23:59:59Z",
        ),
        _p("NEW", ParcelStatus.IN_TRANSIT, planned_from="2099-01-01T10:00:00Z"),
    ]
    sensor = ORLENPaczkaNextDeliverySensor(_coordinator(data), _entry())
    assert sensor.extra_state_attributes["barcode"] == "NEW"


def test_a_whole_day_window_is_an_all_day_event():
    data = [
        _p(
            "A",
            ParcelStatus.IN_TRANSIT,
            planned_from="2099-04-29T00:00:00+02:00",
            planned_to="2099-04-29T23:59:59+02:00",
        ),
        _p(
            "B",
            ParcelStatus.IN_TRANSIT,
            planned_from="2099-04-29T13:00:00+02:00",
            planned_to="2099-04-29T23:59:59+02:00",
        ),
    ]
    events = ORLENPaczkaDeliveriesCalendar(_coordinator(data), _entry())._events()
    assert (events[0].start, events[0].end) == (date(2099, 4, 29), date(2099, 4, 30))
    assert isinstance(events[1].start, datetime)
    assert events[1].start == datetime(2099, 4, 29, 11, 0, tzinfo=UTC)


def test_collecting_a_waiting_parcel_keeps_pickup():
    sample = pickup_sample()
    sample["history"].append(event("1000", "05-07-2026, 10:00", "Collected"))
    parcel = normalize_parcel(sample)
    assert parcel["delivered"] is True
    assert parcel["pickup"] is True


def test_planned_window_edge_inputs():
    zone = CARRIER_TZ
    assert planned_window(None, None, zone=zone) == (None, None)
    assert planned_window("garbage", None, zone=zone) == (None, None)
    assert planned_window(12, None, zone=zone) == (None, None)
    assert planned_window(None, "2099-04-29", zone=zone)[0].startswith("2099-04-29")
    first = planned_window(date(2099, 4, 29), date(2099, 5, 1), zone=zone)
    assert first[0].startswith("2099-04-29T00:00:00")
    assert first[1].startswith("2099-05-01T23:59:59")
    reversed_ = planned_window("2099-05-01T10:00:00", "2099-04-29T10:00:00", zone=zone)
    assert reversed_[0].startswith("2099-04-29T10:00:00")
    assert planned_window(datetime(2099, 4, 29, 10, 0), None, zone=zone)[0].startswith(
        "2099-04-29T10:00:00"
    )
    one = planned_window("2099-04-29T10:00:00Z", "2099-04-29T10:00:00Z", zone=zone)
    assert one[0] == "2099-04-29T10:00:00Z"
    assert planned_window("2099-04-29T23:59:59", None, zone=zone)[1] is None
