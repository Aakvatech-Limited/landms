frappe.listview_settings['Plot Contract'] = {

    add_fields: ['contract_status', 'customer', 'plot', 'selling_price', 'payment_progress'],

    get_indicator(doc) {
        if (doc.docstatus === 0) {
            return [__('Draft'), 'gray', 'contract_status,=,Draft'];
        }
        const map = {
            'Draft':      ['Draft',      'gray',   'contract_status,=,Draft'],
            'Ongoing':    ['Ongoing',    'yellow', 'contract_status,=,Ongoing'],
            'Completed':  ['Completed',  'green',  'contract_status,=,Completed'],
            'Cancelled':  ['Cancelled',  'red',    'contract_status,=,Cancelled'],
            'Terminated': ['Terminated', 'orange', 'contract_status,=,Terminated'],
        };
        return map[doc.contract_status]
            || [doc.contract_status || 'Unknown', 'gray', `contract_status,=,${doc.contract_status || ''}`];
    },
};
