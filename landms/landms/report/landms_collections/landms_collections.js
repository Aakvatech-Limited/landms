frappe.query_reports["LMS Collections"] = {
	filters: [
		{
			fieldname: "customer",
			label: "Customer",
			fieldtype: "Link",
			options: "Customer"
		},
		{
			fieldname: "from_date",
			label: "From Date",
			fieldtype: "Date"
		},
		{
			fieldname: "to_date",
			label: "To Date",
			fieldtype: "Date",
			default: frappe.datetime.get_today()
		}
	],
	formatter: function (value, row, column, data, default_formatter) {
		let formatted = default_formatter(value, row, column, data);

		if (!data) {
			return formatted;
		}

		if (column.fieldname === "customer") {
			return `<span style="font-weight:600;color:#2f4f4f;">${formatted}</span>`;
		}

		if (column.fieldname === "total_paid") {
			return `<span style="font-weight:700;color:#2f9e44;">${formatted}</span>`;
		}

		if (column.fieldname === "total_outstanding") {
			return `<span style="font-weight:700;color:#e03131;">${formatted}</span>`;
		}

		if (column.fieldname === "collection_rate") {
			const rate = Number(data.collection_rate || 0);
			const color = rate >= 70 ? "#2f9e44" : rate >= 40 ? "#f08c00" : "#e03131";
			return `<span style="font-weight:700;color:${color};">${formatted}</span>`;
		}

		return formatted;
	}
};
