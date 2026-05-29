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
			'Overdue': 'red',
			'Completed': 'green',
			'Cancelled': 'red',
			'Terminated': 'orange',
		};
		frm.page.set_indicator(
			frm.doc.contract_status,
			colors[frm.doc.contract_status] || 'gray'
		);
		_render_payment_countdown(frm);

		render_payment_progress_bar(frm);
		refresh_linked_documents(frm);

		// --- Action buttons ---

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

		// --- Navigation buttons ---

		if (frm.doc.sales_order) {
			frm.add_custom_button(__('Sales Order'), () => {
				frappe.set_route('Form', 'Sales Order', frm.doc.sales_order);
			}, __('View'));
		}

		if (frm.doc.plot_application) {
			frm.add_custom_button(__('Plot Application'), () => {
				frappe.set_route('Form', 'Plot Application', frm.doc.plot_application);
			}, __('View'));
		}

		if (frm.doc.booking_fee_invoice) {
			frm.add_custom_button(__('Plot Sales Invoice'), () => {
				frappe.set_route('Form', 'Sales Invoice', frm.doc.booking_fee_invoice);
			}, __('View'));
		}

		if (frm.doc.plot) {
			frm.add_custom_button(__('Plot Master'), () => {
				frappe.set_route('Form', 'Plot Master', frm.doc.plot);
			}, __('View'));
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

function render_payment_progress_bar(frm) {
	if (frm.is_new()) return;

	const total = frm.doc.total_contract_value || 0;
	const paid = frm.doc.total_paid || 0;
	if (!total) return;

	const pct = Math.min(100, Math.round((paid / total) * 100));
	let color = 'bg-primary';
	if (pct >= 100) color = 'bg-success';
	else if (pct >= 50) color = 'bg-info';
	else if (pct > 0) color = 'bg-warning';

	const bar_html = `
		<div style="margin-top: 6px;">
			<div class="progress" style="height: 12px; border-radius: 6px;">
				<div class="progress-bar ${color}" role="progressbar"
					style="width: ${pct}%; border-radius: 6px;"
					title="${format_currency(paid)} of ${format_currency(total)} (${pct}%)">
				</div>
			</div>
			<div class="text-muted small" style="margin-top: 4px;">
				${format_currency(paid)} paid of ${format_currency(total)} (${pct}%)
			</div>
		</div>
	`;

	const field = frm.get_field('payment_progress');
	if (field && field.$wrapper) {
		let bar_el = field.$wrapper.find('.lms-progress-bar');
		if (!bar_el.length) {
			bar_el = $('<div class="lms-progress-bar"></div>');
			field.$wrapper.append(bar_el);
		}
		bar_el.html(bar_html);
	}
}

function _render_payment_countdown(frm) {
	if (frm.is_new()) return;
	if (!frm.doc.payment_deadline) return;
	if (['Completed', 'Terminated', 'Cancelled'].includes(frm.doc.contract_status)) return;

	const today = frappe.datetime.get_today();
	const deadline = frm.doc.payment_deadline;
	const days_remaining = frappe.datetime.get_day_diff(deadline, today);

	let color, icon, message;
	if (days_remaining < 0) {
		color = '#fde8e8';
		icon = '🔴';
		message = `Overdue by <strong>${Math.abs(days_remaining)} days</strong> — deadline was ${frappe.datetime.str_to_user(deadline)}`;
	} else if (days_remaining <= 30) {
		color = '#fff3cd';
		icon = '🟡';
		message = `<strong>${days_remaining} days remaining</strong> — deadline: ${frappe.datetime.str_to_user(deadline)}`;
	} else {
		color = '#d4edda';
		icon = '🟢';
		message = `<strong>${days_remaining} days remaining</strong> — deadline: ${frappe.datetime.str_to_user(deadline)}`;
	}

	frm.dashboard.set_headline_alert(
		`${icon} Payment Deadline: ${message}`,
		days_remaining < 0 ? 'red' : days_remaining <= 30 ? 'orange' : 'green'
	);
}

function refresh_linked_documents(frm) {
	const wrapper = frm.get_field('linked_documents_html')?.$wrapper;
	if (!wrapper) return;

	if (frm.is_new() || !frm.doc.name) {
		wrapper.html('<div class="text-muted small" style="padding: 8px 0;">Linked documents appear after the contract is saved.</div>');
		return;
	}

	frappe.call({
		method: 'landms.landms.doctype.plot_contract.plot_contract.get_linked_documents_summary',
		args: { plot_contract: frm.doc.name },
		callback(r) {
			wrapper.html(
				r.message
				|| '<div class="text-muted small" style="padding: 8px 0;">No linked documents yet.</div>'
			);
		},
	});
}
