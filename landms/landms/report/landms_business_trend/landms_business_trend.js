frappe.query_reports["LMS Business Trend"] = {
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
			default: frappe.datetime.add_months(frappe.datetime.get_today(), -12),
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

		if (column.fieldname === "period") {
			return `<span style="font-weight:600;color:#2f4f4f;">${formatted}</span>`;
		}

		if (column.fieldname === "new_contract_value" || column.fieldname === "cash_collected") {
			return `<span style="font-weight:700;color:#1c7ed6;">${formatted}</span>`;
		}

		if (column.fieldname === "completed") {
			return `<span style="font-weight:700;color:#2f9e44;">${formatted}</span>`;
		}

		if (column.fieldname === "terminated") {
			return `<span style="font-weight:700;color:#e8590c;">${formatted}</span>`;
		}

		return formatted;
	}
};
