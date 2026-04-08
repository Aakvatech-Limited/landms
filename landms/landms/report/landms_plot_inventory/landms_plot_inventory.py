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
		{"label": "Plot", "fieldname": "plot", "fieldtype": "Link", "options": "Plot Master", "width": 140},
		{"label": "Land Acquisition", "fieldname": "land_acquisition", "fieldtype": "Link", "options": "Land Acquisition", "width": 170},
		{"label": "Acquisition Name", "fieldname": "acquisition_name", "fieldtype": "Data", "width": 220},
		{"label": "Plot Number", "fieldname": "plot_number", "fieldtype": "Data", "width": 130},
		{"label": "Plot Type", "fieldname": "plot_type", "fieldtype": "Data", "width": 120},
		{"label": "Size (sqm)", "fieldname": "plot_size_sqm", "fieldtype": "Float", "width": 110},
		{"label": "Status", "fieldname": "status", "fieldtype": "Data", "width": 120},
		{"label": "Allocated Cost (TZS)", "fieldname": "allocated_cost", "fieldtype": "Float", "width": 180},
		{"label": "Selling Price (TZS)", "fieldname": "selling_price", "fieldtype": "Float", "width": 170},
		{"label": "Margin (TZS)", "fieldname": "margin", "fieldtype": "Float", "width": 150},
		{"label": "Margin %", "fieldname": "margin_pct", "fieldtype": "Percent", "width": 100},
	]


def get_data(filters):
	conditions = ["pm.docstatus = 1"]
	if filters.get("status"):
		conditions.append("pm.status = %(status)s")
	if filters.get("land_acquisition"):
		conditions.append("pm.land_acquisition = %(land_acquisition)s")
	if filters.get("plot_type"):
		conditions.append("pm.plot_type = %(plot_type)s")

	where = " AND ".join(conditions)

	rows = frappe.db.sql(f"""
		SELECT
			pm.name AS plot,
			pm.land_acquisition,
			pm.acquisition_name,
			pm.plot_number,
			pm.plot_type,
			pm.plot_size_sqm,
			pm.status,
			pm.allocated_cost,
			pm.selling_price
		FROM `tabPlot Master` pm
		WHERE {where}
		ORDER BY pm.land_acquisition, pm.status, pm.plot_number
	""", filters, as_dict=True)

	data = []
	for row in rows:
		cost = flt(row.allocated_cost)
		price = flt(row.selling_price)
		margin = price - cost
		margin_pct = (margin / price * 100) if price else 0
		data.append({
			"plot": row.plot,
			"land_acquisition": row.land_acquisition,
			"acquisition_name": row.acquisition_name,
			"plot_number": row.plot_number,
			"plot_type": row.plot_type,
			"plot_size_sqm": flt(row.plot_size_sqm),
			"status": row.status,
			"allocated_cost": cost,
			"selling_price": price,
			"margin": margin,
			"margin_pct": margin_pct,
		})
	return data


def get_summary(data):
	if not data:
		return []

	available = sum(1 for r in data if r["status"] == "Available")
	pending_fee = sum(1 for r in data if r["status"] == "Pending Fee")
	pending_advance = sum(1 for r in data if r["status"] == "Pending Advance")
	reserved = sum(1 for r in data if r["status"] == "Reserved")
	ready_for_handover = sum(1 for r in data if r["status"] == "Ready for Handover")
	delivered = sum(1 for r in data if r["status"] == "Delivered")
	title_closed = sum(1 for r in data if r["status"] == "Title Closed")

	total_cost = sum(flt(r["allocated_cost"]) for r in data)
	total_price = sum(flt(r["selling_price"]) for r in data)
	total_margin = sum(flt(r["margin"]) for r in data)
	margin_pct = (total_margin / total_price * 100) if total_price else 0

	return [
		{"label": "Total Plots", "value": len(data), "datatype": "Int", "indicator": "Blue"},
		{"label": "Available", "value": available, "datatype": "Int", "indicator": "Green"},
		{"label": "Pending Fee", "value": pending_fee, "datatype": "Int", "indicator": "Orange"},
		{"label": "Pending Advance", "value": pending_advance, "datatype": "Int", "indicator": "Yellow"},
		{"label": "Reserved", "value": reserved, "datatype": "Int", "indicator": "Orange"},
		{"label": "Ready for Handover", "value": ready_for_handover, "datatype": "Int", "indicator": "Cyan"},
		{"label": "Delivered", "value": delivered, "datatype": "Int", "indicator": "Blue"},
		{"label": "Title Closed", "value": title_closed, "datatype": "Int", "indicator": "Purple"},
		{"label": "Inventory Cost (TZS)", "value": total_cost, "datatype": "Float", "indicator": "Grey"},
		{"label": "Asking Value (TZS)", "value": total_price, "datatype": "Float", "indicator": "Blue"},
		{"label": "Potential Margin (TZS)", "value": total_margin, "datatype": "Float", "indicator": "Green"},
		{"label": "Portfolio Margin %", "value": margin_pct, "datatype": "Percent", "indicator": "Green" if margin_pct >= 0 else "Red"},
	]


def get_chart(data):
	if not data:
		return None

	status_order = ["Available", "Pending Fee", "Pending Advance", "Reserved", "Ready for Handover", "Delivered", "Title Closed"]
	status_counts = {status: 0 for status in status_order}
	for row in data:
		if row["status"] in status_counts:
			status_counts[row["status"]] += 1

	labels = [status for status in status_order if status_counts[status] > 0]
	values = [status_counts[status] for status in labels]

	if not labels:
		return None

	color_map = {
		"Available": "#2f9e44",
		"Pending Fee": "#f08c00",
		"Pending Advance": "#fab005",
		"Reserved": "#f08c00",
		"Ready for Handover": "#15aabf",
		"Delivered": "#1c7ed6",
		"Title Closed": "#7b2cbf",
	}

	return {
		"data": {
			"labels": labels,
			"datasets": [
				{
					"name": "Plots",
					"values": values,
				}
			],
		},
		"type": "donut",
		"colors": [color_map[label] for label in labels],
	}
