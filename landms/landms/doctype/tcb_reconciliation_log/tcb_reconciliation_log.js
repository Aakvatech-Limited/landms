frappe.ui.form.on('TCB Reconciliation Log', {
	onload(frm) {
		// Default date range from TCB Integration Settings when creating a new doc.
		if (frm.is_new()) {
			frappe.db.get_single_value('TCB Integration Settings', 'reconciliation_lookback_days')
				.then(days => {
					const lookback = cint(days) || 1;
					const today = frappe.datetime.get_today();
					const from_date = frappe.datetime.add_days(today, -lookback);
					frm.set_value('date_range_end', today);
					frm.set_value('date_range_start', from_date);
				});
		}
	},

	refresh(frm) {
		_render_status_indicator(frm);

		const status = frm.doc.status;
		clearTimeout(frm.__recon_poll);

		if (!frm.is_new() && (status === 'Draft' || status === 'Failed' || status === 'Stopped')) {
			frm.add_custom_button(__('Run Reconciliation'), () => {
				frappe.confirm(
					__('Run TCB reconciliation for {0} to {1}?',
						[frm.doc.date_range_start, frm.doc.date_range_end]),
					() => {
						frm.call({
							method: 'run',
							args: { name: frm.docname },
							freeze: true,
							freeze_message: __('Queueing TCB reconciliation…'),
							callback(r) {
								frm.reload_doc();
							},
						});
					}
				);
			}).addClass('btn-primary');
		}

		if (!frm.is_new() && status === 'Running' && !frm.doc.stop_requested) {
			frm.add_custom_button(__('Stop Reconciliation'), () => {
				frappe.confirm(
					__('Request stop for this reconciliation run? It will stop after the current step finishes.'),
					() => {
						frm.call({
							method: 'request_stop',
							args: { name: frm.docname },
							freeze: true,
							freeze_message: __('Requesting stop…'),
							callback() {
								frm.reload_doc();
							},
						});
					}
				);
			}).addClass('btn-warning');
		}

		if (!frm.is_new() && status === 'Running') {
			frm.__recon_poll = setTimeout(() => {
				if (!frm.is_dirty()) {
					frm.reload_doc();
				}
			}, 5000);
		}

		// Lock fields once we're past Draft.
		const editable = (status === 'Draft');
		frm.set_df_property('date_range_start', 'read_only', !editable);
		frm.set_df_property('date_range_end',   'read_only', !editable);
		frm.set_df_property('triggered_by',     'read_only', !editable);
	},
});


function _render_status_indicator(frm) {
	const colors = {
		Draft:   'gray',
		Running: 'blue',
		Success: 'green',
		Partial: 'yellow',
		Failed:  'red',
		Stopped: 'orange',
	};
	frm.page.set_indicator(
		__(frm.doc.status),
		colors[frm.doc.status] || 'gray'
	);

	// Color-code each row in the Payment Rows child table so the accountant
	// can immediately see what passed, what was skipped, and what failed.
	setTimeout(() => {
		const grid = frm.fields_dict.rows && frm.fields_dict.rows.grid;
		if (!grid) return;

		const rowColors = {
			Applied: { bg: '#d3f9d8', color: '#1b7a2e', label: '✓ Applied' },
			Ignored: { bg: '#fff3cd', color: '#856404', label: '⚠ Ignored' },
			Failed:  { bg: '#fde8e8', color: '#c0392b', label: '✗ Failed'  },
		};

		grid.wrapper.find('.grid-row').each(function () {
			const row_name = $(this).attr('data-name');
			if (!row_name) return;
			const row_doc = frappe.get_doc('TCB Reconciliation Log Row', row_name);
			if (!row_doc) return;
			const cfg = rowColors[row_doc.row_status];
			if (!cfg) return;
			$(this).css({ 'background-color': cfg.bg, 'border-left': `4px solid ${cfg.color}` });
			$(this).find('[data-fieldname="row_status"]').css({ color: cfg.color, 'font-weight': '700' });
		});
	}, 300);
}
