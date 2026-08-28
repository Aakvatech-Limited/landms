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
		{"label": "Date", "fieldname": "date", "fieldtype": "Date", "width": 105},
		{"label": "Type", "fieldname": "source_type", "fieldtype": "Data", "width": 120},
		{
			"label": "Source",
			"fieldname": "source_document",
			"fieldtype": "Dynamic Link",
			"options": "source_doctype",
			"width": 150,
		},
		{
			"label": "source_doctype",
			"fieldname": "source_doctype",
			"fieldtype": "Data",
			"width": 1,
			"hidden": 1,
		},
		{
			"label": "Customer",
			"fieldname": "customer",
			"fieldtype": "Link",
			"options": "Customer",
			"width": 170,
		},
		{"label": "Plot", "fieldname": "plot", "fieldtype": "Link", "options": "Plot Master", "width": 120},
		{"label": "Total Paid (TZS)", "fieldname": "total_paid", "fieldtype": "Float", "width": 150},
		{"label": "Govt Share (TZS)", "fieldname": "govt_share", "fieldtype": "Float", "width": 150},
		{
			"label": "Forfeited — Company Net (TZS)",
			"fieldname": "forfeited",
			"fieldtype": "Float",
			"width": 200,
		},
		{"label": "Refund Due (TZS)", "fieldname": "refund_due", "fieldtype": "Float", "width": 150},
		{
			"label": "Forfeiture JE",
			"fieldname": "forfeiture_je",
			"fieldtype": "Link",
			"options": "Journal Entry",
			"width": 150,
		},
		{"label": "Reason", "fieldname": "reason", "fieldtype": "Data", "width": 240},
	]


def get_data(filters):
	"""Every event that forfeited customer money. Two sources, no overlap:
	  - Contract Termination  -> Plot Contract.forfeiture_entry
	  - Sales Order closed / cancelled / auto-expired -> Sales Order.forfeiture_entry

	Each customer's Total Paid splits three ways:
	    Total Paid = Govt Share + Forfeited (company net) + Refund Due
	"Forfeited" is the company's NET income (already net of the government share);
	"Refund Due" is owed back to the customer (lms_refund_amount on the JE). Govt Share
	is derived as Total Paid - Forfeited - Refund (equals the govt share actually
	withheld on the payments). Total Paid comes from the contract (terminations) or the
	Sales Order's advance_paid (SO-side, where the draft contract was deleted)."""
	pc_conditions = ["pc.forfeiture_entry IS NOT NULL", "pc.forfeiture_entry != ''", "je.docstatus = 1"]
	so_conditions = ["so.forfeiture_entry IS NOT NULL", "so.forfeiture_entry != ''", "je.docstatus = 1"]
	if filters.get("customer"):
		pc_conditions.append("pc.customer = %(customer)s")
		so_conditions.append("so.customer = %(customer)s")
	if filters.get("land_acquisition"):
		pc_conditions.append("pc.land_acquisition = %(land_acquisition)s")
		so_conditions.append("so.land_acquisition = %(land_acquisition)s")
	if filters.get("from_date"):
		pc_conditions.append("je.posting_date >= %(from_date)s")
		so_conditions.append("je.posting_date >= %(from_date)s")
	if filters.get("to_date"):
		pc_conditions.append("je.posting_date <= %(to_date)s")
		so_conditions.append("je.posting_date <= %(to_date)s")

	stype = filters.get("source_type")
	rows = []

	if stype != "Sales Order":
		rows += frappe.db.sql(
			f"""
			SELECT je.posting_date          AS date,
			       'Termination'            AS source_type,
			       pc.name                  AS source_document,
			       'Plot Contract'          AS source_doctype,
			       pc.customer,
			       pc.plot,
			       pc.total_paid            AS total_paid,
			       je.total_debit           AS forfeited,
			       je.lms_refund_amount     AS refund_due,
			       je.name                  AS forfeiture_je,
			       pc.termination_reason    AS reason
			FROM `tabPlot Contract` pc
			INNER JOIN `tabJournal Entry` je ON je.name = pc.forfeiture_entry
			WHERE {" AND ".join(pc_conditions)}
		""",
			filters,
			as_dict=True,
		)

	if stype != "Termination":
		rows += frappe.db.sql(
			f"""
			SELECT je.posting_date          AS date,
			       'Sales Order'            AS source_type,
			       so.name                  AS source_document,
			       'Sales Order'            AS source_doctype,
			       so.customer,
			       so.plot,
			       so.advance_paid          AS total_paid,
			       je.total_debit           AS forfeited,
			       je.lms_refund_amount     AS refund_due,
			       je.name                  AS forfeiture_je,
			       'Closed / cancelled / auto-expired before contract was submitted' AS reason
			FROM `tabSales Order` so
			INNER JOIN `tabJournal Entry` je ON je.name = so.forfeiture_entry
			WHERE {" AND ".join(so_conditions)}
		""",
			filters,
			as_dict=True,
		)

	for r in rows:
		r["total_paid"] = flt(r.get("total_paid"))
		r["forfeited"] = flt(r.get("forfeited"))
		r["refund_due"] = flt(r.get("refund_due"))
		# Govt share = whatever of Total Paid is left after the company's net forfeit
		# and the customer's refund. (Pre-fix forfeitures posted gross, so this is 0.)
		r["govt_share"] = max(0.0, r["total_paid"] - r["forfeited"] - r["refund_due"])
	rows.sort(key=lambda r: r.get("date") or "", reverse=True)
	return rows


def get_summary(data):
	if not data:
		return []
	total_paid = sum(flt(r["total_paid"]) for r in data)
	total_govt = sum(flt(r["govt_share"]) for r in data)
	total_forfeited = sum(flt(r["forfeited"]) for r in data)
	total_refund = sum(flt(r["refund_due"]) for r in data)
	return [
		{"label": "Forfeiture Events", "value": len(data), "datatype": "Int", "indicator": "Blue"},
		{
			"label": "Total Paid by Customers",
			"value": total_paid,
			"datatype": "Currency",
			"indicator": "Blue",
		},
		{"label": "Government Share", "value": total_govt, "datatype": "Currency", "indicator": "Orange"},
		{
			"label": "Forfeited — Company Net",
			"value": total_forfeited,
			"datatype": "Currency",
			"indicator": "Green",
		},
		{
			"label": "Refund Due to Customers",
			"value": total_refund,
			"datatype": "Currency",
			"indicator": "Red",
		},
	]


def get_chart(data):
	if not data:
		return None
	by_type = {}
	for r in data:
		t = r.get("source_type") or "Other"
		by_type[t] = by_type.get(t, 0.0) + flt(r["forfeited"])
	labels = list(by_type.keys())
	if not labels:
		return None
	return {
		"data": {
			"labels": labels,
			"datasets": [{"name": "Forfeited — Company Net (TZS)", "values": [by_type[l] for l in labels]}],
		},
		"type": "bar",
		"colors": ["#2f9e44"],
	}
