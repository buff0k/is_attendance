# Clocking Controllers

Two standalone Python scripts that run on the Windows PC at the site, outside the Frappe bench
entirely. They live here (inside the `is_attendance` app source) so they're version-controlled
alongside the ERP logic they feed, but neither one imports Frappe or runs on the bench - both are
plain scripts you deploy to the Windows host and run there.

| Script | Runs where | Job |
|---|---|---|
| `gateway.py` | Windows host, next to the HIKVision terminals | Talks to the terminals (live webhook + history polling), writes daily `recordList_YYYY-MM-DD.csv` files. Knows nothing about Frappe. |
| `erp_uploader.py` | Same Windows host | Reads those CSVs (read-only, never touches them) and uploads them into one or more Frappe instances as `Clocking Import` documents. Knows nothing about the terminals. |

They're deliberately decoupled: `gateway.py`'s job is "capture reliably," `erp_uploader.py`'s job
is "get it into the ERP," and neither can break the other.

## Credentials are never committed

Every config file here that would need to hold a real password or API key is git-ignored:

- `gateway.json` (terminal admin credentials) - gitignored. `gateway.example.json` is the tracked
  template.
- `erp_uploader.json` (per-Frappe-instance API keys) - gitignored. `erp_uploader.example.json` is
  the tracked template.
- Runtime logs (`gateway.log`, `erp_uploader.log`, `logs/`) and state (`state.json`,
  `erp_uploader_state.json`) are also gitignored - host-local, never meaningful to commit.

**Setup on the Windows host**: copy each `.example.json` to its real filename at the path the
script expects (below), then fill in real values. Never rename an `.example.json` in place and
edit it directly inside a git checkout - copy it out, or the real one will eventually get
committed the first time someone runs a blanket `git add`.

## Deployment layout

Both scripts use fixed Windows paths for their runtime data, independent of wherever you happen
to check this repo out to - so the source can live in git, while the actual credentials/logs/state
live only on the host:

```
C:\HikGateway\
  gateway.py                (copied from this folder)
  gateway.json               <- real credentials, from gateway.example.json
  gateway.log                 (created automatically)
  state.json                  (created automatically - IN/OUT shift state)
  poll_state.json             (created automatically - per-device history poll watermark)
  records\
    recordList_2026-09-04.csv (created automatically, one per day)
    ...

  ErpUploader\
    erp_uploader.py          (copied from this folder)
    erp_uploader.json         <- real credentials, from erp_uploader.example.json
    erp_uploader.log           (created automatically)
    erp_uploader_state.json    (created automatically - per-instance upload state)
    logs\
      eben.log                 (created automatically)
      jorrie.log
      isambane.log
```

`erp_uploader.py` only ever *reads* `C:\HikGateway\records\` - it never renames, moves, or edits a
CSV `gateway.py` writes, so both can run at the same time with no coordination needed.

## `gateway.py`

Runs a small Flask server (port 8080) for live HIKVision webhook events, plus a background thread
that polls each terminal's stored event history every 5 minutes as a safety net for anything the
live webhook missed. Every clocking - live or polled - goes through the same dedup + IN/OUT logic
and gets appended to that day's CSV.

**History polling window**: the very first successful poll of a device covers the full
`HISTORY_LOOKBACK_DAYS` (30 days) as a one-time backfill. Every poll after that only asks for
events since that device's own last successful poll (kept in `poll_state.json`, with a
15-minute overlap as a safety margin) - not the full 30 days again. This matters: on a device
with a large event history, repeatedly re-requesting the full window every 5 minutes forever is
what was causing the mid-pagination `HTTP 401`s seen in testing (the terminal's own digest auth
session going stale under that much sustained pagination, not a wrong password - it always
happened after many earlier pages on the same poll had already succeeded). If that still happens
on an unusually large one-time backfill, `fetch_device_history()` now re-authenticates with a
fresh session and resumes at the exact same position rather than restarting the search, so one
stale session doesn't throw away everything already fetched.

**Config** - `gateway.json`, shape documented in `gateway.example.json` and in
`load_gateway_config()`'s docstring in the script itself:

```json
{
  "default_username": "admin",
  "default_password": "...",
  "devices": [
    { "ip": "192.168.0.101", "label": "Main-Entrance", "enabled": true },
    { "ip": "192.168.0.102", "label": "Side-Door", "enabled": true,
      "username": "...", "password": "..." }
  ]
}
```

- `default_username`/`default_password` apply to every device that doesn't set its own.
- Any device entry may set its own `username`/`password` to override the defaults - useful when
  one terminal was set up with a different admin password than the rest. Omit both on a device to
  use the shared defaults.
- `enabled: false` stops polling a terminal without deleting its config.
- Missing or malformed `gateway.json` fails loudly on startup (`RuntimeError`) rather than silently
  running with no devices.

**Run it**: `python gateway.py` (needs `flask` and `requests` installed). It logs to both the
console and `gateway.log`. Intended to run continuously (e.g. as a Scheduled Task or a Windows
service wrapper) - it has no exit condition of its own besides Ctrl+C.

## `erp_uploader.py`

Polls every 5 minutes (loop, no server). Each cycle, for every **enabled** instance in
`erp_uploader.json` that has both an `api_key` and `api_secret` set:

1. Looks at every `recordList_*.csv` in `C:\HikGateway\records\`. If that file's size hasn't
   changed since this *instance's* last upload of it, skip it - already sent. Otherwise upload it
   as a new `Clocking Import` document via the REST API, and if the resulting document's
   status already reads `Pending Import`, immediately call its `queue_import()` method to run the
   import.
2. Separately queries that instance for any `Clocking Import` still sitting at
   `docstatus=0, status=Pending Import` - this catches documents that started out as `Missing
   Information` (unrecognised employee code, etc.) on an earlier cycle and have since been fixed by
   a person in Desk. The ERP's own `validate()` re-runs on that save and flips status to `Pending
   Import` by itself; this step is what notices and finishes the import, with nobody needing to
   click "Start Import" by hand.

**Does it wait for a person before importing?** Yes. A freshly uploaded CSV only auto-imports if
every employee code in it already resolves cleanly. If anything's unresolved, the document sits at
`Missing Information` untouched until a person maps it in Desk - at which point step 2 above picks
it up on the next cycle automatically.

**Config** - `erp_uploader.json`, shape documented in `erp_uploader.example.json`:

```json
{
  "instances": [
    { "name": "eben", "base_url": "https://eben.isambane.co.za",
      "api_key": "...", "api_secret": "...", "enabled": true },
    { "name": "jorrie", "base_url": "https://jorrie.isambane.co.za",
      "api_key": "", "api_secret": "", "enabled": false }
  ]
}
```

Each instance is independent - its own credentials, its own enabled flag, its own upload-state
entry in `erp_uploader_state.json`, its own log file under `logs\`.

**Adding a new instance later (e.g. going live on `isambane.co.za`)**: add its entry (or flip
`enabled: true` and fill in real credentials on the placeholder already in the template), and
that's the whole step. Because state is tracked per instance name, a name with no prior state
entry has nothing to compare file sizes against, so *every* existing CSV in `records\` looks new to
it and gets uploaded on its very first enabled cycle - full backfill, automatically, no separate
migration script needed.

**Run it**: `python erp_uploader.py` (needs `requests` installed). Also intended to run
continuously, independently of `gateway.py` - a Scheduled Task per script, or both under the same
process supervisor, either works since they never touch each other's files.

## Frappe-side prerequisite

Each target instance needs an API user with a role that can create `Clocking Import`
documents and call its `queue_import()` method (an HR User is sufficient - see that doctype's
permissions) with an API key/secret generated for it. That key/secret is what goes into
`erp_uploader.json` for that instance.
