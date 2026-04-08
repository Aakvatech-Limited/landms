frappe.query_reports["LMS Executive Dashboard"] = {
	filters: [],
	formatter: function (value, row, column, data, default_formatter) {
		let formatted = default_formatter(value, row, column, data);

		if (!data) {
			return formatted;
		}

		if (column.fieldname === "section") {
			return `<span style="font-weight: 600; color: #2f4f4f;">${formatted}</span>`;
		}

		if (column.fieldname === "kpi") {
			return `<span style="font-weight: 600;">${formatted}</span>`;
		}

		if (column.fieldname === "value" && data.section === "FINANCIALS") {
			return `<span style="font-weight: 700; color: #1f6feb;">${formatted}</span>`;
		}

		if (column.fieldname === "value" && data.section === "RATIOS") {
			return `<span style="font-weight: 700; color: #6b4e16;">${formatted}</span>`;
		}

		return formatted;
	}
};
