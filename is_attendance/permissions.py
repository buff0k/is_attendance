# Copyright (c) 2026, BuFf0k and contributors
# For license information, please see license.txt

"""
Branch-scoped visibility for clocking data.

An HR User responsible for a specific Branch (e.g. the HR clerk at
Bankfontein) should only see clocking records for employees whose home
Branch (Employee.branch) is one they're responsible for. Responsibility is
configured on the ``IS Attendance Settings`` singleton's ``hr_per_branch``
child table (``IS Attendance User Branch``: user + branch rows).

This mirrors ir/permissions.py's Branch Limits design exactly (same
"no rows -> unrestricted" fallback, same SQL-subquery shape) rather than
inventing a new approach - see hr_per_branch there for the original.
"""

from __future__ import annotations

import frappe

SETTINGS_DOCTYPE = "IS Attendance Settings"

# Doctype -> fieldname holding the Employee link whose Employee.branch is
# checked against a user's hr_per_branch rows.
BRANCH_LIMITED_DOCTYPES = {
    "Employee Checkin": "employee",
    "Clocking Adjustment": "employee",
}


def responsible_branches_for_user(user: str | None = None) -> list[str]:
    """Branches `user` is responsible for, from IS Attendance Settings.hr_per_branch.
    An empty list means the branch limit doesn't apply to this user at all -
    callers must treat that as "no restriction", not "restricted from everything"."""
    user = user or frappe.session.user
    rows = frappe.get_all(
        "IS Attendance User Branch",
        filters={
            "parent": SETTINGS_DOCTYPE,
            "parenttype": SETTINGS_DOCTYPE,
            "parentfield": "hr_per_branch",
            "user": user,
        },
        fields=["branch"],
    )
    return [row.branch for row in rows if row.get("branch")]


def _employee_branch(employee: str | None) -> str | None:
    if not employee:
        return None
    return frappe.db.get_value("Employee", employee, "branch")


def _branch_is_restricted(employee: str | None, user: str | None = None) -> bool:
    """True only if this user has hr_per_branch rows (branch limits apply to them
    at all) AND the employee's branch isn't among them. No rows -> unrestricted."""
    branches = responsible_branches_for_user(user)
    if not branches:
        return False
    return _employee_branch(employee) not in branches


def _sql_branch_condition(doctype: str, employee_field: str, user: str | None) -> str | None:
    branches = responsible_branches_for_user(user)
    if not branches:
        return None
    escaped = ", ".join(frappe.db.escape(value) for value in branches)
    return (
        f"`tab{doctype}`.`{employee_field}` IN "
        f"(SELECT name FROM `tabEmployee` WHERE branch IN ({escaped}))"
    )


def _permission_query(doctype: str, user: str | None) -> str:
    employee_field = BRANCH_LIMITED_DOCTYPES.get(doctype)
    if not employee_field:
        return ""
    return _sql_branch_condition(doctype, employee_field, user) or ""


def _has_permission(doc, user: str | None = None, ptype: str | None = None) -> bool:
    user = user or frappe.session.user
    employee_field = BRANCH_LIMITED_DOCTYPES.get(doc.doctype)
    if not employee_field:
        return True
    if _branch_is_restricted(doc.get(employee_field), user):
        return False
    return True


def employee_checkin_permission_query_conditions(user: str | None = None) -> str:
    return _permission_query("Employee Checkin", user)


def employee_checkin_has_permission(doc, user: str | None = None, ptype: str | None = None) -> bool:
    return _has_permission(doc, user, ptype)


def clocking_adjustment_permission_query_conditions(user: str | None = None) -> str:
    return _permission_query("Clocking Adjustment", user)


def clocking_adjustment_has_permission(doc, user: str | None = None, ptype: str | None = None) -> bool:
    return _has_permission(doc, user, ptype)
