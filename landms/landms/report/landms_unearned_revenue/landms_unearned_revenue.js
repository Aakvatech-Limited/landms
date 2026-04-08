frappe.query_reports["LMS Unearned Revenue"] = {
	filters: [
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

		if (column.fieldname === "contract_status") {
			return `<span style="display:inline-block;padding:2px 10px;border-radius:12px;font-weight:600;background:#fff4e6;color:#9c5c00;">${formatted}</span>`;
		}

		if (column.fieldname === "total_paid") {
			return `<span style="font-weight:700;color:#f08c00;">${formatted}</span>`;
		}

		if (column.fieldname === "total_outstanding") {
			return `<span style="font-weight:700;color:#e03131;">${formatted}</span>`;
		}

		if (column.fieldname === "pct_collected") {
			const pct = Number(data.pct_collected || 0);
			const color = pct >= 60 ? "#2f9e44" : pct >= 30 ? "#f08c00" : "#e03131";
			return `<span style="font-weight:700;color:${color};">${formatted}</span>`;
		}

		return formatted;
	}
};
