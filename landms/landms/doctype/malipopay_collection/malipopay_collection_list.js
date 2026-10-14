// Copyright (c) 2026, Aakvatech Limited and contributors
// For license information, please see license.txt

frappe.listview_settings["Malipopay Collection"] = {
  add_fields: ["state", "provider_fully_paid", "needs_attention"],
  get_indicator(doc) {
    if (doc.state === "Active" && doc.provider_fully_paid) {
      return [__("Paid"), "green", "provider_fully_paid,=,1"];
    }
    const colours = {
      Active: "blue",
      "Pending Create": "orange",
      Creating: "orange",
      "Create Failed": "red",
      "Suspend Requested": "orange",
      Suspended: "yellow",
      "Resume Requested": "orange",
      "Kill Requested": "orange",
      "Needs Attention": "red",
      Dead: "gray",
    };
    return [
      __(doc.state),
      colours[doc.state] || "gray",
      "state,=," + doc.state,
    ];
  },
};
