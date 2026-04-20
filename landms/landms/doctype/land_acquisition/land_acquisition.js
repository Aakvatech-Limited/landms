frappe.ui.form.on('Land Acquisition', {
    refresh(frm) {
        if (frm.doc.__islocal || !frm.doc.name) {
            // New doc — wipe any stale supplier tables left over from a
            // previously viewed Land Acquisition so the user doesn't see
            // somebody else's PO/PI/PE rows on their fresh form.
            ['land_seller_summary_html', 'other_supplier_summary_html'].forEach(f => {
                const wrapper = frm.get_field(f)?.$wrapper;
                if (wrapper) wrapper.empty();
            });
            return;
        }

        // Workflow status alerts
        if (frm.doc.docstatus === 0 && frm.doc.status === 'Pending Approval') {
            frm.dashboard.set_headline_alert('Waiting for approval', 'orange');
        } else if (frm.doc.docstatus === 1 && frm.doc.status === 'Approved') {
            frm.dashboard.set_headline_alert('Approved', 'green');
        } else if (frm.doc.docstatus === 1 && frm.doc.status === 'Subdivided') {
            frm.dashboard.set_headline_alert('Approved and subdivided', 'blue');
        }

        refresh_cost_summary(frm);
        refresh_plot_counts(frm);

        frm.add_custom_button('Purchase Order', () => {
            frappe.flags.new_po_land_acquisition = frm.doc.name;
            frappe.new_doc('Purchase Order');
        }, __('Create'));

        frm.add_custom_button('Purchase Invoice', () => {
            frappe.flags.new_pi_land_acquisition = frm.doc.name;
            frappe.new_doc('Purchase Invoice');
        }, __('Create'));
    }
});

function refresh_cost_summary(frm) {
    frappe.call({
        method: 'landms.landms.doctype.land_acquisition.land_acquisition.sync_land_acquisition_cost_summary',
        args: { land_acquisition: frm.doc.name },
        callback(r) {
            const summary = r.message || {};
            const totals = summary.totals || {};

            const fields = {
                acquisition_cost_tzs: totals.acquisition_cost_tzs,
                cost_per_sqm_tzs: totals.cost_per_sqm_tzs,
                total_committed_tzs: totals.committed,
                total_paid_tzs: totals.paid,
                total_outstanding_tzs: totals.outstanding,
                total_unbilled_po_tzs: totals.unbilled_po,
            };
            for (const [name, value] of Object.entries(fields)) {
                frm.doc[name] = Number(value || 0);
                frm.refresh_field(name);
            }

            if (flt(frm.doc.total_area_sqm) > 0) {
                frm.set_df_property(
                    'cost_per_sqm_tzs', 'description',
                    `${flt(totals.acquisition_cost_tzs).toLocaleString()} TZS ÷ ${flt(frm.doc.total_area_sqm).toLocaleString()} sqm`
                );
            }

            render_supplier_table(
                frm, 'land_seller_summary_html',
                summary.sellers || [],
                'No land sellers tagged to this Land Acquisition yet.'
            );
            render_supplier_table(
                frm, 'other_supplier_summary_html',
                summary.others || [],
                'No other suppliers tagged to this Land Acquisition yet.'
            );
        }
    });
}

function refresh_plot_counts(frm) {
    frappe.call({
        method: 'landms.landms.doctype.land_acquisition.land_acquisition.sync_land_acquisition_plot_summary',
        args: { land_acquisition: frm.doc.name },
        callback(r) {
            const s = r.message || {};
            ['total_plots', 'available_plots', 'reserved_plots', 'delivered_plots'].forEach(f => {
                frm.doc[f] = Number(s[f] || 0);
                frm.refresh_field(f);
            });
        }
    });
}

function render_supplier_table(frm, fieldname, rows, empty_message) {
    const wrapper = frm.get_field(fieldname)?.$wrapper;
    if (!wrapper) return;

    if (!rows.length) {
        wrapper.html(`<div class="text-muted" style="padding: 8px 0;">${empty_message}</div>`);
        return;
    }

    const escape_html = (v) => frappe.utils.escape_html(String(v || ''));
    const fmt = (v) => format_currency(v || 0, 'TZS');

    const body = rows.map((row) => {
        const docs = build_drilldown_links(row);
        const outColor = row.outstanding > 0 ? '#d73a49' : 'inherit';
        const unbColor = row.unbilled_po > 0 ? '#e36209' : 'inherit';
        return `
            <tr>
                <td style="padding: 10px; vertical-align: middle;">
                    <strong>${escape_html(row.supplier_name)}</strong>
                    <div class="text-muted small">${escape_html(row.supplier)}</div>
                </td>
                <td class="text-right" style="padding: 10px; vertical-align: middle;">${fmt(row.committed)}</td>
                <td class="text-right" style="padding: 10px; vertical-align: middle;">${fmt(row.billed)}</td>
                <td class="text-right" style="padding: 10px; vertical-align: middle;">${fmt(row.paid)}</td>
                <td class="text-right" style="padding: 10px; vertical-align: middle; color: ${outColor};">${fmt(row.outstanding)}</td>
                <td class="text-right" style="padding: 10px; vertical-align: middle; color: ${unbColor};">${fmt(row.unbilled_po)}</td>
                <td style="padding: 10px; vertical-align: middle; font-size: 12px;">${docs}</td>
            </tr>
        `;
    }).join('');

    wrapper.html(`
        <div class="table-responsive">
            <table class="table table-bordered" style="margin-bottom: 0; font-size: 13px;">
                <thead style="background-color: #f8f9fa;">
                    <tr>
                        <th style="padding: 10px; font-weight: 600;">Supplier</th>
                        <th class="text-right" style="padding: 10px; font-weight: 600;">Committed</th>
                        <th class="text-right" style="padding: 10px; font-weight: 600;">Billed</th>
                        <th class="text-right" style="padding: 10px; font-weight: 600;">Paid</th>
                        <th class="text-right" style="padding: 10px; font-weight: 600;">Outstanding</th>
                        <th class="text-right" style="padding: 10px; font-weight: 600;">Unbilled PO</th>
                        <th style="padding: 10px; font-weight: 600;">Documents</th>
                    </tr>
                </thead>
                <tbody>${body}</tbody>
            </table>
        </div>
    `);
}

function build_drilldown_links(row) {
    const escape_html = (v) => frappe.utils.escape_html(String(v || ''));
    const link = (doctype, name) =>
        `<a href="/app/${doctype}/${encodeURIComponent(name)}" target="_blank">${escape_html(name)}</a>`;

    const parts = [];
    if (row.po_docs && row.po_docs.length) {
        parts.push(`PO: ${row.po_docs.map(d => link('purchase-order', d.name)).join(', ')}`);
    }
    if (row.pi_docs && row.pi_docs.length) {
        parts.push(`PI: ${row.pi_docs.map(d => link('purchase-invoice', d.name)).join(', ')}`);
    }
    if (row.pe_docs && row.pe_docs.length) {
        const pes = row.pe_docs.map(d => {
            const tag = d.against === 'po'
                ? ` <span class="text-muted">(advance)</span>`
                : '';
            return `${link('payment-entry', d.name)}${tag}`;
        });
        parts.push(`PE: ${pes.join(', ')}`);
    }
    return parts.join('<br>') || '<span class="text-muted">-</span>';
}
