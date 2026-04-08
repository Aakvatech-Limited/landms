frappe.query_reports["LMS Revenue Report"] = {
	filters: [
		{
			fieldname: "grouping",
			label: "Grouping",
			fieldtype: "Select",
			options: "Monthly\nWeekly",
			default: "Monthly",
			reqd: 1
		},
		{
			fieldname: "from_date",
			label: "From Date",
			fieldtype: "Date",
			default: frappe.datetime.add_months(frappe.datetime.get_today(), -6),
			reqd: 1
		},
		{
			fieldname: "to_date",
			label: "To Date",
			fieldtype: "Date",
			default: frappe.datetime.get_today(),
			reqd: 1
		}
	],
	formatter: function (value, row, column, data, default_formatter) {
		let formatted = default_formatter(value, row, column, data);

		if (!data) {
			return formatted;
		}

		if (data.period === "TOTAL") {
			formatted = `<span style="font-weight:700;color:#1f2937;">${formatted}</span>`;
		}

		if (column.fieldname === "govt_fee") {
			return `<span style="font-weight:700;color:#f08c00;">${formatted}</span>`;
		}

		if (column.fieldname === "net_revenue") {
			return `<span style="font-weight:700;color:#2f9e44;">${formatted}</span>`;
		}

		if (column.fieldname === "total_collected") {
			return `<span style="font-weight:700;color:#1c7ed6;">${formatted}</span>`;
		}

		return formatted;
	}
};
