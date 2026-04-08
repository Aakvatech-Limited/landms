frappe.listview_settings['Plot Application'] = {

    add_fields: ['status', 'customer', 'plot', 'application_fee'],

    get_indicator(doc) {
        if (doc.docstatus === 0) {
            return [__('Draft'), 'gray', 'docstatus,=,0'];
        }
        const map = {
            'Draft':     ['Draft',     'gray',   'status,=,Draft'],
            'Submitted': ['Submitted', 'blue',   'status,=,Submitted'],
            'Paid':      ['Paid',      'green',  'status,=,Paid'],
            'Converted': ['Converted', 'purple', 'status,=,Converted'],
            'Expired':   ['Expired',   'red',    'status,=,Expired'],
            'Cancelled': ['Cancelled', 'red',    'status,=,Cancelled'],
        };
        return map[doc.status] || [doc.status || 'Unknown', 'gray', `status,=,${doc.status || ''}`];
    },
};
