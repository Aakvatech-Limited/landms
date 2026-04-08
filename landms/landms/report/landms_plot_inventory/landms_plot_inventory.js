frappe.query_reports["LMS Plot Inventory"] = {
	filters: [
		{
			fieldname: "land_acquisition",
			label: "Land Acquisition",
			fieldtype: "Link",
			options: "Land Acquisition"
		},
		{
			fieldname: "plot_type",
			label: "Plot Type",
			fieldtype: "Select",
			options: "\nResidential\nCommercial\nMixed-Use"
		},
		{
			fieldname: "status",
			label: "Status",
			fieldtype: "Select",
			options: "\nAvailable\nPending Fee\nPending Advance\nReserved\nReady for Handover\nDelivered\nTitle Closed"
		}
	],
	formatter: function (value, row, column, data, default_formatter) {
		let formatted = default_formatter(value, row, column, data);

		if (!data) {
			return formatted;
		}

		if (column.fieldname === "status") {
			const styles = {
				"Available": "background:#e6fcf0;color:#1f7a3f;",
				"Pending Fee": "background:#fff4e6;color:#9c5c00;",
				"Pending Advance": "background:#fff9db;color:#8f5a00;",
				"Reserved": "background:#fff4e6;color:#9c5c00;",
				"Ready for Handover": "background:#e3fafc;color:#0b7285;",
				"Delivered": "background:#e7f5ff;color:#0b5cab;",
				"Title Closed": "background:#f3e8ff;color:#6b21a8;"
			};
			const style = styles[data.status] || "background:#f1f3f5;color:#495057;";
			return `<span style="display:inline-block;padding:2px 10px;border-radius:12px;font-weight:600;${style}">${formatted}</span>`;
		}

		if (column.fieldname === "selling_price") {
			return `<span style="font-weight:700;color:#1f6feb;">${formatted}</span>`;
		}

		if (column.fieldname === "margin" || column.fieldname === "margin_pct") {
			const margin = Number(data.margin || 0);
			const color = margin >= 0 ? "#2f9e44" : "#c92a2a";
			return `<span style="font-weight:700;color:${color};">${formatted}</span>`;
		}

		return formatted;
	}
};
