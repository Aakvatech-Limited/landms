// Copyright (c) 2026, Aakvatech Limited and contributors
// For license information, please see license.txt

// Buttons mirror TCB Control Number: Retry Registration while the bill is not
// registered, a decline (here "Expire at Malipopay") while it is unpaid, and
// nothing once it is paid in full or dead. Report to Malipopay resends what
// Malipopay must know about money taken outside it (malipopay_external).
const MP_RETRYABLE = ["Create Failed", "Pending Create", "Creating"];
const MP_EXPIRABLE = [
  "Active",
  "Needs Attention",
  "Kill Requested",
  "Create Failed",
  "Pending Create",
];
const MP_REPORTABLE = ["Active", "Needs Attention"];

frappe.ui.form.on("Malipopay Collection", {
  refresh(frm) {
    if (frm.is_new()) return;
    const paid = !!frm.doc.provider_fully_paid;
    mp_show_status(frm, paid);

    if (MP_RETRYABLE.includes(frm.doc.state)) {
      mp_button(
        frm,
        __("Retry Registration"),
        "retry_registration",
        __(
          "This will ask Malipopay whether a bill already exists for <b>{0}</b> and link it, " +
            "or create one only if Malipopay confirms there is none. Continue?",
          [frm.doc.control_number]
        ),
        __("Registering with Malipopay…"),
        __("Malipopay Registration Failed")
      );
    }

    if (MP_EXPIRABLE.includes(frm.doc.state) && !paid) {
      mp_button(
        frm,
        __("Expire at Malipopay"),
        "expire_at_malipopay",
        __(
          "This closes Malipopay bill <b>{0}</b> so it can no longer take money. It cannot be undone.<br><br>" +
            "Only the Malipopay bill is closed — the TCB number <b>{1}</b> stays live. To stop the number " +
            "everywhere, use Decline at TCB on the TCB Control Number instead. Continue?",
          [
            frm.doc.ml_reference || frm.doc.control_number,
            frm.doc.control_number,
          ]
        ),
        __("Expiring at Malipopay…"),
        __("Malipopay Expire Failed")
      );
    }

    if (frm.doc.ml_reference) {
      mp_button(
        frm,
        __("Check Payments"),
        "check_payments",
        __(
          "Read bill <b>{0}</b> from Malipopay now and book any payment not yet recorded? " +
            "A payment already booked is never booked twice.",
          [frm.doc.ml_reference]
        ),
        __("Reading payments from Malipopay…"),
        __("Could Not Check Payments")
      );
    }

    if (frm.doc.ml_reference && MP_REPORTABLE.includes(frm.doc.state)) {
      mp_button(
        frm,
        __("Report to Malipopay"),
        "report_to_malipopay",
        __(
          "Tell Malipopay now about payments on <b>{0}</b> made outside Malipopay (TCB, counter), " +
            "and resend any message Malipopay refused or did not answer? " +
            "Safe to repeat — Malipopay is only sent what it does not already have.",
          [frm.doc.sales_order]
        ),
        __("Reporting to Malipopay…"),
        __("Malipopay Report Not Complete")
      );
    }

    if (
      frm.doc.control_number &&
      frappe.model.can_read("TCB Control Number")
    ) {
      frm.add_custom_button(
        __("TCB Control Number"),
        () =>
          frappe.set_route(
            "Form",
            "TCB Control Number",
            frm.doc.control_number
          ),
        __("View")
      );
    }
    if (frm.doc.sales_order) {
      frm.add_custom_button(
        __("Sales Order"),
        () => frappe.set_route("Form", "Sales Order", frm.doc.sales_order),
        __("View")
      );
    }
  },
});

function mp_button(frm, label, method, question, freeze_message, fail_title) {
  frm.add_custom_button(
    label,
    () => {
      frappe.confirm(question, () => {
        frm.call({
          method: method,
          doc: frm.doc,
          freeze: true,
          freeze_message: freeze_message,
          callback(r) {
            const result = r.message || {};
            if (result.ok) {
              frappe.show_alert(
                { message: result.message, indicator: "green" },
                8
              );
            } else {
              frappe.msgprint({
                title: fail_title,
                message: result.message,
                indicator: "red",
              });
            }
            frm.reload_doc();
          },
        });
      });
    },
    __("Actions")
  );
}

// One line at the top saying what this state means and what to do about it.
function mp_show_status(frm, paid) {
  const d = frm.doc;
  const ml = d.ml_reference || __("(no Malipopay reference yet)");
  let text, colour;
  if (d.state === "Active" && paid) {
    [text, colour] = [
      __("Paid in full at Malipopay ({0}). Nothing to do.", [ml]),
      "green",
    ];
  } else if (d.state === "Active") {
    [text, colour] = [
      __(
        "Live at Malipopay as {0} — the customer can pay by bank or mobile money.",
        [ml]
      ),
      "blue",
    ];
  } else if (d.state === "Pending Create" || d.state === "Creating") {
    [text, colour] = [
      __(
        "Being registered with Malipopay. If this does not change within a few minutes, use Retry Registration."
      ),
      "orange",
    ];
  } else if (d.state === "Create Failed") {
    [text, colour] = [
      __(
        "Not registered with Malipopay — the customer can only pay through TCB. Use Retry Registration."
      ),
      "red",
    ];
  } else if (d.state === "Kill Requested") {
    [text, colour] = [
      __(
        "The order was stopped but Malipopay has not yet expired bill {0}, so it can still take money. " +
          "Retried every hour; use Expire at Malipopay to try now.",
        [ml]
      ),
      "orange",
    ];
  } else if (d.state === "Needs Attention") {
    [text, colour] = [
      __("Needs attention: {0}", [
        frappe.utils.escape_html(d.attention_reason || ""),
      ]),
      "red",
    ];
  } else if (d.state === "Dead") {
    [text, colour] = [
      __("Closed — this bill can no longer take money."),
      "gray",
    ];
  }
  if (
    d.needs_attention &&
    d.state !== "Needs Attention" &&
    d.attention_reason
  ) {
    text =
      (text ? text + "<br>" : "") +
      "⚠ " +
      frappe.utils.escape_html(d.attention_reason);
    colour =
      colour === "green" || colour === "gray" || colour === "blue"
        ? "orange"
        : colour;
  }
  frm.set_intro(text || "", colour);
}
