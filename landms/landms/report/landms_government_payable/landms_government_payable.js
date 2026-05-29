frappe.query_reports["LandMS Government Payable"] = {
	filters: [
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
		},
		{
			fieldname: "land_acquisition",
			label: "Land Acquisition",
			fieldtype: "Link",
			options: "Land Acquisition"
		}
	],
	formatter: function (value, row, column, data, default_formatter) {
		let formatted = default_formatter(value, row, column, data);
		if (!data) return formatted;

		if (column.fieldname === "govt_amount") {
			return `<span style="font-weight:700;color:#1971c2;">${formatted}</span>`;
		}

		return formatted;
	}
};
