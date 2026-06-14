frappe.query_reports["LandMS Forfeitures"] = {
	filters: [
		{
			fieldname: "source_type",
			label: "Type",
			fieldtype: "Select",
			options: "\nTermination\nSales Order"
		},
		{
			fieldname: "customer",
			label: "Customer",
			fieldtype: "Link",
			options: "Customer"
		},
		{
			fieldname: "land_acquisition",
			label: "Land Acquisition",
			fieldtype: "Link",
			options: "Land Acquisition"
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
		if (column.fieldname === "forfeited") {
			return `<span style="font-weight:700;color:#2f9e44;">${formatted}</span>`;
		}
		if (column.fieldname === "refund_due" && Number(data.refund_due || 0) > 0) {
			return `<span style="font-weight:700;color:#e8590c;">${formatted}</span>`;
		}
		return formatted;
	}
};
