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
		{"label": "Contract",                  "fieldname": "contract",          "fieldtype": "Link",    "options": "Plot Contract", "width": 150},
		{"label": "Sales Order",               "fieldname": "sales_order",       "fieldtype": "Link",    "options": "Sales Order",   "width": 150},
		{"label": "Customer",                  "fieldname": "customer",          "fieldtype": "Link",    "options": "Customer",      "width": 190},
		{"label": "Plot",                      "fieldname": "plot",              "fieldtype": "Link",    "options": "Plot Master",   "width": 130},
		{"label": "Contract Date",             "fieldname": "contract_date",     "fieldtype": "Date",                                "width": 120},
		{"label": "Status",                    "fieldname": "contract_status",   "fieldtype": "Data",                                "width": 100},
		{"label": "Contract Value (TZS)",      "fieldname": "selling_price",     "fieldtype": "Float",                               "width": 170},
		{"label": "Advances Collected (TZS)",  "fieldname": "total_paid",        "fieldtype": "Float",                               "width": 200},
		{"label": "Still Outstanding (TZS)",   "fieldname": "total_outstanding", "fieldtype": "Float",                               "width": 190},
		{"label": "% Collected",               "fieldname": "pct_collected",     "fieldtype": "Percent",                             "width": 110},
	]


def get_data(filters):
	conditions = [
		"docstatus = 1",
		"contract_status = 'Ongoing'",
		"total_paid > 0",
	]
	if filters.get("customer"):
		conditions.append("customer = %(customer)s")

	where = " AND ".join(conditions)

	rows = frappe.db.sql(f"""
		SELECT
			name             AS contract,
			sales_order,
			customer,
			plot,
			contract_date,
			contract_status,
			selling_price,
			total_paid,
			total_outstanding
		FROM `tabPlot Contract`
		WHERE {where}
		ORDER BY total_paid DESC
	""", filters, as_dict=True)

	data = []
	for row in rows:
		price = flt(row.selling_price)
		paid  = flt(row.total_paid)
		pct   = (paid / price * 100) if price else 0
		data.append({
			"contract":          row.contract,
			"sales_order":       row.sales_order,
			"customer":          row.customer,
			"plot":              row.plot,
			"contract_date":     row.contract_date,
			"contract_status":   row.contract_status,
			"selling_price":     price,
			"total_paid":        paid,
			"total_outstanding": flt(row.total_outstanding),
			"pct_collected":     pct,
		})
	return data


def get_summary(data):
	if not data:
		return []
	total_advances    = sum(flt(r["total_paid"])        for r in data)
	total_outstanding = sum(flt(r["total_outstanding"]) for r in data)
	collection_pct = (total_advances / (total_advances + total_outstanding) * 100) if (total_advances + total_outstanding) else 0
	return [
		{"label": "Contracts Pending Delivery",  "value": len(data),          "datatype": "Int",   "indicator": "Blue"},
		{"label": "Total Advances Collected",    "value": total_advances,     "datatype": "Currency", "indicator": "Yellow"},
		{"label": "Revenue Still to be Earned",  "value": total_outstanding,  "datatype": "Currency", "indicator": "Red"},
		{
			"label": "Collected Portion %",
			"value": collection_pct,
			"datatype": "Percent",
			"indicator": "Green" if collection_pct >= 50 else "Orange",
		},
	]


def get_chart(data):
	if not data:
		return None

	top_rows = sorted(data, key=lambda d: flt(d.get("total_outstanding")), reverse=True)[:8]
	if not top_rows:
		return None

	return {
		"data": {
			"labels": [r["contract"] for r in top_rows],
			"datasets": [
				{"name": "Collected", "values": [flt(r["total_paid"]) for r in top_rows]},
				{"name": "Unearned", "values": [flt(r["total_outstanding"]) for r in top_rows]},
			],
		},
		"type": "bar",
		"colors": ["#f08c00", "#e03131"],
		"barOptions": {"stacked": 1},
	}
