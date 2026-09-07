// Copyright (c) 2026, BuFf0k and contributors
// For license information, please see license.txt

/*
 * Roster-style visual timeline: each date in [from_date, to_date] is one
 * ROW, with a shared 00:00-24:00 axis running left to right across every
 * row (a single hour ruler header above them all, so times line up
 * vertically between rows like a schedule/roster). Each Employee Checkin
 * is a small rounded-square marker positioned by its time-of-day on that
 * date's row. Drag a marker to retime it, click one to type an exact time
 * precisely, click empty space on a row to add a missing punch, use the
 * small x on a marker to remove it. All of this edits the exact same
 * `checkin_rows` child table the server-side controller already works
 * against (load_checkins/on_submit/on_cancel, all unchanged) - this
 * widget is purely a different way to view and edit that same data. The
 * real grid is kept (hidden by default) as a fallback for typing values
 * directly. See "Edit as Table".
 *
 * IN/OUT is never set by hand here. attendance_sync._normalize_log_types()
 * completely ignores whatever log_type is stored on a checkin and
 * re-derives IN/OUT purely by chronological alternation within a day -
 * so a per-marker manual toggle would just drift out of sync with what
 * actually determines computed hours. Instead, every mutation
 * re-alternates the whole affected day's active (non-removed) punches by
 * time (earliest = IN, then alternating) and writes that back onto
 * log_type - so adding an earlier punch than the current IN correctly
 * makes the new one the IN and flips the old one to OUT, and every other
 * change ripples the same way, always matching what submit will actually
 * create/store.
 */

const CADJ_ROW_LABEL_WIDTH = 108; // px - keeps every row's track and the ruler header the same width; wide enough for the leave badge/action below the date
const CADJ_STAGGER_THRESHOLD_MINUTES = 20; // markers this close in time get nudged apart vertically
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

			frm.add_custom_button(__("Edit as Table"), () => cadj_toggle_table(frm));
		}

		if (frm.doc.amended_from) {
			frappe.msgprint(
				__(
					"This is an amendment - reload checkins for the period before editing, since the ones copied from the original may no longer be current."
				)
			);
		}

		// The Remove flag (toggled via the marker's x, or the table's own
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
		// appear, then re-renders again once it resolves to fill in badges.
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
// as a badge per day in cadj_render_timeline() below.
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
				frm.__cadj_leave_info_key = null; // force a refetch so the new badge shows
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
// IN/OUT auto-alternation - every mutation re-derives log_type for a
// whole day's active (non-removed) rows by pure chronological order,
// matching attendance_sync._normalize_log_types() exactly. This is what
// makes adding an earlier punch than the current IN correctly turn the
// new one into the IN and flip the old one to OUT, and ripples the same
// way through every later change on that day.
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

function cadj_realternate_day(day_rows) {
	const active = day_rows
		.filter((row) => !row.remove)
		.slice()
		.sort((a, b) => cadj_parse_datetime(a.time) - cadj_parse_datetime(b.time));

	let expected = "IN";
	for (const row of active) {
		if (row.log_type !== expected) {
			frappe.model.set_value(row.doctype, row.name, "log_type", expected);
		}
		expected = expected === "IN" ? "OUT" : "IN";
	}
}

function cadj_realternate_all_days(frm) {
	const by_day = cadj_rows_by_day(frm);
	Object.values(by_day).forEach(cadj_realternate_day);
}

function cadj_realternate_affected_days(frm, ...date_keys) {
	const by_day = cadj_rows_by_day(frm);
	const unique_keys = [...new Set(date_keys)];
	unique_keys.forEach((key) => {
		if (by_day[key]) cadj_realternate_day(by_day[key]);
	});
}

function cadj_after_mutation(frm, ...affected_date_keys) {
	cadj_realternate_affected_days(frm, ...affected_date_keys);
	frm.dirty();
	cadj_render_timeline(frm);
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
		.cadj-roster-row { display: flex; align-items: center; margin-bottom: 10px; }
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
		.cadj-leave-badge {
			font-size: 9px;
			font-weight: 500;
			padding: 1px 6px;
			border-radius: 8px;
			margin-top: 3px;
			white-space: nowrap;
			max-width: ${CADJ_ROW_LABEL_WIDTH}px;
			overflow: hidden;
			text-overflow: ellipsis;
		}
		.cadj-leave-badge.cadj-leave-full { background: #e6f4ea; color: #1e7e34; border: 1px solid #b7dfc0; }
		.cadj-leave-badge.cadj-leave-half { background: #fff8e1; color: #8a6d00; border: 1px solid #f0dfa0; }
		.cadj-leave-badge.cadj-leave-pending { background: #f1f1f1; color: #666; border-style: dashed; }
		.cadj-leave-action {
			display: none;
			font-size: 10px;
			color: var(--text-muted);
			cursor: pointer;
			margin-top: 3px;
			text-decoration: underline;
		}
		.cadj-roster-row:hover .cadj-leave-action { display: inline-block; }
		.cadj-track.cadj-track-leave { background: #eef7ee; }
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
			height: 34px;
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
			top: 9px;
			font-size: 11px;
			color: var(--text-muted);
			pointer-events: none;
		}
		.cadj-pin {
			position: absolute;
			top: 5px;
			width: 16px;
			height: 16px;
			border-radius: 4px;
			transform: translateX(-50%);
			cursor: grab;
			border: 2px solid #fff;
			box-shadow: 0 0 0 1px rgba(0,0,0,0.25);
			z-index: 2;
		}
		.cadj-pin.cadj-dragging { cursor: grabbing; z-index: 6; }
		.cadj-pin.cadj-in { background: #2e8b57; }
		.cadj-pin.cadj-out { background: #c0392b; }
		.cadj-pin.cadj-new { border-style: dashed; border-color: #2e8b57; }
		.cadj-pin.cadj-removed { background: #999 !important; opacity: 0.5; }
		.cadj-pin.cadj-corrected { box-shadow: 0 0 0 2px #d4a017; }
		.cadj-pin-label {
			position: absolute;
			top: 20px;
			left: 50%;
			transform: translateX(-50%);
			font-size: 10px;
			white-space: nowrap;
			color: var(--text-muted);
			pointer-events: none;
		}
		.cadj-pin-remove {
			position: absolute;
			top: -8px;
			right: -8px;
			width: 14px;
			height: 14px;
			line-height: 12px;
			text-align: center;
			font-size: 11px;
			border-radius: 50%;
			background: #fff;
			border: 1px solid var(--border-color, #d1d8dd);
			display: none;
			cursor: pointer;
			z-index: 3;
		}
		.cadj-pin:hover .cadj-pin-remove { display: block; }
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

	cadj_date_range(frm.doc.from_date, frm.doc.to_date).forEach((day) => {
		const key = cadj_date_key(day);
		const day_rows = (by_day[key] || [])
			.slice()
			.sort((a, b) => cadj_parse_datetime(a.time || a.original_time) - cadj_parse_datetime(b.time || b.original_time));

		const leave_here = (frm.__cadj_leave_info || {})[key];
		const is_full_leave_day = !!(leave_here && !leave_here.half_day);

		const $row = $(`<div class="cadj-roster-row"></div>`).appendTo($roster);
		const $label = $(`<div class="cadj-row-label"></div>`).appendTo($row);
		$label.append(`<div>${frappe.datetime.str_to_user(key, false, true)}</div>`);

		if (leave_here) {
			const badge_variant = leave_here.half_day ? "cadj-leave-half" : "cadj-leave-full";
			const pending_variant = leave_here.status && leave_here.status !== "Approved" ? "cadj-leave-pending" : "";
			const badge_text = `${leave_here.half_day ? __("Half Day") : __("On Leave")}: ${frappe.utils.escape_html(leave_here.leave_type)}`;
			$label.append(
				`<div class="cadj-leave-badge ${badge_variant} ${pending_variant}" title="${frappe.utils.escape_html(leave_here.leave_application)} (${frappe.utils.escape_html(leave_here.status)})">${badge_text}</div>`
			);
		}

		if (!readonly) {
			const $leave_action = $(
				`<div class="cadj-leave-action">${leave_here ? __("+ another leave") : __("+ leave")}</div>`
			).appendTo($label);
			$leave_action.on("click", (event) => {
				event.stopPropagation();
				cadj_open_leave_dialog(frm, key);
			});
		}

		const $track = $(
			`<div class="cadj-track ${readonly ? "cadj-readonly" : ""} ${is_full_leave_day ? "cadj-track-leave" : ""}"></div>`
		).appendTo($row);

		[6, 12, 18].forEach((hour) => {
			$track.append(`<div class="cadj-hour-line" style="left:${(hour / 24) * 100}%"></div>`);
		});

		if (!day_rows.length) {
			const hint = is_full_leave_day
				? __("On leave - no clocking expected")
				: readonly
				? ""
				: __("Click to add a punch");
			$track.append(`<span class="cadj-empty-hint">${hint}</span>`);
		}

		let last_minutes = null;
		let stagger_toggle = false;

		day_rows.forEach((row) => {
			const time_obj = cadj_parse_datetime(row.time || row.original_time);
			const minutes = cadj_minutes_of_day(time_obj);
			const left_pct = (minutes / 1440) * 100;

			if (last_minutes !== null && Math.abs(minutes - last_minutes) < CADJ_STAGGER_THRESHOLD_MINUTES) {
				stagger_toggle = !stagger_toggle;
			} else {
				stagger_toggle = false;
			}
			last_minutes = minutes;
			const top_offset = stagger_toggle ? 19 : 5;

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
			]
				.filter(Boolean)
				.join(" ");

			const $pin = $(`
				<div class="${classes}" style="left:${left_pct}%; top:${top_offset}px;" title="${__("Drag to retime, click to type an exact time")}">
					<span class="cadj-pin-label">${row.log_type || ""} ${cadj_pad(time_obj.getHours())}:${cadj_pad(time_obj.getMinutes())}</span>
					${readonly ? "" : `<span class="cadj-pin-remove">&times;</span>`}
				</div>
			`).appendTo($track);

			if (!readonly) {
				cadj_wire_pin(frm, $pin, $track, row, time_obj, key);
			}
		});

		if (!readonly) {
			cadj_wire_track(frm, $track, day, key);
		}
	});
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
		cadj_after_mutation(frm, day_key);
	});

	$pin.on("pointerdown", (event) => {
		if ($(event.target).hasClass("cadj-pin-remove")) return;
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
			$pin.find(".cadj-pin-label").text(
				`${row.log_type || ""} ${cadj_pad(Math.floor(current_minutes / 60))}:${cadj_pad(current_minutes % 60)}`
			);
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
			cadj_after_mutation(frm, day_key);
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
		cadj_after_mutation(frm, day_key);
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
		if ($(event.target).closest(".cadj-pin").length) return; // handled by the pin itself

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
		cadj_after_mutation(frm, day_key);
	});
}
