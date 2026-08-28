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
		{
			"label": "Application",
			"fieldname": "application",
			"fieldtype": "Link",
			"options": "Plot Application",
			"width": 140,
		},
		{
			"label": "Customer",
			"fieldname": "customer",
			"fieldtype": "Link",
			"options": "Customer",
			"width": 200,
		},
		{"label": "Plot", "fieldname": "plot", "fieldtype": "Link", "options": "Plot Master", "width": 130},
		{"label": "Plot Number", "fieldname": "plot_number", "fieldtype": "Data", "width": 130},
		{"label": "Acquisition", "fieldname": "acquisition_name", "fieldtype": "Data", "width": 180},
		{
			"label": "Plot Type",
			"fieldname": "plot_type",
			"fieldtype": "Link",
			"options": "Plot Type",
			"width": 130,
		},
		{
			"label": "Allocated Value (TZS)",
			"fieldname": "allocated_value",
			"fieldtype": "Float",
			"width": 180,
		},
		{"label": "Paid (TZS)", "fieldname": "paid", "fieldtype": "Float", "width": 150},
		{"label": "Outstanding (TZS)", "fieldname": "outstanding", "fieldtype": "Float", "width": 160},
		{"label": "Status", "fieldname": "status", "fieldtype": "Data", "width": 110},
		{"label": "Application Date", "fieldname": "application_date", "fieldtype": "Date", "width": 130},
	]


def get_data(filters):
	"""One row per Plot Application (customer ↔ plot allocation), joined to the
	Plot Master for the plot number / type / price ("how much"), and to the Plot
	Contract (if one exists) for amount paid. Outstanding is derived as
	allocated value − paid so it is consistent whether or not a contract exists yet."""
	conditions = ["pa.docstatus = 1"]
	if filters.get("customer"):
		conditions.append("pa.customer = %(customer)s")
	if filters.get("land_acquisition"):
		conditions.append("pa.land_acquisition = %(land_acquisition)s")
	if filters.get("plot_type"):
		conditions.append("pm.plot_type = %(plot_type)s")
	if filters.get("status"):
		conditions.append("pa.status = %(status)s")
	if filters.get("from_date"):
		conditions.append("pa.application_date >= %(from_date)s")
	if filters.get("to_date"):
		conditions.append("pa.application_date <= %(to_date)s")

	where = " AND ".join(conditions)

	rows = frappe.db.sql(
		f"""
		SELECT
			pa.name              AS application,
			pa.customer,
			pa.plot,
			pm.plot_number,
			pa.acquisition_name,
			pm.plot_type,
			pm.selling_price     AS allocated_value,
			pa.status,
			pa.application_date,
			pc.total_paid        AS paid
		FROM `tabPlot Application` pa
		LEFT JOIN `tabPlot Master`   pm ON pm.name = pa.plot
		LEFT JOIN `tabPlot Contract` pc ON pc.plot_application = pa.name AND pc.docstatus != 2
		WHERE {where}
		ORDER BY pa.application_date DESC, pa.name DESC
	""",
		filters,
		as_dict=True,
	)

	data = []
	for row in rows:
		allocated = flt(row.allocated_value)
		paid = flt(row.paid)
		data.append(
			{
				"application": row.application,
				"customer": row.customer,
				"plot": row.plot,
				"plot_number": row.plot_number,
				"acquisition_name": row.acquisition_name,
				"plot_type": row.plot_type,
				"allocated_value": allocated,
				"paid": paid,
				"outstanding": max(0.0, allocated - paid),
				"status": row.status,
				"application_date": row.application_date,
			}
		)
	return data


def get_summary(data):
	if not data:
		return []
	total_plots = len(data)
	total_value = sum(flt(r["allocated_value"]) for r in data)
	total_paid = sum(flt(r["paid"]) for r in data)
	total_out = sum(flt(r["outstanding"]) for r in data)
	return [
		{"label": "Plots Allocated", "value": total_plots, "datatype": "Int", "indicator": "Blue"},
		{"label": "Total Allocated Value", "value": total_value, "datatype": "Currency", "indicator": "Blue"},
		{"label": "Total Paid", "value": total_paid, "datatype": "Currency", "indicator": "Green"},
		{"label": "Total Outstanding", "value": total_out, "datatype": "Currency", "indicator": "Red"},
	]


def get_chart(data):
	if not data:
		return None
	by_status = {}
	for row in data:
		s = row.get("status") or "Unknown"
		by_status[s] = by_status.get(s, 0) + 1
	labels = list(by_status.keys())
	if not labels:
		return None
	return {
		"data": {
			"labels": labels,
			"datasets": [{"name": "Allocations", "values": [by_status[s] for s in labels]}],
		},
		"type": "donut",
	}
