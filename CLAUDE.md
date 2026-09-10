# Working in this repository

Home Assistant custom integration for **ORLEN Paczka** parcel tracking.
Distributed via HACS; not part of HA core. One carrier in the
[ha-parcel-integrations](https://github.com/ha-parcel-integrations) suite,
**generated from ha-carrier-template** — everything outside *Carrier-specific
notes* is suite-wide; when in doubt check the template or a sibling repo.
No DTO layer.

## Shared conventions — fetch when relevant

Suite-wide rules live in
[`.github/CONVENTIONS.md`](https://github.com/ha-parcel-integrations/.github/blob/main/CONVENTIONS.md)
and are **not** repeated here. Don't fetch it every session — fetch it **before**
you act in one of these areas:

| Before you … | Fetch `CONVENTIONS.md` § |
|---|---|
| touch entities, sensors, config/options flow, coordinator, diagnostics, translations | *Home Assistant developer docs* (its table points on to the canonical HA page — don't rely on memory) |
| add/rename a parcel field, a `ParcelStatus`, or a bus event; change the sort/first-refresh; touch unmapped-status logging | *Parcel contract* — exact key set, units, sort, events + suppression; `test_parcels.py::test_normalize_publishes_exactly_the_canonical_keys` guards the key set |
| change which optional field this carrier populates vs. always returns `None` | Update `const.py`'s `CAPABILITIES` in the same commit — it feeds the comparison table on the docs site, so a field that starts (or stops) coming back non-null and isn't reflected there is a wrong claim on the website, not just a stale comment. If this carrier has more than one backend (a country-specific transport, not just a config option) with genuinely different field support, `CAPABILITIES` should be a `CAPABILITIES_BY_VARIANT` dict instead — one frozenset per backend, so a field only some backends populate doesn't get silently intersected away or overclaimed for the rest. See ha-dpd's or ha-gls's `const.py` for a live example |
| ship anything while below 1.0.0 (unconfirmed data) | *Pre-1.0 releases* — one-shot WARNINGs for every guessed shape/code |
| consider "fixing" a lint/pattern the skill flags (poll interval, inline client, sync requests) | *Deliberate skill divergences* — likely intentional, don't re-flag |
| commit, bump, tag, release, or write release notes; add a feature without a test | *Workflow / Commits / Versioning / Testing* |

**Structure, options flow, dynamic polling and module layout are suite-wide**
and identical in every carrier — the authoritative spec is
[`ha-carrier-template/scaffold/CLAUDE.md`](https://github.com/ha-parcel-integrations/ha-carrier-template/blob/main/scaffold/CLAUDE.md).
This repo follows it exactly.

**Suite-wide tripwires, kept inline on purpose:**
- **First refresh in `__init__.py`, before `async_forward_entry_setups`** — from
  a forwarded platform HA can't catch `ConfigEntryNotReady` and half-sets-up the
  entry. Runtime-only; tests don't catch a regression.
- **Setup stale-entity sweep is scoped to `domain == "sensor"` and skips
  `non_parcel_unique_ids`** — else it deletes the refresh button / the
  summary+diagnostic sensors. Add a new non-parcel sensor's unique_id to the set.
- **Per-parcel sensors are removed by the summary sensor** via
  `entity_registry.async_remove` (self-removal races and leaves ghosts).

## Carrier-specific notes

**API mechanics live in `carrier-research/orlen-paczka/api/`** — do not copy
endpoints or payload details into this public repository.

- The integration is keyless and code-based. HTTP 200 with an empty history is
  a normal pending parcel, not an error; retain it for later polling.
- `history[]` is the source of truth. Ignore `historyHtml`: it disagreed with
  structured history in a live response and must not enter diagnostics.
- `690` is only `at_pickup_point` when its full Polish label explicitly says
  that the parcel waits for collection. Otherwise it is `unknown`; `1000` is
  the sole observed terminal collection code.
- ORLEN timestamps are naive Poland-local values and are parsed as
  `Europe/Warsaw`. ETA, structured pickup point, sender/receiver, weight,
  dimensions and URL are intentionally `None`; `CAPABILITIES` contains only
  opt-in history.
- **Allegro Delivery (`AD…` codes) is out of scope.** ORLEN's own tracking
  page refers those to Allegro; `config_flow.py` rejects an `AD`-prefixed
  code with a message pointing the user there rather than forwarding it to
  ORLEN's endpoint (which answers `err: 1003` for every `AD…` code tried).
  `err: 1003` is otherwise handled defensively — ORLEN may introduce another
  code family that reaches this endpoint.
- **`return`/`returnTruck` are not yet mapped to `returning`.** No return
  payload has been captured, so both fields are only retained in `raw` and
  fire a one-shot warning the first time they appear non-empty — a
  status-map row needs real return-response evidence before the lifecycle
  changes.
- **Read-only by design.** This integration only tracks; it never submits or
  modifies an ORLEN sender/return workflow, and it never scrapes the public
  tracking HTML or parses `historyHtml` — the JSONP payload is the only data
  source.

## Running tests

```
python -m pytest tests/ --cov=custom_components.orlen_paczka
```

Coverage must stay **above 95%** (silver `test-coverage` rule). Run before
committing. A code change updates the README + this file + `docs/` in the same
commit; the API reference lives in this carrier's own directory in the private
`carrier-research/<slug>/api/`, never in this repo.
