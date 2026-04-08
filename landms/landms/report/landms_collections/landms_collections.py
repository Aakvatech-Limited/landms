import frappe
from frappe.utils import flt


def execute(filters=None):
	filters = filters or {}
	columns = get_columns()
	data    = get_data(filters)
	summary = get_summary(data)
	chart   = get_chart(data)
	return columns, data, None, chart, summary


def get_columns():
	return [
		{"label": "Customer",              "fieldname": "customer",          "fieldtype": "Link",    "options": "Customer", "width": 210},
		{"label": "Contracts",             "fieldname": "contract_count",    "fieldtype": "Int",                            "width": 100},
		{"label": "Total Invoiced (TZS)",  "fieldname": "total_invoiced",    "fieldtype": "Float",                          "width": 190},
		{"label": "Total Paid (TZS)",      "fieldname": "total_paid",        "fieldtype": "Float",                          "width": 170},
		{"label": "Outstanding (TZS)",     "fieldname": "total_outstanding", "fieldtype": "Float",                          "width": 180},
		{"label": "Collection Rate %",     "fieldname": "collection_rate",   "fieldtype": "Percent",                        "width": 150},
	]


def get_data(filters):
	conditions = [
		"pc.docstatus = 1",
		"pc.contract_status IN ('Ongoing', 'Completed')",
	]
	if filters.get("customer"):
		conditions.append("pc.customer = %(customer)s")
	if filters.get("from_date"):
		conditions.append("pc.contract_date >= %(from_date)s")
	if filters.get("to_date"):
		conditions.append("pc.contract_date <= %(to_date)s")

	where = " AND ".join(conditions)

	rows = frappe.db.sql(f"""
		SELECT
			pc.customer,
			COUNT(pc.name)            AS contract_count,
			SUM(pc.selling_price)     AS total_invoiced,
			SUM(pc.total_paid)        AS total_paid,
			SUM(pc.total_outstanding) AS total_outstanding
		FROM `tabPlot Contract` pc
		WHERE {where}
		GROUP BY pc.customer
		ORDER BY total_paid DESC
	""", filters, as_dict=True)

	data = []
	for row in rows:
		invoiced = flt(row.total_invoiced)
		paid     = flt(row.total_paid)
		rate     = (paid / invoiced * 100) if invoiced else 0
		data.append({
			"customer":          row.customer,
			"contract_count":    row.contract_count,
			"total_invoiced":    invoiced,
			"total_paid":        paid,
			"total_outstanding": flt(row.total_outstanding),
			"collection_rate":   rate,
		})
	return data


def get_summary(data):
	if not data:
		return []
	total_invoiced    = sum(flt(r["total_invoiced"])    for r in data)
	total_paid        = sum(flt(r["total_paid"])        for r in data)
	total_outstanding = sum(flt(r["total_outstanding"]) for r in data)
	overall_rate      = (total_paid / total_invoiced * 100) if total_invoiced else 0
	return [
		{"label": "Total Invoiced",          "value": total_invoiced,            "datatype": "Float", "indicator": "Blue"},
		{"label": "Total Collected",         "value": total_paid,                "datatype": "Float", "indicator": "Green"},
		{"label": "Total Outstanding",       "value": total_outstanding,         "datatype": "Float", "indicator": "Red"},
		{"label": "Overall Collection Rate", "value": round(overall_rate, 1),    "datatype": "Float", "indicator": "Blue"},
	]


def get_chart(data):
	if not data:
		return None

	top_rows = sorted(data, key=lambda d: flt(d.get("total_outstanding")), reverse=True)[:8]
	if not top_rows:
		return None

	return {
		"data": {
			"labels": [r["customer"] for r in top_rows],
			"datasets": [
				{"name": "Collected", "values": [flt(r["total_paid"]) for r in top_rows]},
				{"name": "Outstanding", "values": [flt(r["total_outstanding"]) for r in top_rows]},
			],
		},
		"type": "bar",
		"colors": ["#2f9e44", "#e03131"],
		"barOptions": {"stacked": 1},
	}
