# Copyright (c) 2026, BuFf0k and contributors
# For license information, please see license.txt

"""
One record per deployed sage_employee_puller.py instance - see
is_attendance.controllers.sage_payroll.get_remote_controller_config() (what
a controller polls to learn which Sage Companies/DSNs it should serve,
replacing the old local `companies` JSON config) and
list_pending_pull_requests()/ingest_employees() (which stamp
last_heartbeat/last_sync_at here so the Connected/last-sync picture on this
doctype's own list view stays live).
"""

from __future__ import annotations

from frappe.model.document import Document


class SageRemoteController(Document):
	pass
