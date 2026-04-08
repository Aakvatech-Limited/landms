frappe.ui.form.on('Plot Contract', {
	setup(frm) {
		frm.set_query('plot', () => ({
			filters: { status: 'Available' }
		}));
	},

	refresh(frm) {
		const colors = {
			'Draft': 'gray',
			'Ongoing': 'yellow',
			'Completed': 'green',
			'Cancelled': 'red',
			'Terminated': 'orange',
		};
		frm.page.set_indicator(
			frm.doc.contract_status,
			colors[frm.doc.contract_status] || 'gray'
		);

		refresh_linked_documents(frm);

		if (frm.doc.docstatus === 1 && frm.doc.contract_status === 'Ongoing') {
			frm.add_custom_button(__('Terminate Contract'), () => {
				frappe.prompt(
					{
						fieldname: 'reason',
						fieldtype: 'Long Text',
						label: __('Termination Reason'),
						reqd: 1,
						description: __('Explain why this contract is being terminated. Any money already paid remains forfeited.'),
					},
					(values) => {
						frappe.call({
							method: 'terminate_contract',
							doc: frm.doc,
							args: { reason: values.reason },
							freeze: true,
							freeze_message: __('Terminating contract...'),
							callback() { frm.reload_doc(); },
						});
					},
					__('Terminate Contract'),
					__('Terminate')
				);
			}, __('Actions'));
		}

		if (frm.doc.booking_fee_invoice) {
			frm.add_custom_button(__('View Plot Sales Invoice'), () => {
				frappe.set_route('Form', 'Sales Invoice', frm.doc.booking_fee_invoice);
			}, __('Actions'));
		}

		if (frm.doc.sales_order) {
			frm.add_custom_button(__('View Sales Order'), () => {
				frappe.set_route('Form', 'Sales Order', frm.doc.sales_order);
			}, __('Actions'));
		}

		if (frm.doc.plot_application) {
			frm.add_custom_button(__('View Plot Application'), () => {
				frappe.set_route('Form', 'Plot Application', frm.doc.plot_application);
			}, __('Actions'));
		}
	},

	plot(frm) {
		if (!frm.doc.plot) {
			frm.set_value('land_acquisition', '');
			frm.set_value('acquisition_name', '');
			frm.set_value('selling_price', 0);
			return;
		}

		frappe.db.get_doc('Plot Master', frm.doc.plot).then(plot_doc => {
			if (plot_doc.status !== 'Available') {
				frappe.msgprint({
					title: __('Plot Not Available'),
					message: __('Plot {0} has status "{1}". Only Available plots can be contracted.', [
						frm.doc.plot,
						plot_doc.status,
					]),
					indicator: 'red',
				});
				frm.set_value('plot', '');
				return;
			}

			frm.set_value('land_acquisition', plot_doc.land_acquisition);
			frm.set_value('selling_price', plot_doc.selling_price);
			frm.set_value('booking_fee_percent', plot_doc.booking_fee_percent || 0);
			frm.set_value('payment_completion_days', plot_doc.payment_completion_days || 0);
			frm.set_value('government_share_percent', plot_doc.government_share_percent || 0);
			frappe.db.get_value('Land Acquisition', plot_doc.land_acquisition, 'acquisition_name')
				.then(r => {
					frm.set_value('acquisition_name', (r.message && r.message.acquisition_name) || '');
				});
			recalculate_amounts(frm);
		});
	},

	contract_date(frm) {
		recalculate_amounts(frm);
	},
});

function recalculate_amounts(frm) {
	const selling_price = frm.doc.selling_price || 0;
	const pct = frm.doc.booking_fee_percent || 0;
	if (!selling_price || !pct) return;

	const fee = selling_price * (pct / 100);
	const balance = selling_price - fee;
	frm.set_value('booking_fee_amount', fee);
	frm.set_value('balance_due', balance);

	if (!frm.doc.contract_date) return;

	const total_days = frm.doc.payment_completion_days || 90;
	const deadline = frappe.datetime.add_days(frm.doc.contract_date, total_days);
	frm.set_value('payment_deadline', deadline);
	build_payment_schedule(frm, fee, balance, total_days);
}

function build_payment_schedule(frm, booking_fee, balance, total_days) {
	if (!frm.doc.contract_date || frm.doc.sales_order) return;

	frm.clear_table('payment_schedule');
	frm.add_child('payment_schedule', {
		installment_number: 1,
		description: 'Advance',
		due_date: frm.doc.contract_date,
		expected_amount: booking_fee,
		paid_amount: 0,
		outstanding_amount: booking_fee,
		paid_date: null,
		sales_invoice: '',
		status: 'Pending',
	});

	if (balance > 0) {
		frm.add_child('payment_schedule', {
			installment_number: 2,
			description: 'Balance',
			due_date: frappe.datetime.add_days(frm.doc.contract_date, total_days),
			expected_amount: balance,
			paid_amount: 0,
			outstanding_amount: balance,
			paid_date: null,
			sales_invoice: '',
			status: 'Pending',
		});
	}

	frm.refresh_field('payment_schedule');
}

function refresh_linked_documents(frm) {
	const wrapper = frm.get_field('linked_documents_html')?.$wrapper;
	if (!wrapper) return;

	if (frm.is_new() || !frm.doc.name) {
		wrapper.html('<div class="text-muted" style="padding: 8px 0;">Linked documents appear after the contract is saved.</div>');
		return;
	}

	frappe.call({
		method: 'landms.landms.doctype.plot_contract.plot_contract.get_linked_documents_summary',
		args: { plot_contract: frm.doc.name },
		callback(r) {
			wrapper.html(
				r.message
				|| '<div class="text-muted" style="padding: 8px 0;">No linked documents yet.</div>'
			);
		},
	});
}
