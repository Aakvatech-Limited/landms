frappe.query_reports["LMS Revenue Recognition"] = {
	filters: [
		{
			fieldname: "from_date",
			label: "From Date",
			fieldtype: "Date",
			default: frappe.datetime.add_months(frappe.datetime.get_today(), -1)
		},
		{
			fieldname: "to_date",
			label: "To Date",
			fieldtype: "Date",
			default: frappe.datetime.get_today()
		},
		{
			fieldname: "customer",
			label: "Customer",
			fieldtype: "Link",
			options: "Customer"
		}
	],
	formatter: function (value, row, column, data, default_formatter) {
		let formatted = default_formatter(value, row, column, data);

		if (!data) {
			return formatted;
		}

		if (column.fieldname === "revenue") {
			return `<span style="font-weight:700;color:#1c7ed6;">${formatted}</span>`;
		}

		if (column.fieldname === "cogs") {
			return `<span style="font-weight:700;color:#e03131;">${formatted}</span>`;
		}

		if (column.fieldname === "gross_margin" || column.fieldname === "margin_pct") {
			const margin = Number(data.gross_margin || 0);
			const color = margin >= 0 ? "#2f9e44" : "#e03131";
			return `<span style="font-weight:700;color:${color};">${formatted}</span>`;
		}

		return formatted;
	}
};
