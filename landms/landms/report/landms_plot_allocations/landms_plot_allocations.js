frappe.query_reports["LandMS Plot Allocations"] = {
  filters: [
    {
      fieldname: "customer",
      label: "Customer",
      fieldtype: "Link",
      options: "Customer",
    },
    {
      fieldname: "land_acquisition",
      label: "Land Acquisition",
      fieldtype: "Link",
      options: "Land Acquisition",
    },
    {
      fieldname: "plot_type",
      label: "Plot Type",
      fieldtype: "Link",
      options: "Plot Type",
    },
    {
      fieldname: "status",
      label: "Status",
      fieldtype: "Select",
      options: "\nSubmitted\nPaid\nConverted\nExpired\nCancelled",
    },
    {
      fieldname: "from_date",
      label: "From Date",
      fieldtype: "Date",
    },
    {
      fieldname: "to_date",
      label: "To Date",
      fieldtype: "Date",
    },
  ],
};
