"""Sanitized ORLEN Paczka responses captured from the public tracker."""
from __future__ import annotations

ACTIVE_CODE = "2100000000001"
DELIVERED_CODE = "2100000000002"


def event(code: str, date: str, label: str, short: str = "") -> dict:
    """Return one structured ORLEN history event."""
    return {"date": date, "code": code, "label": label, "labelShort": short or label}


def delivered_sample(code: str = DELIVERED_CODE) -> dict:
    """A completed pickup parcel, with the observed ascending event order."""
    history = [
        event("200", "03-07-2026, 04:02", "Parcel prepared by sender"),
        event("210", "03-07-2026, 16:45", "Parcel handed in at point"),
        event("100", "03-07-2026, 23:53", "Parcel at regional sorting centre"),
        event("300", "04-07-2026, 05:21", "Parcel at central sorting centre"),
        event("680", "04-07-2026, 12:23", "Parcel travelling to pickup point"),
        event("690", "04-07-2026, 14:23", "Twoja paczka czeka na odbiór w punkcie"),
        event("1000", "04-07-2026, 16:13", "Collected by customer"),
    ]
    return {
        "status": "OK", "number": code, "full": True, "history": history,
        "label": history[-1]["label"], "return": False, "truckNo": code,
        "returnTruck": "Brak danych", "historyHtml": "<table>ignored</table>",
    }


def active_sample(code: str = ACTIVE_CODE) -> dict:
    """An ordinary parcel in transit, with no ETA or structured pickup point."""
    sample = delivered_sample(code)
    sample["history"] = sample["history"][:5]
    sample["label"] = sample["history"][-1]["label"]
    return sample


def pickup_sample(code: str = ACTIVE_CODE) -> dict:
    """A parcel whose full label confirms the otherwise ambiguous code 690."""
    sample = active_sample(code)
    sample["history"].append(
        event("690", "04-07-2026, 14:23", "Twoja paczka czeka na odbiór w punkcie")
    )
    sample["label"] = sample["history"][-1]["label"]
    return sample


def ambiguous_690_sample(code: str = ACTIVE_CODE) -> dict:
    """A real observed 690 conflict: full and short labels disagree."""
    sample = active_sample(code)
    sample["history"].append(
        event("690", "15-06-2026, 02:00", "Paczka w innym kraju", "Ready for collection")
    )
    sample["label"] = sample["history"][-1]["label"]
    return sample
