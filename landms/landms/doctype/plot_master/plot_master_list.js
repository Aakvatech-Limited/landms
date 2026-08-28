frappe.listview_settings["Plot Master"] = {
  add_fields: ["status", "plot_number", "plot_type", "land_acquisition"],

  get_indicator(doc) {
    if (doc.docstatus === 0) {
      return [__("Draft"), "gray", "docstatus,=,0"];
    }
    if (doc.docstatus === 2) {
      return [__("Cancelled"), "red", "docstatus,=,2"];
    }
    const map = {
      Available: ["Available", "green", "status,=,Available"],
      "Pending Fee": ["Pending Fee", "orange", "status,=,Pending Fee"],
      "Pending Advance": [
        "Pending Advance",
        "yellow",
        "status,=,Pending Advance",
      ],
      Reserved: ["Reserved", "orange", "status,=,Reserved"],
      "Ready for Handover": [
        "Ready for Handover",
        "cyan",
        "status,=,Ready for Handover",
      ],
      Delivered: ["Delivered", "blue", "status,=,Delivered"],
      "Title Closed": ["Title Closed", "darkgreen", "status,=,Title Closed"],
    };
    return (
      map[doc.status] || [
        doc.status || "Unknown",
        "gray",
        `status,=,${doc.status || ""}`,
      ]
    );
  },
};
