app_name = "is_attendance"
app_title = "Isambane Attendance"
app_publisher = "BuFf0k"
app_description = "Attendance Management and Integration Tool"
app_email = "buff0k@gmail.com"
app_license = "mit"
app_logo_url = "/assets/is_attendance/images/is-logo.svg"
app_home = "/desk/attendance"
required_apps = ["frappe/hrms"]
add_to_apps_screen = [
	{
		"name": app_name,
		"logo": "/assets/is_attendance/images/is-logo.svg",
		"title": app_title,
		"route": app_home,
	}
]
fixtures = [
	{"dt": "Custom Field", "filters": [["name", "in", [
		"Employee Checkin-isa_branch",
	]]]},
]
doc_events = {
	"Employee Checkin": {
		"after_insert": "is_attendance.controllers.attendance_sync.on_employee_checkin",
	},
	"Leave Application": {
		"on_submit": "is_attendance.controllers.attendance_sync.on_leave_application_change",
		"on_cancel": "is_attendance.controllers.attendance_sync.on_leave_application_change",
		"on_update_after_submit": "is_attendance.controllers.attendance_sync.on_leave_application_change",
	},
}
scheduler_events = {
	"daily": [
		"is_attendance.controllers.attendance_sync.enqueue_daily_sync",
	],
}