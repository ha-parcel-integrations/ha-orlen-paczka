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

from collections.abc import Iterable
from datetime import UTC, date, datetime, time, timedelta, tzinfo
import logging
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
# Code 690 carries the "ready for collection" category even when the parcel was
# handed to a carrier abroad; only the sentence tells the two apart.
_HANDOFF_LABELS = (
    "paczka w innym kraju",
    "przekazana do przewoźnika w innym kraju",
)
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
        if isinstance(label, str) and any(
            handoff in label.lower() for handoff in _HANDOFF_LABELS
        ):
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
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def to_iso_timestamp(value: Any) -> str | None:
    """Parse ORLEN's ``DD-MM-YYYY, HH:MM`` time in Europe/Warsaw."""
    if not isinstance(value, str):
        return None
    try:
        return (
            datetime.strptime(value.strip(), "%d-%m-%Y, %H:%M")
            .replace(tzinfo=CARRIER_TZ)
            .isoformat()
        )
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
        "text": f"{round(length)} x {round(width)} x {round(height)} cm",
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
            "raw_status": event.get("label")
            or event.get("labelShort")
            or event.get("code"),
        }
        parsed = parse_iso(timestamp)
        if parsed is None:
            unparseable.append(entry)
        else:
            parseable.append((parsed, entry))
    parseable.sort(key=lambda item: item[0])
    ordered = [entry for _, entry in parseable] + unparseable
    return ordered[-max_events:]


def _moment(value: Any, zone: tzinfo) -> tuple[datetime, bool, str] | None:
    """Return ``(aware datetime, date_only, text)`` for a carrier time, or ``None``.

    A naive time or a bare date gets ``zone``; an aware time keeps its offset
    and, when it came in as a string, its own spelling.
    """
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        moment = datetime.combine(value, time.min, tzinfo=zone)
        return moment, True, moment.isoformat()
    elif isinstance(value, str) and value:
        try:
            if len(value) == 10:
                moment = datetime.combine(
                    date.fromisoformat(value), time.min, tzinfo=zone
                )
                return moment, True, moment.isoformat()
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        if parsed.tzinfo:
            return parsed, False, value
    else:
        return None
    moment = parsed if parsed.tzinfo else parsed.replace(tzinfo=zone)
    return moment, False, moment.isoformat()


def _end_of_day(moment: datetime, zone: tzinfo) -> datetime:
    local = moment.astimezone(zone)
    return datetime.combine(local.date(), time(23, 59, 59), tzinfo=zone)


def planned_window(
    start: Any, end: Any, *, zone: tzinfo
) -> tuple[str | None, str | None]:
    """Return ``(planned_from, planned_to)`` in the canonical window shape.

    ``start`` and ``end`` are ISO strings, dates or datetimes, or ``None``; an
    epoch number must be converted first (see ``to_iso_timestamp``). A window
    keeps both ends. One moment with a time ends at ``23:59:59`` the same local
    day; a date covers ``00:00:00`` to ``23:59:59``; two dates run from the
    first day's start to the last day's end. Equal values are one moment,
    reversed values are swapped, and ``planned_to`` is never set without
    ``planned_from``. ``zone`` is the carrier's own zone: it dates naive times
    and bare dates and decides which day a moment ends on, so a ``Z`` moment
    still ends on the carrier's local day. When nothing is left of that day
    after the moment, there is no ``planned_to``.
    """
    first = _moment(start, zone)
    last = _moment(end, zone)
    if first is None:
        first, last = last, None
    if first is None:
        return None, None

    def closing(moment: tuple[datetime, bool, str]) -> datetime:
        return _end_of_day(moment[0], zone) if moment[1] else moment[0]

    if last is not None and closing(last) < first[0]:
        first, last = last, first
    if last is not None and closing(last) == first[0]:
        last = None

    if last is None:
        end_of_day = _end_of_day(first[0], zone)
        return first[2], end_of_day.isoformat() if end_of_day > first[0] else None
    return first[2], closing(last).isoformat() if last[1] else last[2]


_NO_WINDOW_STATUSES = frozenset(
    {ParcelStatus.AT_PICKUP_POINT, ParcelStatus.RETURNING, ParcelStatus.DELIVERED}
)


def planned_for_status(
    status: ParcelStatus, planned_from: str | None, planned_to: str | None
) -> tuple[str | None, str | None]:
    """Empty the delivery window once the parcel is at its destination or done."""
    if status in _NO_WINDOW_STATUSES:
        return None, None
    return planned_from, planned_to


def is_pickup(
    route_flag: bool | None,
    status: ParcelStatus,
    event_statuses: Iterable[ParcelStatus | None] = (),
) -> bool:
    """Whether the delivery route ends at a pickup point.

    The carrier's own route flag wins; otherwise the parcel having reached
    ``AT_PICKUP_POINT`` at any point counts, so the value survives collection.
    """
    if route_flag:
        return True
    return status is ParcelStatus.AT_PICKUP_POINT or any(
        event is ParcelStatus.AT_PICKUP_POINT for event in event_statuses
    )


def normalize_parcel(raw: dict, *, include_history: bool = False) -> dict:
    """Return a carrier-agnostic parcel dict with the payload under ``raw``.

    The ORLEN payload exposes no ETA, named pickup point, sender/recipient,
    weight or dimensions. ``historyHtml`` is deliberately omitted from ``raw``:
    it is display markup that disagreed with the structured history in a live
    response and must not end up in public diagnostics.
    """
    history = raw.get("history")
    if not isinstance(history, list):
        history = []
    latest = history[-1] if history and isinstance(history[-1], dict) else {}
    tracking_code = raw.get("truckNo") or raw.get("number")
    status_code = latest.get("code")
    raw_status = latest.get("label") or raw.get("label") or status_code
    status = map_parcel_status(status_code, latest.get("label"))
    delivered = status is ParcelStatus.DELIVERED
    delivered_at = to_iso_timestamp(latest.get("date")) if delivered else None
    planned_from, planned_to = planned_for_status(
        status, *planned_window(None, None, zone=CARRIER_TZ)
    )
    pickup = is_pickup(
        None,
        status,
        (
            map_event_status(event.get("code"), event.get("label"))
            for event in history
            if isinstance(event, dict)
        ),
    )
    safe_raw = {key: value for key, value in raw.items() if key != "historyHtml"}

    error = raw.get("_orlen_error")
    if error == "1003":
        _warn_shape_once(
            "unsupported_1003",
            "ORLEN Paczka returned err=1003 for a configured code. It may be "
            "Allegro Delivery; retain it as unknown and report this: %s",
            NEW_ISSUE_URL,
        )
    elif error == "1004":
        _warn_shape_once(
            "untrackable_1004",
            "ORLEN Paczka returned err=1004 for a configured code, meaning it "
            "cannot be tracked; keeping it as unknown. Report this if the "
            "parcel should be trackable: %s",
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
        "status",
        "number",
        "full",
        "history",
        "label",
        "return",
        "truckNo",
        "returnTruck",
        "historyHtml",
        "err",
        "_orlen_error",
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
        {key for event in history if isinstance(event, dict) for key in event}
        - known_event_keys
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
        "planned_from": planned_from,
        "planned_to": planned_to,
        "pickup": pickup,
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
    filter_type = options.get(CONF_DELIVERED_FILTER_TYPE, DEFAULT_DELIVERED_FILTER_TYPE)
    amount = int(
        options.get(CONF_DELIVERED_FILTER_AMOUNT, DEFAULT_DELIVERED_FILTER_AMOUNT)
    )
    if filter_type == "days":
        cutoff = datetime.now(UTC) - timedelta(days=amount)
        return [
            parcel
            for parcel in parcels
            if (parsed := parse_iso(parcel.get("delivered_at"))) is None
            or parsed >= cutoff
        ]
    return parcels[:amount]
