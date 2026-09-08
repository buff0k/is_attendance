// Copyright (c) 2026, BuFf0k and contributors
// For license information, please see license.txt

/*
 * Roster-style visual timeline: each date in [from_date, to_date] is one
 * ROW, with a shared 00:00-24:00 axis running left to right across every
 * row (a single hour ruler header above them all, so times line up
 * vertically between rows like a schedule/roster). Each Employee Checkin
 * is a pill-shaped block (its time shown inside it) positioned by its
 * time-of-day on that date's row, with a green bar connecting a paired
 * IN to its OUT so the worked interval reads at a glance. Drag a pill to
 * retime it, click one to type an exact time precisely, click empty
 * space on a row to add a missing punch (or use "Batch Add Clockings" to
 * fill many days from one shift pattern at once), use the small x on a
 * pill to remove it, use the small ⇄ to flip its IN/OUT by hand. All of
 * this edits the exact same `checkin_rows` child table the server-side
 * controller already works against (load_checkins/on_submit/on_cancel,
 * all unchanged) - this widget is purely a different way to view and
 * edit that same data. The real grid is kept (hidden by default) as a
 * fallback for typing values directly. See "Edit as Table".
 *
 * By default IN/OUT is auto-derived, not set by hand: every mutation
 * re-alternates ALL of this employee's active (non-removed) punches, in
 * one pass, by time (earliest = IN, then alternating) and writes that
 * back onto log_type - so adding an earlier punch than the current IN
 * correctly makes the new one the IN and flips the old one to OUT, and
 * every other change ripples the same way. This is what
 * attendance_sync._normalize_log_types() does server-side too when it
 * computes actual hours (it discards whatever log_type is stored and
 * re-derives it the same way), so auto-alternation is right for the
 * overwhelming majority of real punches.
 *
 * Deliberately global, not scoped per calendar day (rows are still
 * grouped by day for rendering - see cadj_rows_by_day - just not for this
 * calculation): resetting to "expected IN" at every midnight is exactly
 * what used to silently relabel a real overnight shift's day-2 OUT back
 * to IN, since day 2's own bucket had no memory that day 1 ended
 * mid-shift. A single global sequence has no such reset - an IN on day 1
 * paired with an OUT on day 2 is honoured exactly as inserted, and for
 * any day that's fully paired on its own (the overwhelming majority),
 * this produces the identical result the old per-day version did, since
 * an even count always lands back on "expected IN" for whatever comes
 * next regardless.
 *
 * The ⇄ toggle exists for the real cases that genuinely don't alternate
 * cleanly - a double-tap, two INs in a row from a terminal glitch, etc.
 * Manually flipping a pill anchors it: its own value is never overwritten
 * again, but the sequence still continues from whatever it actually is,
 * so the correction ripples forward through every later punch instead of
 * being silently fought by the next auto-alternated one. This only
 * changes what's *stored and printed* on this document; it does not
 * change how the server computes hours once submitted (see above - that
 * recomputation never trusts stored log_type regardless of where it came
 * from), so a manual flip here is about the record being accurate, not
 * about steering the hours calculation.
 *
 * A day whose own active count is odd (an unpaired trailing punch) still
 * gets a small "may continue" connector at the right edge of its track,
 * and the next day a matching one at its left edge - a plain visual hint
 * that a shift might cross midnight, independent of how the alternation
 * above actually resolves it.
 */

const CADJ_ROW_LABEL_WIDTH = 100; // px - keeps every row's track and the ruler header the same width
const CADJ_STAGGER_THRESHOLD_MINUTES = 40; // pills this close in time get nudged apart vertically - wider pills need more room than the old dots did
const CADJ_CLICK_VS_DRAG_PX = 4; // pointerup within this many px of pointerdown counts as a click, not a drag

frappe.ui.form.on("Clocking Adjustment", {
	refresh(frm) {
		if (frm.doc.docstatus === 0) {
			frm.add_custom_button(__("Load Checkins for Period"), () => {
				const load = () => cadj_load_and_render(frm, true);

				if (frm.doc.checkin_rows && frm.doc.checkin_rows.length) {
					frappe.confirm(
						__("This replaces the pins below with a fresh load from Employee Checkin, discarding any unsaved edits. Continue?"),
						load
					);
				} else {
					load();
				}
			});

			frm.add_custom_button(__("Batch Add Clockings"), () => cadj_open_batch_dialog(frm));
			frm.add_custom_button(__("Edit as Table"), () => cadj_toggle_table(frm));
		}

		if (frm.doc.amended_from) {
			frappe.msgprint(
				__(
					"This is an amendment - reload checkins for the period before editing, since the ones copied from the original may no longer be current."
				)
			);
		}

		// The Remove flag (toggled via the pill's x, or the table's own
		// checkbox) is the only way to discard any row, new or existing -
		// disabling grid row-deletion avoids silently orphaning an existing
		// checkin with nothing left to tell submit to actually remove it.
		const grid = frm.fields_dict.checkin_rows.grid;
		grid.cannot_delete_rows = true;
		grid.refresh();

		// The table stays hidden by default - the roster is the primary
		// surface. Doesn't touch its own hidden state if the user has
		// already toggled it visible via "Edit as Table" this session.
		if (frm.doc.__cadj_table_visible === undefined) {
			frm.set_df_property("checkin_rows", "hidden", 1);
		}

		cadj_maybe_auto_load(frm);
		// Renders immediately with whatever leave info is already cached
		// (none, on first load) so the grid never waits on this fetch to
		// appear, then re-renders again once it resolves to fill in the
		// leave blocks.
		cadj_render_timeline(frm);
		cadj_maybe_reload_leave_info(frm).then(() => cadj_render_timeline(frm));
	},

	employee(frm) {
		cadj_maybe_auto_load(frm);
		cadj_maybe_reload_leave_info(frm).then(() => cadj_render_timeline(frm));
	},

	from_date(frm) {
		cadj_maybe_auto_load(frm);
		cadj_maybe_reload_leave_info(frm).then(() => cadj_render_timeline(frm));
	},

	to_date(frm) {
		cadj_maybe_auto_load(frm);
		cadj_maybe_reload_leave_info(frm).then(() => cadj_render_timeline(frm));
	},
});

// ------------------------------------------------------------------
// Leave awareness - fetched once per distinct employee/from_date/to_date
// combination (cached on frm, same "don't refetch if nothing that matters
// changed" approach cadj_maybe_auto_load uses for checkins), and rendered
// as an on-track block per day in cadj_render_timeline() below.
// ------------------------------------------------------------------

function cadj_maybe_reload_leave_info(frm) {
	if (!(frm.doc.employee && frm.doc.from_date && frm.doc.to_date)) {
		frm.__cadj_leave_info = {};
		frm.__cadj_leave_info_key = null;
		return Promise.resolve();
	}

	const key = `${frm.doc.employee}|${frm.doc.from_date}|${frm.doc.to_date}`;
	if (frm.__cadj_leave_info_key === key) {
		return Promise.resolve();
	}

	return frm.call("get_leave_info").then((r) => {
		frm.__cadj_leave_info = r.message || {};
		frm.__cadj_leave_info_key = key;
	});
}

function cadj_open_leave_dialog(frm, day_key) {
	frappe.prompt(
		[
			{
				fieldname: "leave_type",
				label: __("Leave Type"),
				fieldtype: "Link",
				options: "Leave Type",
				reqd: 1,
			},
			{
				fieldname: "half_day",
				label: __("Half Day"),
				fieldtype: "Check",
				default: 0,
			},
		],
		(values) => {
			frm.call("create_leave_for_date", {
				date: day_key,
				leave_type: values.leave_type,
				half_day: values.half_day,
			}).then((r) => {
				const leave_name = r.message;
				frm.__cadj_leave_info_key = null; // force a refetch so the new block shows
				cadj_maybe_reload_leave_info(frm).then(() => cadj_render_timeline(frm));
				frappe.show_alert({
					message: __('Leave Application <a href="/app/leave-application/{0}" target="_blank">{0}</a> created (still Draft - submit it through the normal Leave approval flow).', [
						leave_name,
					]),
					indicator: "green",
				});
			});
		},
		__("Create Leave Application for {0}", [frappe.datetime.str_to_user(day_key, false, true)]),
		__("Create")
	);
}

// ------------------------------------------------------------------
// Batch Add - fills many days from one shift pattern in a single action
// instead of clicking twice per day. Only ever adds new punches; never
// edits or removes anything already there.
// ------------------------------------------------------------------

function cadj_open_batch_dialog(frm) {
	frappe.prompt(
		[
			{
				fieldname: "from_date",
				label: __("From Date"),
				fieldtype: "Date",
				default: frm.doc.from_date,
				reqd: 1,
			},
			{
				fieldname: "to_date",
				label: __("To Date"),
				fieldtype: "Date",
				default: frm.doc.to_date,
				reqd: 1,
			},
			{
				fieldname: "which_days",
				label: __("Which Days"),
				fieldtype: "Select",
				options: [__("Every day"), __("Weekdays only (Mon-Fri)"), __("Weekends only (Sat-Sun)")].join("\n"),
				default: __("Every day"),
			},
			{
				fieldname: "column_break_times",
				fieldtype: "Column Break",
			},
			{
				fieldname: "in_time",
				label: __("In Time"),
				fieldtype: "Time",
				reqd: 1,
			},
			{
				fieldname: "out_time",
				label: __("Out Time"),
				fieldtype: "Time",
				reqd: 1,
			},
			{
				fieldname: "overnight",
				label: __("Overnight shift (Out is the next day)"),
				fieldtype: "Check",
				default: 0,
			},
			{
				fieldname: "skip_existing",
				label: __("Skip days that already have clockings"),
				fieldtype: "Check",
				default: 1,
			},
		],
		(values) => cadj_apply_batch(frm, values),
		__("Batch Add Clockings"),
		__("Add")
	);
}

function cadj_apply_batch(frm, values) {
	const day_filters = {
		[__("Every day")]: () => true,
		[__("Weekdays only (Mon-Fri)")]: (d) => d.getDay() >= 1 && d.getDay() <= 5,
		[__("Weekends only (Sat-Sun)")]: (d) => d.getDay() === 0 || d.getDay() === 6,
	};
	const day_filter = day_filters[values.which_days] || day_filters[__("Every day")];

	const by_day = cadj_rows_by_day(frm);
	const [in_hh, in_mm] = values.in_time.split(":").map(Number);
	const [out_hh, out_mm] = values.out_time.split(":").map(Number);

	let added_days = 0;

	cadj_date_range(values.from_date, values.to_date).forEach((day) => {
		if (!day_filter(day)) return;

		const key = cadj_date_key(day);
		const existing_active = (by_day[key] || []).filter((row) => !row.remove);
		if (values.skip_existing && existing_active.length) return;

		const in_time = new Date(day);
		in_time.setHours(in_hh, in_mm, 0, 0);
		const out_time = new Date(day);
		out_time.setHours(out_hh, out_mm, 0, 0);
		if (values.overnight) {
			out_time.setDate(out_time.getDate() + 1);
		}

		frm.add_child("checkin_rows", { time: cadj_format_datetime(in_time), log_type: "IN", remove: 0 });
		frm.add_child("checkin_rows", { time: cadj_format_datetime(out_time), log_type: "OUT", remove: 0 });
		added_days += 1;
	});

	frm.refresh_field("checkin_rows");
	if (added_days) {
		cadj_after_mutation(frm);
	}

	frappe.show_alert({
		message: added_days
			? __("Added clockings for {0} day(s).", [added_days])
			: __("No days matched - everything in range already had clockings, or the day filter excluded them all."),
		indicator: added_days ? "green" : "orange",
	});
}

function cadj_toggle_table(frm) {
	const now_hidden = !!frm.get_docfield("checkin_rows").hidden;
	frm.set_df_property("checkin_rows", "hidden", !now_hidden ? 1 : 0);
	frm.doc.__cadj_table_visible = now_hidden; // becoming visible
	frm.refresh_field("checkin_rows");
}

function cadj_maybe_auto_load(frm) {
	if (frm.doc.docstatus !== 0) return;
	if (frm._cadj_loading) return;
	if (!(frm.doc.employee && frm.doc.from_date && frm.doc.to_date)) return;
	if (frm.doc.checkin_rows && frm.doc.checkin_rows.length) return; // never silently overwrite existing rows

	cadj_load_and_render(frm, false);
}

function cadj_load_and_render(frm, notify) {
	frm._cadj_loading = true;
	frm
		.call("load_checkins")
		.then(() => {
			frm.refresh_field("checkin_rows");
			frm.dirty();
			cadj_realternate_all_days(frm);
			cadj_render_timeline(frm);
			cadj_maybe_reload_leave_info(frm).then(() => cadj_render_timeline(frm));
			if (notify) {
				frappe.show_alert({
					message: __("Checkins loaded for the period."),
					indicator: "green",
				});
			}
		})
		.finally(() => {
			frm._cadj_loading = false;
		});
}

// ------------------------------------------------------------------
// Date/time helpers - deliberately not relying on the Date constructor's
// string-parsing quirks or on moment.js being available; parses/formats
// Frappe's own "YYYY-MM-DD HH:mm:ss" / "YYYY-MM-DD" shapes explicitly.
// ------------------------------------------------------------------

function cadj_pad(n) {
	return String(n).padStart(2, "0");
}

function cadj_parse_datetime(value) {
	if (!value) return null;
	const [datePart, timePart = "00:00:00"] = value.split(" ");
	const [y, m, d] = datePart.split("-").map(Number);
	const [hh = 0, mm = 0, ss = 0] = timePart.split(":").map(Number);
	return new Date(y, m - 1, d, hh, mm, ss);
}

function cadj_format_datetime(dateObj) {
	return (
		`${dateObj.getFullYear()}-${cadj_pad(dateObj.getMonth() + 1)}-${cadj_pad(dateObj.getDate())} ` +
		`${cadj_pad(dateObj.getHours())}:${cadj_pad(dateObj.getMinutes())}:${cadj_pad(dateObj.getSeconds())}`
	);
}

function cadj_date_key(dateObj) {
	return `${dateObj.getFullYear()}-${cadj_pad(dateObj.getMonth() + 1)}-${cadj_pad(dateObj.getDate())}`;
}

function cadj_date_range(from_date_str, to_date_str) {
	const [fy, fm, fd] = from_date_str.split("-").map(Number);
	const [ty, tm, td] = to_date_str.split("-").map(Number);
	const cur = new Date(fy, fm - 1, fd);
	const end = new Date(ty, tm - 1, td);
	const days = [];
	while (cur <= end) {
		days.push(new Date(cur));
		cur.setDate(cur.getDate() + 1);
	}
	return days;
}

function cadj_minutes_of_day(dateObj) {
	return dateObj.getHours() * 60 + dateObj.getMinutes() + dateObj.getSeconds() / 60;
}

// ------------------------------------------------------------------
// IN/OUT auto-alternation - every mutation re-derives log_type for ALL
// active (non-removed) rows across the whole loaded period, in one
// chronological pass (see the module comment at the top on why this is
// global rather than per calendar day). This is what makes adding an
// earlier punch than the current IN correctly turn the new one into the
// IN and flip the old one to OUT, and ripples the same way through every
// later change - including across a midnight boundary.
// ------------------------------------------------------------------

function cadj_rows_by_day(frm) {
	const by_day = {};
	for (const row of frm.doc.checkin_rows || []) {
		const reference_time = row.time || row.original_time;
		if (!reference_time) continue;
		const key = cadj_date_key(cadj_parse_datetime(reference_time));
		(by_day[key] = by_day[key] || []).push(row);
	}
	return by_day;
}

function cadj_active_sorted(day_rows) {
	return day_rows
		.filter((row) => !row.remove)
		.slice()
		.sort((a, b) => cadj_parse_datetime(a.time) - cadj_parse_datetime(b.time));
}

function cadj_realternate_all_days(frm) {
	// One pass, in pure chronological order across the WHOLE loaded
	// period - deliberately not scoped per calendar day. Alternating
	// separately per day (resetting to "expected IN" at every midnight)
	// is what silently relabelled a real overnight shift's day-2 OUT back
	// to IN: day 2's own bucket has no memory that day 1 ended mid-shift.
	// A single global sequence has no such reset, so an IN on day 1
	// paired with an OUT on day 2 is honoured exactly as inserted - and
	// for the overwhelming majority of days (fully paired, no overnight
	// crossing), this produces the identical result the old per-day
	// version did, since an even count within a day always lands back on
	// "expected IN" for whatever comes next anyway.
	const active = (frm.doc.checkin_rows || [])
		.filter((row) => !row.remove && (row.time || row.original_time))
		.slice()
		.sort((a, b) => cadj_parse_datetime(a.time || a.original_time) - cadj_parse_datetime(b.time || b.original_time));

	let expected = "IN";
	for (const row of active) {
		// A manually-set pin (the ⇄ toggle - see cadj_wire_pin) is an
		// anchor: its own value is never overwritten, but the sequence
		// still continues from whatever it actually is, so a genuine
		// correction correctly ripples forward through every later punch
		// instead of being silently fought by the next auto-alternated
		// one.
		if (!row.__cadj_manual && row.log_type !== expected) {
			frappe.model.set_value(row.doctype, row.name, "log_type", expected);
		}
		expected = row.log_type === "IN" ? "OUT" : "IN";
	}
}

function cadj_after_mutation(frm) {
	cadj_realternate_all_days(frm);
	frm.dirty();
	cadj_render_timeline(frm);
}

// ------------------------------------------------------------------
// Row classification - drives the row's background tint and the
// overnight "may continue" connectors (see module comment up top).
// Mirrors attendance_compliance_summary.py's own treatment of a
// half-day-leave date (a single punch is expected, not flagged) closely
// enough to look consistent with what that report and the Dashboard
// already show, computed straight from the rows already loaded
// client-side - no server round trip needed for this.
// ------------------------------------------------------------------

function cadj_classify_day(day_rows, leave_here) {
	const active = cadj_active_sorted(day_rows);
	const is_full_leave = !!(leave_here && !leave_here.half_day);
	const is_half_leave = !!(leave_here && leave_here.half_day);

	if (is_full_leave) {
		return { tint: "leave", ends_unpaired: false };
	}

	if (!active.length) {
		return { tint: "missing", ends_unpaired: false };
	}

	const ends_unpaired = active.length % 2 === 1;

	if (ends_unpaired && is_half_leave) {
		// A single punch is exactly what a real half day looks like - not
		// flagged, same reasoning attendance_compliance_summary.py uses.
		return { tint: "leave", ends_unpaired: true };
	}

	if (ends_unpaired) {
		return { tint: "incomplete", ends_unpaired: true };
	}

	return { tint: "ok", ends_unpaired: false };
}

function cadj_compute_intervals(active_sorted_rows) {
	const intervals = [];
	for (let i = 0; i + 1 < active_sorted_rows.length; i += 2) {
		const start = cadj_minutes_of_day(cadj_parse_datetime(active_sorted_rows[i].time));
		const end = cadj_minutes_of_day(cadj_parse_datetime(active_sorted_rows[i + 1].time));
		if (end > start) intervals.push({ start, end });
	}
	return intervals;
}

// ------------------------------------------------------------------
// Rendering - a roster grid: row label (date) + a shared-width 24h track
// per row, all aligned under one hour-ruler header.
// ------------------------------------------------------------------

function cadj_ensure_style() {
	if (document.getElementById("cadj-timeline-style")) return;

	const style = document.createElement("style");
	style.id = "cadj-timeline-style";
	style.textContent = `
		.cadj-roster { width: 100%; }
		.cadj-roster-row {
			display: flex;
			align-items: center;
			margin-bottom: 6px;
			padding: 4px;
			border-radius: 6px;
		}
		.cadj-roster-row.cadj-row-missing { background: rgba(192, 57, 43, 0.08); }
		.cadj-roster-row.cadj-row-incomplete { background: rgba(212, 160, 23, 0.12); }
		.cadj-roster-row.cadj-row-ok { background: rgba(46, 139, 87, 0.05); }
		.cadj-row-label {
			flex: 0 0 ${CADJ_ROW_LABEL_WIDTH}px;
			width: ${CADJ_ROW_LABEL_WIDTH}px;
			font-size: 11px;
			font-weight: 600;
			color: var(--text-muted);
			padding-right: 8px;
			text-align: right;
			display: flex;
			flex-direction: column;
			align-items: flex-end;
		}
		.cadj-row-status-dot {
			display: inline-block;
			width: 7px;
			height: 7px;
			border-radius: 50%;
			margin-right: 4px;
		}
		.cadj-row-status-dot.cadj-row-missing { background: #c0392b; }
		.cadj-row-status-dot.cadj-row-incomplete { background: #d4a017; }
		.cadj-row-status-dot.cadj-row-ok { background: #2e8b57; }
		.cadj-row-status-dot.cadj-row-leave { background: #6c757d; }
		.cadj-leave-action {
			display: none;
			font-size: 10px;
			color: var(--text-muted);
			cursor: pointer;
			margin-top: 3px;
			text-decoration: underline;
		}
		.cadj-roster-row:hover .cadj-leave-action { display: inline-block; }
		.cadj-ruler-row { display: flex; align-items: center; margin-bottom: 6px; }
		.cadj-ruler-spacer { flex: 0 0 ${CADJ_ROW_LABEL_WIDTH}px; width: ${CADJ_ROW_LABEL_WIDTH}px; }
		.cadj-ruler-track {
			position: relative;
			flex: 1 1 auto;
			height: 16px;
		}
		.cadj-ruler-tick {
			position: absolute;
			top: 0;
			font-size: 10px;
			color: var(--text-muted);
			transform: translateX(-50%);
		}
		.cadj-track {
			position: relative;
			flex: 1 1 auto;
			height: 52px;
			background: var(--control-bg, #f4f5f6);
			border: 1px solid var(--border-color, #d1d8dd);
			border-radius: 6px;
			cursor: copy;
		}
		.cadj-track.cadj-readonly { cursor: default; }
		.cadj-hour-line {
			position: absolute;
			top: 0;
			bottom: 0;
			width: 0;
			border-left: 1px dashed var(--border-color, #e6e9eb);
		}
		.cadj-empty-hint {
			position: absolute;
			left: 8px;
			top: 50%;
			transform: translateY(-50%);
			font-size: 11px;
			color: var(--text-muted);
			pointer-events: none;
		}
		.cadj-interval {
			position: absolute;
			top: 50%;
			transform: translateY(-50%);
			height: 26px;
			background: rgba(46, 139, 87, 0.35);
			border-radius: 6px;
			z-index: 1;
			pointer-events: none;
		}
		.cadj-leave-block {
			position: absolute;
			top: 6px;
			bottom: 6px;
			left: 2px;
			right: 2px;
			border-radius: 5px;
			display: flex;
			align-items: center;
			justify-content: center;
			font-size: 11px;
			font-weight: 600;
			z-index: 1;
			text-decoration: none;
			overflow: hidden;
			white-space: nowrap;
			text-overflow: ellipsis;
			padding: 0 6px;
		}
		.cadj-leave-block.cadj-leave-full { background: #e6f4ea; color: #1e7e34; border: 1px solid #b7dfc0; }
		.cadj-leave-block.cadj-leave-pending { opacity: 0.7; }
		.cadj-leave-tag {
			position: absolute;
			top: 3px;
			left: 3px;
			font-size: 9px;
			font-weight: 600;
			padding: 1px 6px;
			border-radius: 8px;
			z-index: 1;
			text-decoration: none;
			background: #fff8e1;
			color: #8a6d00;
			border: 1px dashed #f0dfa0;
			max-width: 60%;
			overflow: hidden;
			white-space: nowrap;
			text-overflow: ellipsis;
		}
		.cadj-leave-tag.cadj-leave-pending { opacity: 0.7; }
		.cadj-connector {
			position: absolute;
			top: 50%;
			transform: translateY(-50%);
			font-size: 14px;
			color: var(--text-muted);
			z-index: 3;
			pointer-events: none;
		}
		.cadj-connector-right { right: -13px; }
		.cadj-connector-left { left: -13px; }
		.cadj-pin {
			position: absolute;
			top: 50%;
			min-width: 54px;
			height: 30px;
			line-height: 26px;
			padding: 0 8px;
			border-radius: 15px;
			transform: translate(-50%, -50%);
			cursor: grab;
			border: 2px solid #fff;
			box-shadow: 0 1px 3px rgba(0,0,0,0.3);
			z-index: 2;
			text-align: center;
			font-size: 11px;
			font-weight: 600;
			color: #fff;
			white-space: nowrap;
		}
		.cadj-pin.cadj-dragging { cursor: grabbing; z-index: 6; }
		.cadj-pin.cadj-in { background: #2e8b57; }
		.cadj-pin.cadj-out { background: #c0392b; }
		.cadj-pin.cadj-new { border-style: dashed; border-color: #2e8b57; }
		.cadj-pin.cadj-removed { background: #999 !important; opacity: 0.5; text-decoration: line-through; }
		.cadj-pin.cadj-corrected { box-shadow: 0 0 0 2px #d4a017; }
		.cadj-pin.cadj-manual { box-shadow: 0 0 0 2px #6c5ce7; }
		.cadj-pin-remove, .cadj-pin-toggle {
			position: absolute;
			top: -8px;
			width: 14px;
			height: 14px;
			line-height: 12px;
			text-align: center;
			font-size: 10px;
			border-radius: 50%;
			background: #fff;
			color: #333;
			border: 1px solid var(--border-color, #d1d8dd);
			display: none;
			cursor: pointer;
			z-index: 3;
		}
		.cadj-pin-remove { right: -8px; }
		.cadj-pin-toggle { left: -8px; }
		.cadj-pin:hover .cadj-pin-remove, .cadj-pin:hover .cadj-pin-toggle { display: block; }
		.cadj-time-edit {
			position: absolute;
			top: -30px;
			transform: translateX(-50%);
			z-index: 10;
			width: 90px;
		}
	`;
	document.head.appendChild(style);
}

function cadj_render_timeline(frm) {
	const field = frm.get_field("checkin_timeline");
	if (!field || !field.$wrapper) return;

	cadj_ensure_style();

	const $wrapper = field.$wrapper;
	$wrapper.empty();

	if (!frm.doc.from_date || !frm.doc.to_date) {
		$wrapper.append(`<div class="text-muted">${__("Set Employee, From Date, and To Date to load checkins.")}</div>`);
		return;
	}

	const readonly = frm.doc.docstatus !== 0;
	const $roster = $(`<div class="cadj-roster"></div>`).appendTo($wrapper);

	// One shared hour ruler above every row, so times line up vertically.
	const $ruler_row = $(`<div class="cadj-ruler-row"></div>`).appendTo($roster);
	$ruler_row.append(`<div class="cadj-ruler-spacer"></div>`);
	const $ruler_track = $(`<div class="cadj-ruler-track"></div>`).appendTo($ruler_row);
	[0, 6, 12, 18, 24].forEach((hour) => {
		const left_pct = (hour / 24) * 100;
		const label = hour === 24 ? "24:00" : `${cadj_pad(hour)}:00`;
		const align = hour === 0 ? "left: 0;" : hour === 24 ? "left: 100%; transform: translateX(-100%);" : `left:${left_pct}%;`;
		$ruler_track.append(`<div class="cadj-ruler-tick" style="${align}">${label}</div>`);
	});

	const by_day = cadj_rows_by_day(frm);
	const leave_info = frm.__cadj_leave_info || {};
	let previous_ended_unpaired = false;

	cadj_date_range(frm.doc.from_date, frm.doc.to_date).forEach((day) => {
		const key = cadj_date_key(day);
		const day_rows = by_day[key] || [];
		const active = cadj_active_sorted(day_rows);
		const leave_here = leave_info[key];
		const classification = cadj_classify_day(day_rows, leave_here);
		const starts_unpaired = previous_ended_unpaired;

		const $row = $(`<div class="cadj-roster-row cadj-row-${classification.tint}"></div>`).appendTo($roster);
		const $label = $(`<div class="cadj-row-label"></div>`).appendTo($row);
		$label.append(
			`<div><span class="cadj-row-status-dot cadj-row-${classification.tint}"></span>${frappe.datetime.str_to_user(key, false, true)}</div>`
		);

		if (!readonly) {
			const $leave_action = $(
				`<div class="cadj-leave-action">${leave_here ? __("+ another leave") : __("+ leave")}</div>`
			).appendTo($label);
			$leave_action.on("click", (event) => {
				event.stopPropagation();
				cadj_open_leave_dialog(frm, key);
			});
		}

		const $track = $(`<div class="cadj-track ${readonly ? "cadj-readonly" : ""}"></div>`).appendTo($row);

		[6, 12, 18].forEach((hour) => {
			$track.append(`<div class="cadj-hour-line" style="left:${(hour / 24) * 100}%"></div>`);
		});

		if (leave_here) {
			const pending = leave_here.status && leave_here.status !== "Approved" ? "cadj-leave-pending" : "";
			const href = `/app/leave-application/${encodeURIComponent(leave_here.leave_application)}`;
			const title = `${frappe.utils.escape_html(leave_here.leave_application)} (${frappe.utils.escape_html(leave_here.status)}) - ${__(
				"click to open"
			)}`;

			if (leave_here.half_day) {
				// A half day still expects a real partial clocking - a small
				// corner tag rather than covering the track, so the rest of
				// it stays clickable for adding/dragging real punches.
				$(
					`<a class="cadj-leave-tag ${pending}" href="${href}" target="_blank" title="${title}">${__("Half Day")}: ${frappe.utils.escape_html(
						leave_here.leave_type
					)}</a>`
				)
					.appendTo($track)
					.on("click", (event) => event.stopPropagation());
			} else {
				// Sized to the employee's actual Shift Assignment for this
				// date when one exists (shift_start_minutes/end_minutes,
				// from get_leave_info() - see clocking_adjustment.py's own
				// _shift_minutes_for_date), so the block represents their
				// real expected hours rather than the whole calendar day.
				// Falls back to spanning the full track (the CSS default
				// inset, left/right unset here) when there's no Shift
				// Assignment to derive hours from at all - a Leave
				// Application itself never stores clock times.
				let inline_style = "";
				if (leave_here.shift_start_minutes != null && leave_here.shift_end_minutes != null) {
					const left_pct = (leave_here.shift_start_minutes / 1440) * 100;
					const width_pct = ((leave_here.shift_end_minutes - leave_here.shift_start_minutes) / 1440) * 100;
					inline_style = ` style="left:${left_pct}%; right:auto; width:${width_pct}%;"`;
				}
				$(
					`<a class="cadj-leave-block cadj-leave-full ${pending}"${inline_style} href="${href}" target="_blank" title="${title}">${__("On Leave")}: ${frappe.utils.escape_html(
						leave_here.leave_type
					)}</a>`
				)
					.appendTo($track)
					.on("click", (event) => event.stopPropagation());
			}
		}

		const is_full_leave_block = leave_here && !leave_here.half_day;
		if (!day_rows.length && !is_full_leave_block) {
			const hint = readonly ? "" : __("Click to add a punch");
			$track.append(`<span class="cadj-empty-hint">${hint}</span>`);
		}

		cadj_compute_intervals(active).forEach((interval) => {
			const left_pct = (interval.start / 1440) * 100;
			const width_pct = ((interval.end - interval.start) / 1440) * 100;
			$track.append(`<div class="cadj-interval" style="left:${left_pct}%; width:${width_pct}%;"></div>`);
		});

		if (starts_unpaired) {
			$track.append(
				`<span class="cadj-connector cadj-connector-left" title="${__("May continue from the previous day's shift")}">◀</span>`
			);
		}
		if (classification.ends_unpaired) {
			$track.append(
				`<span class="cadj-connector cadj-connector-right" title="${__("May continue into the next day's shift")}">▶</span>`
			);
		}

		let last_minutes = null;
		let stagger_toggle = false;

		active.forEach((row) => {
			const time_obj = cadj_parse_datetime(row.time);
			const minutes = cadj_minutes_of_day(time_obj);
			const left_pct = (minutes / 1440) * 100;

			const is_staggered = last_minutes !== null && Math.abs(minutes - last_minutes) < CADJ_STAGGER_THRESHOLD_MINUTES;
			if (is_staggered) {
				stagger_toggle = !stagger_toggle;
			} else {
				stagger_toggle = false;
			}
			last_minutes = minutes;
			// Offset from vertical center, not an absolute top - 0 keeps a
			// pin centered on (bookending) the green interval bar; only
			// pins close together in time move off-center, alternating
			// above/below so they don't overlap each other.
			const y_offset = is_staggered ? (stagger_toggle ? -16 : 16) : 0;

			cadj_render_pin(frm, $track, row, time_obj, left_pct, y_offset, key, readonly);
		});

		// Removed rows aren't in `active` (the interval/pairing math above
		// ignores them, matching what will actually submit) but still need
		// to be visible so undoing a removal is possible from the roster
		// itself, not just the table.
		day_rows
			.filter((row) => row.remove)
			.forEach((row) => {
				const time_obj = cadj_parse_datetime(row.time || row.original_time);
				const minutes = cadj_minutes_of_day(time_obj);
				const left_pct = (minutes / 1440) * 100;
				cadj_render_pin(frm, $track, row, time_obj, left_pct, 0, key, readonly);
			});

		if (!readonly) {
			cadj_wire_track(frm, $track, day, key);
		}

		previous_ended_unpaired = classification.ends_unpaired;
	});
}

function cadj_render_pin(frm, $track, row, time_obj, left_pct, y_offset, day_key, readonly) {
	const is_new = !row.checkin;
	const is_removed = !!row.remove;
	const is_corrected =
		!is_new && !is_removed && (row.time !== row.original_time || row.log_type !== row.original_log_type);

	const classes = [
		"cadj-pin",
		row.log_type === "OUT" ? "cadj-out" : "cadj-in",
		is_new ? "cadj-new" : "",
		is_removed ? "cadj-removed" : "",
		is_corrected ? "cadj-corrected" : "",
		row.__cadj_manual ? "cadj-manual" : "",
	]
		.filter(Boolean)
		.join(" ");

	// transform (not top) carries the vertical centering + stagger offset,
	// so a pin's own visual middle lines up with the green interval bar's
	// middle (see .cadj-track's fixed height) - dragging only ever
	// animates `left` afterward, leaving this transform untouched.
	const $pin = $(`
		<div class="${classes}" style="left:${left_pct}%; transform: translate(-50%, calc(-50% + ${y_offset}px));" title="${__("Drag to retime, click to type an exact time")}">
			<span class="cadj-pin-time">${cadj_pad(time_obj.getHours())}:${cadj_pad(time_obj.getMinutes())}</span>
			${readonly ? "" : `<span class="cadj-pin-toggle" title="${__("Manually flip to IN/OUT")}">⇄</span>`}
			${readonly ? "" : `<span class="cadj-pin-remove">&times;</span>`}
		</div>
	`).appendTo($track);

	if (!readonly) {
		cadj_wire_pin(frm, $pin, $track, row, time_obj, day_key);
	}
}

// ------------------------------------------------------------------
// Interaction wiring - all within one day's own track (percentage-based,
// 0-100% = 00:00-24:00 for that row), matching the roster's one-row-per-
// day model - a drag stays within its own row/day.
// ------------------------------------------------------------------

function cadj_wire_pin(frm, $pin, $track, row, time_obj, day_key) {
	// Remove toggle - stops propagation so it never also triggers the
	// pin's own click/drag handling.
	$pin.find(".cadj-pin-remove").on("pointerdown click", (event) => {
		event.stopPropagation();
		event.preventDefault();
		frappe.model.set_value(row.doctype, row.name, "remove", row.remove ? 0 : 1);
		cadj_after_mutation(frm);
	});

	// Manual IN/OUT flip - marks this row (not a real DocField, so it's
	// never saved/submitted - purely an in-session "leave this one alone,
	// but count it when working out what comes next" anchor for
	// cadj_realternate_all_days, see that function's own comment).
	$pin.find(".cadj-pin-toggle").on("pointerdown click", (event) => {
		event.stopPropagation();
		event.preventDefault();
		row.__cadj_manual = true;
		frappe.model.set_value(row.doctype, row.name, "log_type", row.log_type === "OUT" ? "IN" : "OUT");
		cadj_after_mutation(frm);
	});

	$pin.on("pointerdown", (event) => {
		if ($(event.target).hasClass("cadj-pin-remove") || $(event.target).hasClass("cadj-pin-toggle")) return;
		event.preventDefault();
		event.stopPropagation();

		const pointer_id = event.originalEvent.pointerId;
		$pin[0].setPointerCapture(pointer_id);

		const start_x = event.clientX;
		let moved_px = 0;
		let current_minutes = cadj_minutes_of_day(time_obj);

		$pin.addClass("cadj-dragging");

		const on_move = (move_event) => {
			const track_rect = $track[0].getBoundingClientRect();
			moved_px = Math.abs(move_event.clientX - start_x);

			let fraction = (move_event.clientX - track_rect.left) / track_rect.width;
			fraction = Math.min(1, Math.max(0, fraction));
			current_minutes = Math.round(fraction * 1440);

			$pin.css("left", `${(current_minutes / 1440) * 100}%`);
			$pin.find(".cadj-pin-time").text(`${cadj_pad(Math.floor(current_minutes / 60))}:${cadj_pad(current_minutes % 60)}`);
		};

		const on_up = () => {
			$pin[0].releasePointerCapture(pointer_id);
			$pin.off("pointermove", on_move);
			$pin.off("pointerup", on_up);
			$pin.removeClass("cadj-dragging");

			if (moved_px <= CADJ_CLICK_VS_DRAG_PX) {
				cadj_open_time_editor(frm, $pin, row, time_obj, day_key);
				return;
			}

			current_minutes = Math.min(current_minutes, 1439);
			const new_time = new Date(time_obj);
			new_time.setHours(Math.floor(current_minutes / 60), current_minutes % 60, 0, 0);
			frappe.model.set_value(row.doctype, row.name, "time", cadj_format_datetime(new_time));
			cadj_after_mutation(frm);
		};

		$pin.on("pointermove", on_move);
		$pin.on("pointerup", on_up);
	});
}

function cadj_open_time_editor(frm, $pin, row, time_obj, day_key) {
	$pin.find(".cadj-time-edit").remove();

	const $input = $(
		`<input type="time" class="cadj-time-edit form-control input-xs" value="${cadj_pad(time_obj.getHours())}:${cadj_pad(
			time_obj.getMinutes()
		)}">`
	).appendTo($pin);

	$input[0].focus();
	$input[0].showPicker && $input[0].showPicker();

	const commit = () => {
		const value = $input.val();
		if (!value) {
			$input.remove();
			return;
		}
		const [hh, mm] = value.split(":").map(Number);
		const new_time = new Date(time_obj);
		new_time.setHours(hh, mm, 0, 0);
		frappe.model.set_value(row.doctype, row.name, "time", cadj_format_datetime(new_time));
		cadj_after_mutation(frm);
	};

	$input.on("blur", commit);
	$input.on("keydown", (event) => {
		if (event.key === "Enter") commit();
		if (event.key === "Escape") cadj_render_timeline(frm);
	});
	$input.on("click", (event) => event.stopPropagation());
}

function cadj_wire_track(frm, $track, day, day_key) {
	$track.on("click", (event) => {
		if ($(event.target).closest(".cadj-pin, .cadj-leave-block, .cadj-leave-tag").length) return; // handled by that element itself

		const track_rect = $track[0].getBoundingClientRect();
		let fraction = (event.clientX - track_rect.left) / track_rect.width;
		fraction = Math.min(1, Math.max(0, fraction));
		const minutes = Math.min(Math.round(fraction * 1440), 1439);

		const new_time = new Date(day);
		new_time.setHours(Math.floor(minutes / 60), minutes % 60, 0, 0);

		frm.add_child("checkin_rows", {
			time: cadj_format_datetime(new_time),
			log_type: "IN",
			remove: 0,
		});
		frm.refresh_field("checkin_rows");
		cadj_after_mutation(frm);
	});
}
