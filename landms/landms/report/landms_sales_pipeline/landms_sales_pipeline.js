frappe.query_reports["LMS Sales Pipeline"] = {
	filters: [
		{
			fieldname: "contract_status",
			label: "Status",
			fieldtype: "Select",
			options: "\nOngoing\nCompleted"
		},
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

		if (column.fieldname === "contract_status") {
			const styles = {
				"Ongoing": "background:#fff4e6;color:#9c5c00;",
				"Completed": "background:#e6fcf0;color:#1f7a3f;",
				"Terminated": "background:#fff3bf;color:#8f5a00;",
				"Cancelled": "background:#fff5f5;color:#c92a2a;",
				"Draft": "background:#f1f3f5;color:#495057;"
			};
			const style = styles[data.contract_status] || styles.Draft;
			return `<span style="display:inline-block;padding:2px 10px;border-radius:12px;font-weight:600;${style}">${formatted}</span>`;
		}

		if (column.fieldname === "progress_pct") {
			const pct = Number(data.progress_pct || 0);
			const color = pct >= 90 ? "#2f9e44" : pct >= 50 ? "#f08c00" : "#e03131";
			return `<span style="font-weight:700;color:${color};">${formatted}</span>`;
		}

		if (column.fieldname === "total_outstanding") {
			return `<span style="font-weight:700;color:#e03131;">${formatted}</span>`;
		}

		if (column.fieldname === "total_paid") {
			return `<span style="font-weight:700;color:#2f9e44;">${formatted}</span>`;
		}

		if (column.fieldname === "installment_summary" && String(data.installment_summary || "").includes("overdue")) {
			return `<span style="font-weight:700;color:#c92a2a;">${formatted}</span>`;
		}

		return formatted;
	}
};
