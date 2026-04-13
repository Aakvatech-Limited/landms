// Copyright (c) 2024, Aakvatech Limited and contributors
// For license information, please see license.txt

frappe.ui.form.on("TCB Control Number", {
	refresh(frm) {
		const declineable = ["Generated", "Registered", "Failed"];
		if (declineable.includes(frm.doc.status)) {
			frm.add_custom_button(__("Decline at TCB"), () => {
				frappe.confirm(
					__(
						"This will call the TCB Reference Decline URL and mark this control number as Declined. " +
						"Are you sure you want to decline <b>{0}</b>?",
						[frm.doc.name]
					),
					() => {
						frm.call({
							method: "decline_control_number",
							doc: frm.doc,
							freeze: true,
							freeze_message: __("Declining reference at TCB…"),
							callback(r) {
								const result = r.message || {};
								if (result.ok) {
									frappe.show_alert({
										message: result.message || __("Control number declined successfully."),
										indicator: "green",
									});
								} else {
									frappe.msgprint({
										title: __("TCB Decline Failed"),
										message: result.message || __("TCB decline call failed. Check TCB API Log for details."),
										indicator: "red",
									});
								}
								frm.refresh();
							},
						});
					}
				);
			}, __("Actions"));
		}
	},
});
