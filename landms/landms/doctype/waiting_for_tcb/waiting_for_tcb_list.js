// Copyright (c) 2026, Aakvatech Limited and contributors
// For license information, please see license.txt

frappe.listview_settings["Waiting for TCB"] = {
  add_fields: ["status", "action"],
  filters: [["status", "in", ["Waiting", "Needs a Person"]]],
  get_indicator(doc) {
    const colour = {
      Waiting: "orange",
      "Needs a Person": "red",
      Done: "green",
      Dropped: "gray",
    }[doc.status];
    return [__(doc.status), colour || "gray", "status,=," + doc.status];
  },
};
