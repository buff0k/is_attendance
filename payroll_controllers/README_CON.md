# Payroll Controllers

One standalone Python script that runs on the Windows PC where Sage VIP
Premier's ODBC DSNs are configured - outside the Frappe bench entirely,
same pattern as `../clocking_controllers/` (`gateway.py`/`erp_uploader.py`
- see that folder's own README_CON.md for the fuller narrative on *why*
this split exists). It lives here (inside the `is_attendance` app source)
so it's version-controlled alongside the ERP logic it feeds, but doesn't
import Frappe or run on the bench.

| Script | Runs where | Job |
|---|---|---|
| `sage_employee_puller.py` | Windows host, next to the Sage VIP Premier ODBC DSNs | Queries Sage directly for one Company Number's active employee list, posts it into a Frappe `Sage Payroll Run`. |

## Credentials are never committed

`sage_employee_puller.json` (Frappe API key/secret) is gitignored.
`sage_employee_puller.example.json` is the tracked template. The runtime
log (`sage_employee_puller.log`) is also gitignored - host-local, never
meaningful to commit.

**Setup on the Windows host**: copy `sage_employee_puller.example.json` to
`C:\HikGateway\PayrollController\sage_employee_puller.json`, then fill in
real values. Never rename the `.example.json` in place and edit it
directly inside a git checkout - copy it out, or the real one will
eventually get committed the first time someone runs a blanket `git add`.

## `sage_employee_puller.py`

A heartbeat, same shape as `gateway.py`/`erp_uploader.py` in
`../clocking_controllers/`: runs continuously, polling Frappe rather than
being invoked with a specific Run to fetch. The trigger always starts on
the Frappe side - a person clicks **"Request Employee Pull"** on a
`Sage Payroll Run` (sets its Status to "Pull Requested"); this heartbeat's
next poll of `is_attendance.controllers.sage_payroll.list_pending_pull_requests`
is what notices and actually does the ODBC work. Nothing here ever queries
Sage unprompted.

Each cycle (`heartbeat_interval_seconds` in the config, default 60s):

1. GETs `list_pending_pull_requests` - every Sage Payroll Run currently at
   Status "Pull Requested", with the Sage Company Number and Paypoints
   each one needs (read from that Run's own `Sage Payroll Company` record
   on the Frappe side - not from this script's local config, so a
   Paypoint added in Frappe takes effect without this file needing an
   edit + restart too).
2. For each request whose Company Number matches a **configured and
   enabled** entry in `sage_employee_puller.json`, connects to that
   Company's own ODBC DSN (`DSN=VIP_Company001;UID=;PWD=;` - blank
   UID/PWD, same connection string the real Excel query tooling already
   uses, confirmed by byte-searching a real Salary Sheet `.xls`'s embedded
   query definition), runs the confirmed identity query once per
   Paypoint, and POSTs the combined employee list to that Run via
   `/api/method/run_doc_method` (`method: "ingest_employees"`) - the same
   mechanism `erp_uploader.py` already uses for `queue_import()`.
3. A request for a Company Number this instance doesn't have
   configured/enabled is skipped with a log line, not an error - lets
   several bench instances (or a future second Windows host) share the
   same polling endpoint without stepping on each other.

Config is re-read every cycle, so enabling a new company or editing its
Paypoint list doesn't need the heartbeat restarted.

**The query, exactly as found** (see the script's own module docstring
for the full byte-search evidence):

```sql
SELECT EMP_INFO_FIXED.Surname AS SURNAME, EMP_INFO_FIXED.EmployeeCode AS COY,
       EMP_INFO_FIXED.FullNames AS NAME, EMP_INFO_FIXED.IDNumber AS ID,
       DESC_JOBTITLE.JobTitleLongDesc AS OCCUPATION
FROM dba.DESC_JOBTITLE DESC_JOBTITLE, dba.EMP_INFO_FIXED EMP_INFO_FIXED
WHERE EMP_INFO_FIXED.JobTitleCode = DESC_JOBTITLE.JobTitleCode
  AND ((EMP_INFO_FIXED.PaypointCode = ?) AND (EMP_INFO_FIXED.EmployeeStatus = 'N'))
ORDER BY EMP_INFO_FIXED.Surname, DESC_JOBTITLE.JobTitleLongDesc
```

Only identity columns (Surname/EmployeeCode/FullNames/IDNumber/
JobTitleLongDesc) are pulled this way - no evidence of a live query for
hours baselines, allowances, or leave balances was found in either real
sample file, so this script doesn't attempt to pull those. Normal /
Overtime 1.5 / Leave hours are computed entirely on the Frappe side from
our own Employee Checkin and Leave Application data instead (see
`is_attendance.controllers.sage_payroll.compute_hours_and_leave`).

**Idempotent**: re-running this script for the same Sage Payroll Run
simply refreshes the same `Sage Payroll Employee` rows (matched on
Sage Employee Code) rather than creating duplicates - safe to re-run to
pick up a Sage-side correction.

**Config** - `sage_employee_puller.json`, shape documented in
`sage_employee_puller.example.json`:

```json
{
  "base_url": "https://eben.isambane.co.za",
  "api_key": "...",
  "api_secret": "...",
  "heartbeat_interval_seconds": 60,
  "companies": [
    { "sage_company_no": "001", "dsn": "VIP_Company001",
      "paypoints": ["BKN", "UADT"], "enabled": true }
  ]
}
```

`companies` needs one entry per Sage Company Number this bench instance
handles - `paypoints` here is only a fallback (the live Paypoint list
comes from Frappe's own `Sage Payroll Company` record on each poll); keep
it roughly in sync anyway so a request still has something to fall back to
if that field is ever missing from a response. Adding a new site later is
just: add its Paypoint on the `Sage Payroll Company` doctype in Frappe.

**Run it**: `python sage_employee_puller.py` (needs `pyodbc` and
`requests` installed, plus the same Sybase SQL Anywhere / iAnywhere ODBC
driver the existing VBA tooling already depends on - if the Excel query
tool works on this PC, the DSN already works). Intended to run
continuously, independently of `gateway.py`/`erp_uploader.py` - a
Scheduled Task, or under the same process supervisor as those two.

## Frappe-side prerequisite

The API user needs a role that can create/write `Sage Payroll Employee`
and write `Sage Payroll Run` documents (System Manager, Payroll Manager,
or Payroll User - see those doctypes' permissions) with an API key/secret
generated for it. That key/secret is what goes into
`sage_employee_puller.json`.

## Deliberately out of scope for this pass

The reverse direction - generating the Sage batch-import `.txt` from
Frappe and getting it into Sage - stays manual: download the `.txt` from
a `Sage Payroll Run`'s "Export .txt" button in Desk, and import it into
Sage via its own Batch Import function by hand. An automated Windows-side
executor for that direction is a later addition, not this one.
