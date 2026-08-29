"""Tests for ORLEN's payload normalisation and status safeguards."""
from datetime import datetime, timedelta, timezone

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.orlen_paczka.const import (
    CAPABILITIES,
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    DOMAIN,
    KNOWN_CAPABILITIES,
    ParcelStatus,
)
from custom_components.orlen_paczka.parcels import (
    apply_delivered_filter,
    build_history,
    map_event_status,
    map_parcel_status,
    normalize_parcel,
    parse_iso,
    sort_parcels_by_ts,
    to_iso_timestamp,
)

from .payloads import (
    ACTIVE_CODE,
    DELIVERED_CODE,
    active_sample,
    ambiguous_690_sample,
    delivered_sample,
    event,
    pickup_sample,
)

CANONICAL_KEYS = [
    "carrier", "barcode", "sender", "receiver", "status", "raw_status",
    "delivered", "delivered_at", "planned_from", "planned_to", "pickup",
    "pickup_point", "url", "weight", "dimensions", "history", "raw",
]


def test_known_statuses_map_to_the_canonical_contract():
    assert map_parcel_status("200") is ParcelStatus.REGISTERED
    assert map_parcel_status("210") is ParcelStatus.REGISTERED
    for code in ("240", "100", "300", "653"):
        assert map_parcel_status(code) is ParcelStatus.IN_TRANSIT
    assert map_parcel_status("680") is ParcelStatus.OUT_FOR_DELIVERY
    assert map_parcel_status("1000") is ParcelStatus.DELIVERED


def test_690_uses_the_full_label_and_never_means_delivered(caplog):
    assert map_parcel_status("690", "Paczka czeka na odbiór w punkcie") is ParcelStatus.AT_PICKUP_POINT
    assert map_parcel_status("690", "Paczka w innym kraju") is ParcelStatus.IN_TRANSIT
    assert map_parcel_status("690", "Unexpected 690 label") is ParcelStatus.UNKNOWN
    assert "issues/new" in caplog.text


def test_unknown_status_warns_once_and_event_mapping_keeps_none(caplog):
    assert map_parcel_status("999", "Unexpected") is ParcelStatus.UNKNOWN
    assert map_parcel_status("999", "Unexpected") is ParcelStatus.UNKNOWN
    assert caplog.text.count("code=999") == 1
    assert map_event_status("999", "Unexpected") is None


def test_timestamp_is_warsaw_local_and_malformed_value_warns(caplog):
    timestamp = to_iso_timestamp("04-07-2026, 16:13")
    assert timestamp == "2026-07-04T16:13:00+02:00"
    assert to_iso_timestamp("not a date") is None
    assert "history[].date" in caplog.text
    assert parse_iso(timestamp).tzinfo is not None


def test_build_history_is_ascending_and_capped():
    history = build_history(delivered_sample()["history"])
    assert history[0]["status"] is ParcelStatus.REGISTERED
    assert history[-1]["status"] is ParcelStatus.DELIVERED
    many = [event("100", f"01-07-2026, {hour:02d}:00", "Moving") for hour in range(24)]
    assert len(build_history(many, max_events=20)) == 20


def test_build_history_skips_bad_entries():
    assert build_history(None) == []
    assert build_history([{"code": "100"}, "not a dict"]) == []


def test_normalize_publishes_the_exact_canonical_shape():
    assert list(normalize_parcel(delivered_sample())) == CANONICAL_KEYS


def test_normalize_maps_completed_pickup_and_omits_html_from_raw():
    parcel = normalize_parcel(delivered_sample(), include_history=True)
    assert parcel["carrier"] == "ORLEN Paczka"
    assert parcel["barcode"] == DELIVERED_CODE
    assert parcel["status"] is ParcelStatus.DELIVERED
    assert parcel["delivered"] is True
    assert parcel["delivered_at"] == "2026-07-04T16:13:00+02:00"
    assert parcel["pickup"] is False
    assert parcel["url"] is None
    assert parcel["history"][-1]["status"] is ParcelStatus.DELIVERED
    assert "historyHtml" not in parcel["raw"]
    assert all(parcel[key] is None for key in ("sender", "receiver", "planned_from", "planned_to", "pickup_point", "weight", "dimensions"))


def test_normalize_active_pickup_and_ambiguous_690():
    active = normalize_parcel(active_sample())
    assert active["barcode"] == ACTIVE_CODE
    assert active["status"] is ParcelStatus.OUT_FOR_DELIVERY
    pickup = normalize_parcel(pickup_sample())
    assert pickup["status"] is ParcelStatus.AT_PICKUP_POINT
    assert pickup["pickup"] is True
    ambiguous = normalize_parcel(ambiguous_690_sample())
    assert ambiguous["status"] is ParcelStatus.IN_TRANSIT


def test_normalize_pending_placeholder_and_number_fallback():
    parcel = normalize_parcel({"number": ACTIVE_CODE})
    assert parcel["barcode"] == ACTIVE_CODE
    assert parcel["status"] is ParcelStatus.UNKNOWN
    assert parcel["raw_status"] is None


def test_capabilities_match_confirmed_payload():
    assert CAPABILITIES == {"history"}
    assert CAPABILITIES <= KNOWN_CAPABILITIES
    assert normalize_parcel(delivered_sample(), include_history=True)["history"] is not None


def test_sorting_and_delivered_retention():
    parcels = [
        {"barcode": "a", "planned_from": "2026-07-02T10:00:00+02:00"},
        {"barcode": "b", "planned_from": None},
        {"barcode": "c", "planned_from": "2026-07-01T10:00:00+02:00"},
    ]
    assert [p["barcode"] for p in sort_parcels_by_ts(parcels, "planned_from")] == ["c", "a", "b"]
    entry = MockConfigEntry(
        domain=DOMAIN,
        options={CONF_DELIVERED_FILTER_TYPE: "parcels", CONF_DELIVERED_FILTER_AMOUNT: 1},
    )
    assert len(apply_delivered_filter([{"barcode": "a"}, {"barcode": "b"}], entry)) == 1
    old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    days_entry = MockConfigEntry(
        domain=DOMAIN,
        options={CONF_DELIVERED_FILTER_TYPE: "days", CONF_DELIVERED_FILTER_AMOUNT: 1},
    )
    assert apply_delivered_filter([{"barcode": "old", "delivered_at": old}], days_entry) == []
