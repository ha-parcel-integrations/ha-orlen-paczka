"""Canonical parcel shape, status mapping and list helpers.

Everything in this module is a **pure function** — no I/O, no Home Assistant
objects beyond the config entry's options. That is deliberate: it keeps the
carrier-specific mapping (which you rewrite per carrier) apart from the
coordinator (which is nearly identical everywhere), and it makes the mapping
trivially unit-testable without spinning up HA.

The status map and normaliser below are the ORLEN-specific pieces. Everything
else — history ordering, sorting, retention and one-shot warnings — is shared
suite machinery.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from homeassistant.config_entries import ConfigEntry

from .const import (
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    DEFAULT_DELIVERED_FILTER_AMOUNT,
    DEFAULT_DELIVERED_FILTER_TYPE,
    HISTORY_MAX_EVENTS,
    ParcelStatus,
)

_LOGGER = logging.getLogger(__name__)

# Where users report a status we do not map yet. Rewritten by the bootstrap
# script; it must point at the carrier's own repo so the log line is
# copy-pasteable straight into a new issue.
#
# The ``?template=`` parameter matters: without it the link opens a blank form,
# and the report comes back missing the version and the log line we need.
NEW_ISSUE_URL = (
    "https://github.com/ha-parcel-integrations/ha-orlen-paczka/issues/new"
    "?template=unrecognised_status.yml"
)

_STATUS_MAP: dict[str, ParcelStatus] = {
    "200": ParcelStatus.REGISTERED,
    "210": ParcelStatus.REGISTERED,
    "240": ParcelStatus.IN_TRANSIT,
    "100": ParcelStatus.IN_TRANSIT,
    "300": ParcelStatus.IN_TRANSIT,
    "653": ParcelStatus.IN_TRANSIT,
    "680": ParcelStatus.OUT_FOR_DELIVERY,
    "1000": ParcelStatus.DELIVERED,
}

CARRIER_TZ = ZoneInfo("Europe/Warsaw")
_PICKUP_LABEL = "czeka na odbiór w punkcie"
_IN_OTHER_COUNTRY_LABEL = "paczka w innym kraju"
_unmapped_statuses_logged: set[str] = set()
_shape_warnings_logged: set[str] = set()


def _warn_shape_once(key: str, message: str, *args: Any) -> None:
    """Log a pre-1.0 payload warning once, without repeating sensitive data."""
    if key in _shape_warnings_logged:
        return
    _shape_warnings_logged.add(key)
    _LOGGER.warning(message, *args)

# Status codes we have already warned about, so each unmapped one is logged
# only once per HA session instead of on every poll.
def _warn_unmapped_status(code: str, label: str | None = None) -> None:
    """Log an unmapped carrier status once, with a copy-paste issue link."""
    if code in _unmapped_statuses_logged:
        return
    _unmapped_statuses_logged.add(code)
    _LOGGER.warning(
        "Unrecognised ORLEN Paczka status — help us map it. Open an issue "
        "and paste this line: %s\n  code=%s label=%r → reported as 'unknown'",
        NEW_ISSUE_URL,
        code,
        label,
    )


def _map_status(code: Any, label: Any = None) -> ParcelStatus | None:
    """Map an ORLEN event code, applying the observed 690 label safeguard."""
    if not isinstance(code, (str, int)):
        return None
    code = str(code)
    if code == "690":
        if isinstance(label, str) and _PICKUP_LABEL in label.lower():
            return ParcelStatus.AT_PICKUP_POINT
        if isinstance(label, str) and _IN_OTHER_COUNTRY_LABEL in label.lower():
            return ParcelStatus.IN_TRANSIT
        return None
    return _STATUS_MAP.get(code)


def map_parcel_status(code: Any, label: Any = None) -> ParcelStatus:
    """Map a carrier status code to a canonical :class:`ParcelStatus`.

    ``None`` (a not-yet-scanned parcel) reports ``unknown`` silently; an
    unrecognised code reports ``unknown`` with a one-shot warning.
    """
    if code is None or code == "":
        return ParcelStatus.UNKNOWN
    mapped = _map_status(code, label)
    if mapped is not None:
        return mapped
    _warn_unmapped_status(str(code), str(label) if label is not None else None)
    return ParcelStatus.UNKNOWN


def map_event_status(code: Any, label: Any = None) -> ParcelStatus | None:
    """Map a history entry's status code to a canonical status, or ``None``.

    Unmapped codes keep ``status: null`` on the history entry (rather than
    ``unknown``, so a consumer can tell "no mapping" from "mapped to unknown")
    and warn once, reusing the parcel-status one-shot set.
    """
    if code is None or code == "":
        return None
    mapped = _map_status(code, label)
    if mapped is not None:
        return mapped
    _warn_unmapped_status(str(code), str(label) if label is not None else None)
    return None


def parse_iso(value: str | None) -> datetime | None:
    """Parse an ISO 8601 string to an aware datetime, or ``None`` on failure.

    Naive values are treated as UTC so a list always sorts without crashing on
    a mixed set.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def to_iso_timestamp(value: Any) -> str | None:
    """Parse ORLEN's ``DD-MM-YYYY, HH:MM`` time in Europe/Warsaw."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value.strip(), "%d-%m-%Y, %H:%M").replace(
            tzinfo=CARRIER_TZ
        ).isoformat()
    except ValueError:
        _warn_shape_once(
            "timestamp",
            "ORLEN Paczka returned an unrecognised event date format. Please "
            "report its shape: %s\n  history[].date type=%s length=%s",
            NEW_ISSUE_URL,
            type(value).__name__,
            len(value),
        )
        return None


def format_dimensions(
    length: float | None, width: float | None, height: float | None
) -> dict[str, Any] | None:
    """Return the canonical ``dimensions`` dict, or ``None`` when incomplete.

    Units contract: **centimetres**, with ``text`` pre-formatted as
    ``"L x W x H cm"`` (integer values, lowercase ``x``) so dashboards can show
    a dimension without doing their own formatting. Convert before calling if
    the carrier reports millimetres or inches.
    """
    if length is None or width is None or height is None:
        return None
    return {
        "length": length,
        "width": width,
        "height": height,
        "text": f"{int(length)} x {int(width)} x {int(height)} cm",
    }


def build_history(
    events: list | None, *, max_events: int = HISTORY_MAX_EVENTS
) -> list[dict]:
    """Build the canonical ``history`` list from the carrier's event list.

    Each entry is ``{timestamp, status, raw_status}`` — identical across all
    suite carriers, and top-level (not under ``raw``) so it survives the
    aggregator's ``strip_raw()``. ``raw_status`` is the carrier's own text, or
    its event code when the API has no human-readable text. Sorted oldest →
    newest and capped to the most recent ``max_events``.

    """
    parseable: list[tuple[datetime, dict]] = []
    unparseable: list[dict] = []
    for event in events or []:
        if not isinstance(event, dict):
            continue
        timestamp = to_iso_timestamp(event.get("date"))
        if not timestamp:
            continue
        entry = {
            "timestamp": timestamp,
            "status": map_event_status(event.get("code"), event.get("label")),
            "raw_status": event.get("label") or event.get("labelShort") or event.get("code"),
        }
        parsed = parse_iso(timestamp)
        if parsed is None:
            unparseable.append(entry)
        else:
            parseable.append((parsed, entry))
    parseable.sort(key=lambda item: item[0])
    ordered = [entry for _, entry in parseable] + unparseable
    return ordered[-max_events:]


def normalize_parcel(raw: dict, *, include_history: bool = False) -> dict:
    """Return a carrier-agnostic parcel dict with the payload under ``raw``.

    The ORLEN payload exposes no ETA, named pickup point, sender/recipient,
    weight or dimensions. ``historyHtml`` is deliberately omitted from ``raw``:
    it is display markup that disagreed with the structured history in a live
    response and must not end up in public diagnostics.
    """
    history = raw.get("history") if isinstance(raw.get("history"), list) else []
    latest = history[-1] if history and isinstance(history[-1], dict) else {}
    tracking_code = raw.get("truckNo") or raw.get("number")
    status_code = latest.get("code")
    raw_status = latest.get("label") or raw.get("label") or status_code
    status = map_parcel_status(status_code, latest.get("label"))
    delivered = status is ParcelStatus.DELIVERED
    delivered_at = to_iso_timestamp(latest.get("date")) if delivered else None
    safe_raw = {key: value for key, value in raw.items() if key != "historyHtml"}

    if raw.get("_orlen_error") == "1003":
        _warn_shape_once(
            "unsupported_1003",
            "ORLEN Paczka returned err=1003 for a configured code. It may be "
            "Allegro Delivery; retain it as unknown and report this: %s",
            NEW_ISSUE_URL,
        )
    if raw.get("return") is True or raw.get("returnTruck") not in (None, "Brak danych"):
        _warn_shape_once(
            "return",
            "ORLEN Paczka return fields appeared for the first time. Please "
            "report field names only: %s\n  return=%s returnTruck_present=%s",
            NEW_ISSUE_URL,
            raw.get("return") is True,
            bool(raw.get("returnTruck") and raw.get("returnTruck") != "Brak danych"),
        )

    unknown_keys = set(raw) - {
        "status", "number", "full", "history", "label", "return", "truckNo",
        "returnTruck", "historyHtml", "err",
    }
    if unknown_keys:
        _warn_shape_once(
            "top_level_keys",
            "ORLEN Paczka returned unexpected top-level keys. Please report "
            "their names only: %s\n  keys=%s",
            NEW_ISSUE_URL,
            sorted(unknown_keys),
        )
    known_event_keys = {"date", "code", "label", "labelShort"}
    unknown_event_keys = sorted(
        {key for event in history if isinstance(event, dict) for key in event} - known_event_keys
    )
    if unknown_event_keys:
        _warn_shape_once(
            "event_keys",
            "ORLEN Paczka returned unexpected history keys. Please report "
            "their names only: %s\n  history[].keys=%s",
            NEW_ISSUE_URL,
            unknown_event_keys,
        )

    return {
        "carrier": "ORLEN Paczka",
        "barcode": tracking_code,
        "sender": None,
        "receiver": None,
        "status": status,
        "raw_status": raw_status,
        "delivered": delivered,
        "delivered_at": delivered_at,
        "planned_from": None,
        "planned_to": None,
        "pickup": status is ParcelStatus.AT_PICKUP_POINT,
        "pickup_point": None,
        # ORLEN offers only a generic form, not a per-parcel public deep link.
        "url": None,
        "weight": None,
        "dimensions": None,
        "history": build_history(history) if include_history else None,
        "raw": safe_raw,
    }


def sort_parcels_by_ts(
    parcels: list[dict], key_field: str, *, descending: bool = False
) -> list[dict]:
    """Return normalised parcels sorted by the ISO timestamp at ``key_field``.

    The suite's sort contract: incoming/outgoing ascending on ``planned_from``,
    delivered descending on ``delivered_at``. Parcels whose value is missing or
    unparseable always sort to the end, regardless of ``descending``.
    """
    with_ts: list[tuple[datetime, dict]] = []
    without_ts: list[dict] = []
    for parcel in parcels:
        parsed = parse_iso(parcel.get(key_field))
        if parsed is None:
            without_ts.append(parcel)
        else:
            with_ts.append((parsed, parcel))
    with_ts.sort(key=lambda item: item[0], reverse=descending)
    return [parcel for _, parcel in with_ts] + without_ts


def apply_delivered_filter(parcels: list[dict], entry: ConfigEntry) -> list[dict]:
    """Trim the delivered list per the entry's retention option.

    ``parcels`` must already be sorted newest-first. ``days`` keeps deliveries
    from the last N days (an unparseable ``delivered_at`` is kept rather than
    silently dropped); the ``parcels`` type keeps the N most recent. Parcels
    stay *tracked* either way — this only controls what the delivered sensor
    shows.
    """
    options = entry.options
    filter_type = options.get(
        CONF_DELIVERED_FILTER_TYPE, DEFAULT_DELIVERED_FILTER_TYPE
    )
    amount = int(
        options.get(CONF_DELIVERED_FILTER_AMOUNT, DEFAULT_DELIVERED_FILTER_AMOUNT)
    )
    if filter_type == "days":
        cutoff = datetime.now(timezone.utc) - timedelta(days=amount)
        return [
            parcel
            for parcel in parcels
            if (parsed := parse_iso(parcel.get("delivered_at"))) is None
            or parsed >= cutoff
        ]
    return parcels[:amount]
