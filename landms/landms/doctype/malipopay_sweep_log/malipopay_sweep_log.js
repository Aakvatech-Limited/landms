frappe.ui.form.on("Malipopay Sweep Log", {
  refresh(frm) {
    _render_status_indicator(frm);

    const status = frm.doc.status;
    clearTimeout(frm.__sweep_poll);

    if (
      !frm.is_new() &&
      ["Queued", "Completed", "Failed", "Stopped"].includes(status)
    ) {
      frm
        .add_custom_button(__("Run Sweep Now"), () => {
          frappe.confirm(
            __(
              "Sweep every live Malipopay collection and book anything they hold that we have not? Previous rows on this log are cleared."
            ),
            () => {
              frm.call({
                method: "run",
                args: { name: frm.docname },
                freeze: true,
                freeze_message: __("Queueing sweep…"),
                callback() {
                  frm.reload_doc();
                },
              });
            }
          );
        })
        .addClass("btn-primary");
    }

    if (
      !frm.is_new() &&
      ["Running", "Queued"].includes(status) &&
      !frm.doc.stop_requested
    ) {
      frm
        .add_custom_button(__("Stop Sweep"), () => {
          frappe.confirm(
            __(
              "Request stop? It will stop after the collection it is working on."
            ),
            () => {
              frm.call({
                method: "request_stop",
                args: { name: frm.docname },
                freeze: true,
                freeze_message: __("Requesting stop…"),
                callback() {
                  frm.reload_doc();
                },
              });
            }
          );
        })
        .addClass("btn-warning");
    }

    if (!frm.is_new() && ["Running", "Queued"].includes(status)) {
      frm.__sweep_poll = setTimeout(() => {
        if (!frm.is_dirty()) {
          frm.reload_doc();
        }
      }, 5000);
    }
  },
});

function _render_status_indicator(frm) {
  const colors = {
    Queued: "gray",
    Running: "blue",
    Completed: "green",
    Failed: "red",
    Stopped: "orange",
  };
  let label = __(frm.doc.status);
  let color = colors[frm.doc.status] || "gray";
  // A completed run that found divergences is not a clean run — say so.
  if (
    frm.doc.status === "Completed" &&
    cint(frm.doc.divergences_flagged) > 0
  ) {
    label = __("Completed — {0} divergence(s)", [frm.doc.divergences_flagged]);
    color = "yellow";
  }
  frm.page.set_indicator(label, color);

  setTimeout(() => {
    const grid = frm.fields_dict.rows && frm.fields_dict.rows.grid;
    if (!grid) return;
    const rowColors = {
      Clean: { bg: "#f1f3f5", color: "#495057" },
      Booked: { bg: "#d3f9d8", color: "#1b7a2e" },
      "Orphan Adopted": { bg: "#e7f0fd", color: "#1c4e80" },
      "Divergence Flagged": { bg: "#fff3cd", color: "#856404" },
      "Skipped (Hold)": { bg: "#f8f0fc", color: "#6741d9" },
      Error: { bg: "#fde8e8", color: "#c0392b" },
    };
    grid.wrapper.find(".grid-row").each(function () {
      const row_name = $(this).attr("data-name");
      if (!row_name) return;
      const row_doc = frappe.get_doc("Malipopay Sweep Log Row", row_name);
      if (!row_doc) return;
      const cfg = rowColors[row_doc.action];
      if (!cfg) return;
      $(this).css({
        "background-color": cfg.bg,
        "border-left": `4px solid ${cfg.color}`,
      });
      $(this)
        .find('[data-fieldname="action"]')
        .css({ color: cfg.color, "font-weight": "700" });
    });
  }, 300);
}
