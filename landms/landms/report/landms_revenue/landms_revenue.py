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
		{"label": "Period",                   "fieldname": "period",       "fieldtype": "Data",    "width": 160},
		{"label": "Contracts Completed",      "fieldname": "completed",    "fieldtype": "Int",     "width": 170},
		{"label": "Revenue Recognized (TZS)", "fieldname": "revenue",      "fieldtype": "Float",   "width": 210},
		{"label": "COGS (TZS)",               "fieldname": "cogs",         "fieldtype": "Float",   "width": 170},
		{"label": "Gross Margin (TZS)",       "fieldname": "gross_margin", "fieldtype": "Float",   "width": 190},
		{"label": "Margin %",                 "fieldname": "margin_pct",   "fieldtype": "Percent", "width": 110},
	]


def get_data(filters):
	"""Accrual revenue: revenue is recognised when a Plot Contract is fully paid
	(status Completed), NOT when cash is collected. Cash collection lives in the
	LandMS Collections report. Revenue = selling_price - government share; COGS is
	the plot's allocated land cost. Grouped by the period the revenue was recognised."""
	grouping  = filters.get("grouping")  or "Monthly"
	from_date = filters.get("from_date") or add_months(today(), -12)
	to_date   = filters.get("to_date")   or today()

	if grouping == "Weekly":
		period_expr = "CONCAT('Week ', LPAD(WEEK(recog.recognition_date, 1), 2, '0'), ' — ', YEAR(recog.recognition_date))"
		sort_expr   = "YEARWEEK(recog.recognition_date, 1)"
	else:
		period_expr = "DATE_FORMAT(recog.recognition_date, '%%M %%Y')"
		sort_expr   = "DATE_FORMAT(recog.recognition_date, '%%Y%%m')"

	# Recognition date = posting date of the contract's recognition Journal Entry
	# (linked via government_fee_entry), falling back to contract_date if absent.
	rows = frappe.db.sql(f"""
		SELECT
			{period_expr}      AS period,
			{sort_expr}        AS sort_key,
			COUNT(recog.name)  AS completed,
			SUM(recog.revenue) AS revenue,
			SUM(recog.cogs)    AS cogs
		FROM (
			SELECT
				pc.name AS name,
				COALESCE(je.posting_date, pc.contract_date)    AS recognition_date,
				(pc.selling_price - pc.government_fee_withheld) AS revenue,
				pm.allocated_cost                              AS cogs
			FROM `tabPlot Contract` pc
			INNER JOIN `tabPlot Master` pm ON pm.name = pc.plot
			LEFT JOIN `tabJournal Entry` je ON je.name = pc.government_fee_entry
			WHERE pc.docstatus = 1
			  AND pc.contract_status = 'Completed'
		) recog
		WHERE recog.recognition_date BETWEEN %(from_date)s AND %(to_date)s
		GROUP BY sort_key, period
		ORDER BY sort_key
	""", {"from_date": from_date, "to_date": to_date}, as_dict=True)

	data = []
	grand_completed = 0
	grand_revenue = 0.0
	grand_cogs = 0.0
	for row in rows:
		revenue = flt(row.revenue)
		cogs    = flt(row.cogs)
		margin  = revenue - cogs
		margin_pct = (margin / revenue * 100) if revenue else 0
		grand_completed += int(row.completed or 0)
		grand_revenue   += revenue
		grand_cogs      += cogs
		data.append({
			"period":       row.period,
			"completed":    int(row.completed or 0),
			"revenue":      revenue,
			"cogs":         cogs,
			"gross_margin": margin,
			"margin_pct":   margin_pct,
		})

	if data:
		grand_margin = grand_revenue - grand_cogs
		data.append({
			"period":       "TOTAL",
			"completed":    grand_completed,
			"revenue":      grand_revenue,
			"cogs":         grand_cogs,
			"gross_margin": grand_margin,
			"margin_pct":   (grand_margin / grand_revenue * 100) if grand_revenue else 0,
		})

	return data


def get_chart(data):
	rows = [r for r in data if r.get("period") != "TOTAL"]
	if not rows:
		return None
	return {
		"data": {
			"labels": [r["period"] for r in rows],
			"datasets": [
				{"name": "Revenue Recognized", "values": [r["revenue"]      for r in rows]},
				{"name": "Gross Margin",       "values": [r["gross_margin"] for r in rows]},
			],
		},
		"type":   "bar",
		"colors": ["#1c7ed6", "#2f9e44"],
	}


def get_summary(data):
	rows = [r for r in data if r.get("period") != "TOTAL"]
	if not rows:
		return []
	total_revenue   = sum(flt(r["revenue"])   for r in rows)
	total_cogs      = sum(flt(r["cogs"])      for r in rows)
	total_completed = sum(int(r["completed"]) for r in rows)
	total_margin    = total_revenue - total_cogs
	margin_pct      = (total_margin / total_revenue * 100) if total_revenue else 0
	return [
		{"label": "Contracts Completed", "value": total_completed, "datatype": "Int",      "indicator": "Blue"},
		{"label": "Revenue Recognized",  "value": total_revenue,   "datatype": "Currency", "indicator": "Green"},
		{"label": "Total COGS",          "value": total_cogs,      "datatype": "Currency", "indicator": "Red"},
		{"label": "Gross Margin",        "value": total_margin,    "datatype": "Currency", "indicator": "Green"},
		{"label": "Gross Margin %",      "value": margin_pct,      "datatype": "Percent",  "indicator": "Green" if margin_pct >= 0 else "Red"},
	]
