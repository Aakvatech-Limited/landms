frappe.ui.form.on('Sales Order', {
    refresh(frm) {
        _render_so_payment_countdown(frm);
        _render_so_close_button(frm);
    },

    setup(frm) {
        frm.set_query('plot_application', () => ({
            filters: {
                docstatus: 1,
                status: 'Paid'
            }
        }));
    },

    plot_application(frm) {
        if (!frm.doc.plot_application) {
            frm.set_value('plot', '');
            frm.set_value('customer', '');
            frm.set_value('land_acquisition', '');
            frm.set_value('acquisition_name', '');
            frm.set_value('booking_fee_percent', 0);
            frm.set_value('government_share_percent', 0);
            frm.set_value('payment_completion_days', 0);
            frm.set_value('payment_deadline', '');
            frm.doc.items = [];
            frm.doc.payment_schedule = [];
            frm.refresh_fields();
            return;
        }
        frappe.call({
            method: 'landms.sales_order_hooks.get_sales_order_defaults',
            args: { plot_application: frm.doc.plot_application },
            freeze: true,
            freeze_message: __('Loading plot details...'),
            callback(r) {
                if (!r.message) return;
                let d = r.message;
                frm.set_value('customer', d.customer);
                frm.set_value('plot', d.plot);
                frm.set_value('land_acquisition', d.land_acquisition);
                frm.set_value('acquisition_name', d.acquisition_name);
                frm.set_value('booking_fee_percent', d.booking_fee_percent);
                frm.set_value('government_share_percent', d.government_share_percent);
                frm.set_value('payment_completion_days', d.payment_completion_days);
                frm.set_value('transaction_date', d.transaction_date);
                frm.set_value('delivery_date', d.delivery_date);
                frm.set_value('payment_deadline', d.payment_deadline);
                frm.set_value('company', d.company);
                frm.set_value('set_warehouse', d.set_warehouse);

                frm.doc.items = [];
                let row = frm.add_child('items');
                Object.assign(row, d.item_row);

                frm.doc.payment_schedule = [];
                (d.schedule_rows || []).forEach(s => {
                    let ps = frm.add_child('payment_schedule');
                    Object.assign(ps, s);
                });

                frm.refresh_fields();
            }
        });
    }
});

function _render_so_close_button(frm) {
    if (frm.is_new()) return;
    if (!frm.doc.plot) return;
    if (frm.doc.docstatus !== 1) return;
    if (['Closed', 'Cancelled'].includes(frm.doc.status)) return;

    setTimeout(() => {
        frm.remove_custom_button(__('Close'), __('Status'));
        frm.remove_custom_button(__('Hold'), __('Status'));
    }, 10);

    if (!frappe.user.has_role('LandMS Sales Manager')) return;

    frm.add_custom_button(__('Close Sales Order'), () => {
        frappe.confirm(
            __('Close this Sales Order? The plot will be released and the TCB reference declined. This cannot be undone.'),
            () => {
                frappe.call({
                    method: 'landms.sales_order_hooks.close_sales_order',
                    args: { sales_order_name: frm.doc.name },
                    freeze: true,
                    freeze_message: __('Closing Sales Order...'),
                    callback(r) {
                        if (r.exc) return;
                        frm.reload_doc();
                    }
                });
            }
        );
    }, __('Actions'));
}

function _render_so_payment_countdown(frm) {
    if (frm.is_new()) return;
    if (!frm.doc.payment_deadline) return;
    if (!frm.doc.plot) return;
    if (frm.doc.docstatus === 2) return;
    if (['Closed', 'Completed'].includes(frm.doc.status)) return;

    frm.dashboard.clear_headline();

    const today = frappe.datetime.get_today();
    const deadline = frm.doc.payment_deadline;
    const days_remaining = frappe.datetime.get_day_diff(deadline, today);

    let indicator, message;
    if (days_remaining < 0) {
        indicator = 'red';
        message = `🔴 Payment overdue by <strong>${Math.abs(days_remaining)} days</strong> — deadline was ${frappe.datetime.str_to_user(deadline)}`;
    } else if (days_remaining <= 30) {
        indicator = 'orange';
        message = `🟡 <strong>${days_remaining} days remaining</strong> to complete payment — deadline: ${frappe.datetime.str_to_user(deadline)}`;
    } else {
        indicator = 'green';
        message = `🟢 <strong>${days_remaining} days remaining</strong> to complete payment — deadline: ${frappe.datetime.str_to_user(deadline)}`;
    }

    frm.dashboard.set_headline_alert(message, indicator);
}
