# Copyright (c) 2026, BuFf0k and contributors
# For license information, please see license.txt

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

CHECKIN_DOCTYPE = "Employee Checkin"
OLD_FIELDNAME = "custom_site"
OLD_CUSTOM_FIELD = "Employee Checkin-custom_site"
OLD_PROPERTY_SETTER = "Employee Checkin-custom_site"
NEW_FIELDNAME = "isa_branch"


def execute():
    """
    Migrate Employee Checkin site data from the old `ir`-owned custom_site
    field to the `is_attendance`-owned isa_branch field, then retire the
    old field.

    This app's `isa_branch` field is normally created via fixture sync, but
    fixtures sync *after* post_model_sync patches run (see
    frappe.migrate.migrate), so this patch cannot assume the column already
    exists on the same `bench migrate` run that introduces it. It creates
    the field itself first (idempotent) to make the migration correct
    regardless of ordering.
    """
    if not frappe.db.exists("DocType", CHECKIN_DOCTYPE):
        return

    _ensure_isa_branch_field_exists()
    _copy_site_values()
    _retire_old_field()


def _ensure_isa_branch_field_exists():
    create_custom_fields(
        {
            CHECKIN_DOCTYPE: [
                {
                    "fieldname": NEW_FIELDNAME,
                    "fieldtype": "Link",
                    "options": "Branch",
                    "label": "Branch",
                    "insert_after": "attendance",
                }
            ]
        },
        ignore_validate=True,
    )


def _has_columns(*fieldnames):
    return all(
        frappe.db.has_column(CHECKIN_DOCTYPE, fieldname)
        for fieldname in fieldnames
    )


def _copy_site_values():
    if not _has_columns(OLD_FIELDNAME, NEW_FIELDNAME):
        return

    frappe.db.sql(
        f"""
        UPDATE `tabEmployee Checkin`
        SET `{NEW_FIELDNAME}` = `{OLD_FIELDNAME}`
        WHERE (`{NEW_FIELDNAME}` IS NULL OR `{NEW_FIELDNAME}` = '')
          AND (`{OLD_FIELDNAME}` IS NOT NULL AND `{OLD_FIELDNAME}` != '')
        """
    )


def _retire_old_field():
    """
    Remove the old custom_site Custom Field (and Property Setter, if any)
    now that its values have been copied to isa_branch. The `ir` app no
    longer declares either as a fixture, so nothing re-creates them.
    """
    if frappe.db.exists("Custom Field", OLD_CUSTOM_FIELD):
        frappe.delete_doc(
            "Custom Field",
            OLD_CUSTOM_FIELD,
            ignore_missing=True,
            force=True,
        )

    if frappe.db.exists("Property Setter", OLD_PROPERTY_SETTER):
        frappe.delete_doc(
            "Property Setter",
            OLD_PROPERTY_SETTER,
            ignore_missing=True,
            force=True,
        )
