import frappe
from frappe.utils import flt


def execute(filters=None):
	filters = filters or {}
	columns = get_columns()
	data = get_data(filters)
	summary = get_summary(data)
	chart = get_chart(data)
	return columns, data, None, chart, summary


def get_columns():
	return [
		{"label": "Date",            "fieldname": "posting_date",   "fieldtype": "Date",   "width": 110},
		{"label": "Journal Entry",   "fieldname": "journal_entry",  "fieldtype": "Link",   "options": "Journal Entry",   "width": 180},
		{"label": "Payment Entry",   "fieldname": "payment_entry",  "fieldtype": "Link",   "options": "Payment Entry",   "width": 180},
		{"label": "Land Acquisition","fieldname": "land_acquisition","fieldtype": "Link",   "options": "Land Acquisition","width": 180},
		{"label": "Customer",        "fieldname": "customer",        "fieldtype": "Link",   "options": "Customer",        "width": 200},
		{"label": "Govt Share (TZS)","fieldname": "govt_amount",    "fieldtype": "Currency",                              "width": 170},
	]


def get_data(filters):
	from_date       = filters.get("from_date")
	to_date         = filters.get("to_date")
	land_acquisition = filters.get("land_acquisition")

	conditions = [
		"je.docstatus = 1",
		"(je.lms_payment_entry IS NOT NULL AND je.lms_payment_entry != '')",
		"jea.credit_in_account_currency > 0",
	]
	params = {}

	if from_date:
		conditions.append("je.posting_date >= %(from_date)s")
		params["from_date"] = from_date
	if to_date:
		conditions.append("je.posting_date <= %(to_date)s")
		params["to_date"] = to_date
	if land_acquisition:
		conditions.append("jea.land_acquisition = %(land_acquisition)s")
		params["land_acquisition"] = land_acquisition

	where = " AND ".join(conditions)

	rows = frappe.db.sql(f"""
		SELECT
			je.name                            AS journal_entry,
			je.posting_date,
			je.lms_payment_entry               AS payment_entry,
			jea.land_acquisition,
			pe.party                           AS customer,
			jea.credit_in_account_currency     AS govt_amount
		FROM `tabJournal Entry` je
		INNER JOIN `tabJournal Entry Account` jea
			ON jea.parent = je.name
			AND jea.credit_in_account_currency > 0
		LEFT JOIN `tabPayment Entry` pe
			ON pe.name = je.lms_payment_entry
		WHERE {where}
		ORDER BY je.posting_date DESC, je.name
	""", params, as_dict=True)

	data = []
	for row in rows:
		data.append({
			"posting_date":    row.posting_date,
			"journal_entry":   row.journal_entry,
			"payment_entry":   row.payment_entry,
			"land_acquisition": row.land_acquisition,
			"customer":        row.customer,
			"govt_amount":     flt(row.govt_amount),
		})

	return data


def get_summary(data):
	if not data:
		return []
	total = sum(flt(r["govt_amount"]) for r in data)
	count = len(data)
	return [
		{"label": "Total Government Payable", "value": total, "datatype": "Currency", "indicator": "Blue"},
		{"label": "Number of Payments",        "value": count, "datatype": "Int",      "indicator": "Grey"},
	]


def get_chart(data):
	if not data:
		return None

	by_la = {}
	for row in data:
		la = row.get("land_acquisition") or "Unassigned"
		by_la[la] = by_la.get(la, 0) + flt(row["govt_amount"])

	if not by_la:
		return None

	labels = list(by_la.keys())
	values = [by_la[la] for la in labels]

	return {
		"data": {
			"labels": labels,
			"datasets": [{"name": "Govt Share (TZS)", "values": values}],
		},
		"type": "bar",
		"colors": ["#1971c2"],
	}
