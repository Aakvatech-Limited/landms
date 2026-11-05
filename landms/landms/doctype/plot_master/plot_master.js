frappe.ui.form.on("Plot Master", {
  setup(frm) {
    // Only Approved/Subdivided LAs are eligible
    frm.set_query("land_acquisition", () => ({
      filters: { status: ["in", ["Approved", "Subdivided"]] },
    }));

    // Only show plot types that have a selling rate on the current LA
    frm.set_query("plot_type", () => {
      if (!frm.doc.land_acquisition) return {};
      return {
        query:
          "landms.landms.doctype.plot_master.plot_master.get_plot_types_for_la",
        filters: { land_acquisition: frm.doc.land_acquisition },
      };
    });
  },

  refresh(frm) {
    const colors = {
      Available: "green",
      "Pending Fee": "orange",
      "Pending Advance": "yellow",
      Reserved: "orange",
      "Ready for Handover": "cyan",
      Delivered: "blue",
      "Title Closed": "darkgreen",
    };
    if (frm.doc.docstatus === 1 && frm.doc.status) {
      frm.page.set_indicator(frm.doc.status, colors[frm.doc.status] || "gray");
    }
    show_customer_history(frm);
  },

  land_acquisition(frm) {
    if (!frm.doc.land_acquisition) {
      clear_financials(frm);
      return;
    }
    frappe.db
      .get_doc("Land Acquisition", frm.doc.land_acquisition)
      .then((la) => {
        if (!["Approved", "Subdivided"].includes(la.status)) {
          frappe.msgprint({
            title: __("Invalid Selection"),
            message: __(
              "Land Acquisition {0} must be Approved or Subdivided (current: {1}).",
              [frm.doc.land_acquisition, la.status]
            ),
            indicator: "red",
          });
          frm.set_value("land_acquisition", "");
          clear_financials(frm);
          return;
        }
        frm.set_value("acquisition_name", la.acquisition_name || "");
        recalculate(frm, la);
        frappe.show_alert({
          message: __("Loaded pricing defaults from {0}.", [la.name]),
          indicator: "blue",
        });
      });
  },

  plot_type(frm) {
    if (frm.doc.land_acquisition) {
      frappe.db
        .get_doc("Land Acquisition", frm.doc.land_acquisition)
        .then((la) => recalculate(frm, la));
    } else {
      frm.set_value("selling_price_per_sqm", 0);
      frm.set_value("selling_price", 0);
    }
  },

  plot_size_sqm(frm) {
    if (frm.doc.land_acquisition && frm.doc.plot_size_sqm) {
      frappe.db
        .get_doc("Land Acquisition", frm.doc.land_acquisition)
        .then((la) => recalculate(frm, la));
    } else {
      frm.set_value("allocated_cost", 0);
      frm.set_value("selling_price", 0);
    }
  },
});

function clear_financials(frm) {
  frm.set_value("acquisition_name", "");
  frm.set_value("cost_per_sqm", 0);
  frm.set_value("selling_price_per_sqm", 0);
  frm.set_value("allocated_cost", 0);
  frm.set_value("selling_price", 0);
}

function recalculate(frm, la) {
  if (!la || !flt(la.total_area_sqm)) {
    frm.set_value("cost_per_sqm", 0);
    frm.set_value(
      "selling_price_per_sqm",
      selling_rate(la, frm.doc.plot_type)
    );
    frm.set_value("allocated_cost", 0);
    frm.set_value("selling_price", 0);
    return;
  }

  const cost_per_sqm =
    flt(la.cost_per_sqm_tzs) ||
    flt(la.acquisition_cost_tzs) / flt(la.total_area_sqm);
  const sell_per_sqm = selling_rate(la, frm.doc.plot_type);
  const plot_sqm = flt(frm.doc.plot_size_sqm);

  frm.set_value("cost_per_sqm", cost_per_sqm);
  frm.set_value("selling_price_per_sqm", sell_per_sqm);
  frm.set_value("allocated_cost", cost_per_sqm * plot_sqm);
  frm.set_value("selling_price", sell_per_sqm * plot_sqm);

  frm.set_df_property(
    "selling_price",
    "description",
    sell_per_sqm
      ? `${sell_per_sqm.toLocaleString()} TZS × ${plot_sqm.toLocaleString()} sqm = ${(
          sell_per_sqm * plot_sqm
        ).toLocaleString()} TZS`
      : __(
          "Set the {0} on Land Acquisition {1} to auto-fill this plot's selling price.",
          [rate_label(frm.doc.plot_type), frm.doc.land_acquisition || ""]
        )
  );
}

function selling_rate(la, plot_type) {
  if (!la || !plot_type) return 0;
  const rates = la.plot_type_rates || [];
  const row = rates.find((r) => r.plot_type === plot_type);
  return row ? flt(row.selling_price_per_sqm) : 0;
}

function rate_label(plot_type) {
  return plot_type
    ? `${plot_type} selling rate per sqm`
    : "plot-type selling rate";
}

// Customer History tab: every customer who took the plot, newest first (read-only, stored nowhere).
function show_customer_history(frm) {
  const wrapper = frm.get_field("customer_history_html").$wrapper;
  if (frm.is_new()) {
    wrapper.empty();
    return;
  }
  frappe
    .call({
      method:
        "landms.landms.doctype.plot_master.customer_history.get_customer_history",
      args: { plot: frm.doc.name },
    })
    .then((r) => {
      const lines = r.message || [];
      if (!lines.length) {
        wrapper.html(`<p class="text-muted">${__("No customer yet.")}</p>`);
        return;
      }
      const link = (doctype, name) =>
        `<a href="/app/${frappe.router.slug(doctype)}/${encodeURIComponent(
          name
        )}">${frappe.utils.escape_html(name)}</a>`;
      const money = (amount) => format_number(amount, null, 0);
      const target = (m) => (m.name ? ` ${link(m.doctype, m.name)}` : "");
      const move = (m) =>
        m.direction === "out"
          ? `${__("Moved")} ${money(m.amount)} →${target(m)}`
          : `${__("Received")} ${money(m.amount)} ←${target(m)}`;
      const rows = lines
        .map(
          (l) => `<tr>
            <td>${
              l.current
                ? `<span class="indicator-pill green">${__("Current")}</span>`
                : ""
            }</td>
            <td>${frappe.utils.escape_html(l.customer)}</td>
            <td>${l.from ? frappe.datetime.str_to_user(l.from) : ""}</td>
            <td>${__(l.outcome)}${l.moves
            .map((m) => `<br>${move(m)}`)
            .join("")}</td>
            <td>${l.documents
              .map(([dt, name]) => link(dt, name))
              .join(" · ")}</td>
          </tr>`
        )
        .join("");
      wrapper.html(`<table class="table table-bordered table-sm">
        <thead><tr><th></th><th>${__("Customer")}</th><th>${__("From")}</th>
        <th>${__("What happened")}</th><th>${__("Documents")}</th></tr></thead>
        <tbody>${rows}</tbody></table>`);
    });
}
