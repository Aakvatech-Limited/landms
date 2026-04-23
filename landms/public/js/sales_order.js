frappe.ui.form.on('Sales Order', {
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
