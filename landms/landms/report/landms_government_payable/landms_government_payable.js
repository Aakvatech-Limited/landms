frappe.query_reports["LMS Government Payable"] = {
	filters: [
		{
			fieldname: "status",
			label: "Fee Status",
			fieldtype: "Select",
			options: "All\nPosted\nPending",
			default: "All"
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

		if (column.fieldname === "fee_status") {
			const status = data.fee_status;
			const style = status === "Posted"
				? "background:#e6fcf0;color:#1f7a3f;"
				: "background:#fff5f5;color:#c92a2a;";
			return `<span style="display:inline-block;padding:2px 10px;border-radius:12px;font-weight:600;${style}">${formatted}</span>`;
		}

		if (column.fieldname === "government_fee_withheld") {
			const color = data.fee_status === "Posted" ? "#2f9e44" : "#e03131";
			return `<span style="font-weight:700;color:${color};">${formatted}</span>`;
		}

		if (column.fieldname === "government_share_percent") {
			return `<span style="font-weight:600;color:#f08c00;">${formatted}</span>`;
		}

		return formatted;
	}
};
