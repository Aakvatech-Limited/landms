import frappe
from frappe.utils import flt, add_months, today


def execute(filters=None):
	filters = filters or {}
	columns = get_columns()
	data    = get_data(filters)
	chart   = get_chart(data)
	summary = get_summary(data)
	return columns, data, None, chart, summary


def get_columns():
	return [
		{"label": "Period",                   "fieldname": "period",             "fieldtype": "Data",  "width": 160},
		{"label": "New Contracts",            "fieldname": "new_contracts",      "fieldtype": "Int",   "width": 140},
		{"label": "New Contract Value (TZS)", "fieldname": "new_contract_value", "fieldtype": "Float", "width": 200},
		{"label": "Cash Collected (TZS)",     "fieldname": "cash_collected",     "fieldtype": "Float", "width": 200},
		{"label": "Completed",                "fieldname": "completed",          "fieldtype": "Int",   "width": 120},
		{"label": "Terminated",               "fieldname": "terminated",         "fieldtype": "Int",   "width": 120},
	]


def get_data(filters):
	grouping  = filters.get("grouping")  or "Monthly"
	from_date = filters.get("from_date") or add_months(today(), -12)
	to_date   = filters.get("to_date")   or today()

	if grouping == "Weekly":
		c_period = "CONCAT('Week ', LPAD(WEEK(contract_date, 1), 2, '0'), ' — ', YEAR(contract_date))"
		c_sort   = "YEARWEEK(contract_date, 1)"
		p_period = "CONCAT('Week ', LPAD(WEEK(pe.posting_date, 1), 2, '0'), ' — ', YEAR(pe.posting_date))"
		p_sort   = "YEARWEEK(pe.posting_date, 1)"
	else:
		c_period = "DATE_FORMAT(contract_date, '%%M %%Y')"
		c_sort   = "DATE_FORMAT(contract_date, '%%Y%%m')"
		p_period = "DATE_FORMAT(pe.posting_date, '%%M %%Y')"
		p_sort   = "DATE_FORMAT(pe.posting_date, '%%Y%%m')"

	# Contracts created per period
	contract_rows = frappe.db.sql(f"""
		SELECT
			{c_period}  AS period,
			{c_sort}    AS sort_key,
			COUNT(name) AS new_contracts,
			SUM(selling_price) AS new_contract_value,
			SUM(CASE WHEN contract_status = 'Completed'  THEN 1 ELSE 0 END) AS cnt_completed,
			SUM(CASE WHEN contract_status = 'Terminated' THEN 1 ELSE 0 END) AS cnt_terminated
		FROM `tabPlot Contract`
		WHERE docstatus = 1
		  AND contract_date BETWEEN %(from_date)s AND %(to_date)s
		GROUP BY sort_key, period
		ORDER BY sort_key
	""", {"from_date": from_date, "to_date": to_date}, as_dict=True)

	# Cash collected per period against the single plot sale invoice.
	revenue_rows = frappe.db.sql(f"""
		SELECT
			{p_period}                AS period,
			{p_sort}                  AS sort_key,
			SUM(per.allocated_amount) AS revenue
		FROM `tabPayment Entry` pe
		INNER JOIN `tabPayment Entry Reference` per
			ON  per.parent            = pe.name
			AND per.reference_doctype = 'Sales Invoice'
		INNER JOIN `tabSales Invoice` si
			ON  si.name                 = per.reference_name
			AND si.docstatus            = 1
			AND si.is_plot_sale_invoice = 1
		LEFT JOIN `tabSales Order` so
			ON  so.plot_sales_invoice   = si.name
			AND so.docstatus            = 1
		LEFT JOIN `tabPlot Contract` pc
			ON  pc.name                 = COALESCE(NULLIF(si.plot_contract, ''), so.plot_contract)
			AND pc.docstatus           != 2
		WHERE pe.party_type   = 'Customer'
		  AND pe.docstatus    = 1
		  AND pe.posting_date BETWEEN %(from_date)s AND %(to_date)s
		GROUP BY sort_key, period
		ORDER BY sort_key
	""", {"from_date": from_date, "to_date": to_date}, as_dict=True)

	# Build unified period map
	all_keys    = {r.sort_key: r.period for r in contract_rows}
	for r in revenue_rows:
		if r.sort_key not in all_keys:
			all_keys[r.sort_key] = r.period

	contract_map = {r.sort_key: r for r in contract_rows}
	revenue_map  = {r.sort_key: flt(r.revenue) for r in revenue_rows}

	data = []
	for sort_key in sorted(all_keys.keys()):
		cr = contract_map.get(sort_key)
		data.append({
			"period":             all_keys[sort_key],
			"new_contracts":      cr.new_contracts           if cr else 0,
			"new_contract_value": flt(cr.new_contract_value) if cr else 0.0,
			"cash_collected":     revenue_map.get(sort_key, 0.0),
			"completed":          cr.cnt_completed           if cr else 0,
			"terminated":         cr.cnt_terminated          if cr else 0,
		})

	return data


def get_chart(data):
	if not data:
		return None
	return {
		"data": {
			"labels": [r["period"] for r in data],
			"datasets": [
				{"name": "New Contract Value",  "values": [r["new_contract_value"] for r in data]},
				{"name": "Cash Collected",      "values": [r["cash_collected"]     for r in data]},
			],
		},
		"type":   "bar",
		"colors": ["#2c5f2e", "#4a9e4d"],
	}


def get_summary(data):
	if not data:
		return []

	total_contracts = sum(int(r.get("new_contracts") or 0) for r in data)
	total_value = sum(flt(r.get("new_contract_value")) for r in data)
	total_revenue = sum(flt(r.get("cash_collected")) for r in data)
	total_completed = sum(int(r.get("completed") or 0) for r in data)
	total_terminated = sum(int(r.get("terminated") or 0) for r in data)
	collection_ratio = (total_revenue / total_value * 100) if total_value else 0

	return [
		{"label": "New Contracts", "value": total_contracts, "datatype": "Int", "indicator": "Blue"},
		{"label": "New Contract Value", "value": total_value, "datatype": "Currency", "indicator": "Blue"},
		{"label": "Cash Collected", "value": total_revenue, "datatype": "Currency", "indicator": "Green"},
		{"label": "Completed Contracts", "value": total_completed, "datatype": "Int", "indicator": "Green"},
		{"label": "Terminated Contracts", "value": total_terminated, "datatype": "Int", "indicator": "Orange"},
		{
			"label": "Collection vs New Value %",
			"value": collection_ratio,
			"datatype": "Percent",
			"indicator": "Green" if collection_ratio >= 70 else "Orange",
		},
	]
