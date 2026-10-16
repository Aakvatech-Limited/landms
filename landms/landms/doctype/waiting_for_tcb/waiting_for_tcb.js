// Copyright (c) 2026, Aakvatech Limited and contributors
// For license information, please see license.txt

// Waiting for TCB: a TCB step that could not be done while TCB was down.
// Try now checks the step again and does it at TCB if still needed;
// Don't do this closes it with nothing sent.
frappe.ui.form.on("Waiting for TCB", {
  refresh(frm) {
    if (!["Waiting", "Needs a Person"].includes(frm.doc.status)) return;

    frm.add_custom_button(__("Try now"), () => {
      frappe.confirm(
        __(
          "Check this step again and, if it is still needed, do it at TCB now?"
        ),
        () =>
          frm.call({
            method: "try_now",
            doc: frm.doc,
            freeze: true,
            freeze_message: __("Asking TCB…"),
            callback(r) {
              const res = r.message || {};
              frappe.msgprint({
                title: __("Waiting for TCB"),
                message: res.message,
                indicator: res.ok ? "green" : "orange",
              });
              frm.reload_doc();
            },
          })
      );
    });

    frm.add_custom_button(__("Don't do this"), () => {
      frappe.prompt(
        {
          fieldname: "note",
          fieldtype: "Small Text",
          label: __("Why"),
          reqd: 1,
        },
        (values) =>
          frm.call({
            method: "stop",
            doc: frm.doc,
            args: { note: values.note },
            callback(r) {
              frappe.show_alert(
                { message: (r.message || {}).message, indicator: "orange" },
                6
              );
              frm.reload_doc();
            },
          }),
        __("Don't do this at TCB"),
        __("Stop it")
      );
    });
  },
});
