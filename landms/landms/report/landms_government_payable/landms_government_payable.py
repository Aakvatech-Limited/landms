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
		{"label": "Contract",        "fieldname": "contract",                "fieldtype": "Link",    "options": "Plot Contract",   "width": 160},
		{"label": "Sales Order",     "fieldname": "sales_order",             "fieldtype": "Link",    "options": "Sales Order",     "width": 150},
		{"label": "Customer",        "fieldname": "customer",                "fieldtype": "Link",    "options": "Customer",        "width": 200},
		{"label": "Plot",            "fieldname": "plot",                    "fieldtype": "Link",    "options": "Plot Master",     "width": 130},
		{"label": "Contract Value (TZS)", "fieldname": "selling_price",      "fieldtype": "Float",                                 "width": 170},
		{"label": "Govt Share %",    "fieldname": "government_share_percent","fieldtype": "Percent",                               "width": 110},
		{"label": "Govt Fee (TZS)",  "fieldname": "government_fee_withheld", "fieldtype": "Float",                                 "width": 170},
		{"label": "Journal Entry",   "fieldname": "government_fee_entry",    "fieldtype": "Link",    "options": "Journal Entry",   "width": 170},
		{"label": "Fee Posted Date", "fieldname": "fee_posted_date",         "fieldtype": "Date",                                  "width": 140},
		{"label": "Status",          "fieldname": "fee_status",              "fieldtype": "Data",                                  "width": 100},
	]


def get_data(filters):
	status_filter = filters.get("status") or "All"
	from_date     = filters.get("from_date")
	to_date       = filters.get("to_date")

	conditions = [
		"pc.docstatus = 1",
		"pc.contract_status = 'Completed'",
		"pc.government_fee_withheld > 0",
	]

	if status_filter == "Posted":
		conditions.append("pc.government_fee_entry IS NOT NULL AND pc.government_fee_entry != ''")
	elif status_filter == "Pending":
		conditions.append("(pc.government_fee_entry IS NULL OR pc.government_fee_entry = '')")

	if from_date:
		conditions.append("je.posting_date >= %(from_date)s")
	if to_date:
		conditions.append("(je.posting_date <= %(to_date)s OR je.posting_date IS NULL)")

	where = " AND ".join(conditions)

	rows = frappe.db.sql(f"""
		SELECT
			pc.name                    AS contract,
			pc.sales_order,
			pc.customer,
			pc.plot,
			pc.selling_price,
			pc.government_share_percent,
			pc.government_fee_withheld,
			pc.government_fee_entry,
			je.posting_date            AS fee_posted_date
		FROM `tabPlot Contract` pc
		LEFT JOIN `tabJournal Entry` je
			ON je.name = pc.government_fee_entry
		WHERE {where}
		ORDER BY je.posting_date DESC, pc.name
	""", {"from_date": from_date, "to_date": to_date}, as_dict=True)

	data = []
	for row in rows:
		data.append({
			"contract":                 row.contract,
			"sales_order":              row.sales_order,
			"customer":                 row.customer,
			"plot":                     row.plot,
			"selling_price":            flt(row.selling_price),
			"government_share_percent": flt(row.government_share_percent),
			"government_fee_withheld":  flt(row.government_fee_withheld),
			"government_fee_entry":     row.government_fee_entry,
			"fee_posted_date":          row.fee_posted_date,
			"fee_status":               "Posted" if row.government_fee_entry else "Pending",
		})

	return data


def get_summary(data):
	if not data:
		return []
	total_fee   = sum(flt(r["government_fee_withheld"]) for r in data)
	posted_fee  = sum(flt(r["government_fee_withheld"]) for r in data if r["fee_status"] == "Posted")
	pending_fee = total_fee - posted_fee
	return [
		{"label": "Total Government Fee",     "value": total_fee,   "datatype": "Float", "indicator": "Blue"},
		{"label": "Posted to Govt Payable",   "value": posted_fee,  "datatype": "Float", "indicator": "Green"},
		{"label": "Still Pending",            "value": pending_fee, "datatype": "Float", "indicator": "Red"},
	]


def get_chart(data):
	if not data:
		return None

	posted_fee = sum(flt(r["government_fee_withheld"]) for r in data if r["fee_status"] == "Posted")
	pending_fee = sum(flt(r["government_fee_withheld"]) for r in data if r["fee_status"] == "Pending")
	if posted_fee <= 0 and pending_fee <= 0:
		return None

	return {
		"data": {
			"labels": ["Posted", "Pending"],
			"datasets": [
				{
					"name": "Government Fee",
					"values": [posted_fee, pending_fee],
				}
			],
		},
		"type": "donut",
		"colors": ["#2f9e44", "#e03131"],
	}
