frappe.ui.form.on("Plot Handover", {
	refresh(frm) {
		set_contract_query(frm);
		if (frm.is_new()) {
			set_company_representative(frm);
		}
	},
	contract(frm) {
		set_contract_details(frm);
		if (frm.is_new()) {
			set_company_representative(frm);
		}
	},
});

function set_contract_query(frm) {
	frm.set_query("contract", function () {
		return {
			filters: {
				docstatus: 1,
				contract_status: "Completed",
			},
		};
	});
}

function set_company_representative(frm) {
	if (frm.doc.handed_over_by && frm.doc.handed_over_by_title) {
		return;
	}

	frappe.call({
		method: "landms.landms.doctype.plot_handover.plot_handover.get_logged_in_representative_details",
		callback: function (r) {
			const data = r.message || {};
			if (!frm.doc.handed_over_by && data.handed_over_by) {
				frm.set_value("handed_over_by", data.handed_over_by);
			}
			if (!frm.doc.handed_over_by_title && data.handed_over_by_title) {
				frm.set_value("handed_over_by_title", data.handed_over_by_title);
			}
		},
	});
}

function set_contract_details(frm) {
	if (!frm.doc.contract) {
		return;
	}

	frappe.call({
		method: "landms.landms.doctype.plot_handover.plot_handover.get_plot_handover_defaults",
		args: {
			contract: frm.doc.contract,
		},
		callback: function (r) {
			const data = r.message || {};
			const fields = [
				"customer",
				"plot",
				"acquisition_name",
				"land_acquisition",
				"contract_date",
				"selling_price",
			];

			fields.forEach((fieldname) => {
				if (Object.prototype.hasOwnProperty.call(data, fieldname)) {
					frm.set_value(fieldname, data[fieldname]);
				}
			});
		},
	});
}
