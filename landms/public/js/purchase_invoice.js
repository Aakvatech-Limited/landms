frappe.ui.form.on("Purchase Invoice", {
  onload(frm) {
    if (!frm.doc.__islocal) return;
    const la = frappe.flags.new_pi_land_acquisition;
    if (!la) return;
    delete frappe.flags.new_pi_land_acquisition;
    frm._land_acquisition = la;
    (frm.doc.items || []).forEach((item) => {
      frappe.model.set_value(item.doctype, item.name, "land_acquisition", la);
    });
  },
});

frappe.ui.form.on("Purchase Invoice Item", {
  item_code(frm, cdt, cdn) {
    if (frm._land_acquisition) {
      const row = frappe.get_doc(cdt, cdn);
      if (!row.land_acquisition) {
        frappe.model.set_value(
          cdt,
          cdn,
          "land_acquisition",
          frm._land_acquisition
        );
      }
    }
  },
});
