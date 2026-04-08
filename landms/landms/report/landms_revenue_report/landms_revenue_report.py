import frappe
from frappe.utils import flt, add_months, today


def execute(filters=None):
	filters = filters or {}
	columns = get_columns()
	data = get_data(filters)
	chart = get_chart(data)
	summary = get_summary(data)
	return columns, data, None, chart, summary


def get_columns():
	return [
		{"label": "Period",                "fieldname": "period",          "fieldtype": "Data",    "width": 160},
		{"label": "No. of Payments",       "fieldname": "payment_count",   "fieldtype": "Int",     "width": 140},
		{"label": "Contracts With Payments","fieldname": "contract_count",  "fieldtype": "Int",     "width": 180},
		{"label": "Total Collected (TZS)", "fieldname": "total_collected", "fieldtype": "Float",   "width": 200},
		{"label": "Govt Fee Portion (TZS)","fieldname": "govt_fee",        "fieldtype": "Float",   "width": 190},
		{"label": "Net Revenue (TZS)",     "fieldname": "net_revenue",     "fieldtype": "Float",   "width": 180},
	]


def get_data(filters):
	grouping  = filters.get("grouping")  or "Monthly"
	from_date = filters.get("from_date") or add_months(today(), -6)
	to_date   = filters.get("to_date")   or today()

	if grouping == "Weekly":
		period_expr = "CONCAT('Week ', LPAD(WEEK(pe.posting_date, 1), 2, '0'), ' — ', YEAR(pe.posting_date))"
		sort_expr   = "YEARWEEK(pe.posting_date, 1)"
	else:
		period_expr = "DATE_FORMAT(pe.posting_date, '%%M %%Y')"
		sort_expr   = "DATE_FORMAT(pe.posting_date, '%%Y%%m')"

	rows = frappe.db.sql(f"""
		SELECT
			{period_expr}                                                AS period,
			{sort_expr}                                                  AS sort_key,
			COUNT(DISTINCT pe.name)                                      AS payment_count,
			COUNT(DISTINCT COALESCE(pc.name, so.plot_contract))          AS contract_count,
			SUM(per.allocated_amount)                                    AS total_collected,
			SUM(
				CASE
					WHEN pc.contract_status = 'Completed'
					THEN per.allocated_amount * pc.government_share_percent / 100
					ELSE 0
				END
			)                                                            AS govt_fee
		FROM `tabPayment Entry` pe
		INNER JOIN `tabPayment Entry Reference` per
			ON  per.parent             = pe.name
			AND per.reference_doctype  = 'Sales Invoice'
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

	data = []
	grand_collected = grand_govt = grand_payments = grand_contracts = 0.0

	for row in rows:
		collected = flt(row.total_collected)
		govt      = flt(row.govt_fee)
		net       = collected - govt
		grand_collected  += collected
		grand_govt       += govt
		grand_payments   += row.payment_count
		grand_contracts  += row.contract_count
		data.append({
			"period":          row.period,
			"payment_count":   row.payment_count,
			"contract_count":  row.contract_count,
			"total_collected": collected,
			"govt_fee":        govt,
			"net_revenue":     net,
		})

	if data:
		data.append({
			"period":          "TOTAL",
			"payment_count":   int(grand_payments),
			"contract_count":  int(grand_contracts),
			"total_collected": grand_collected,
			"govt_fee":        grand_govt,
			"net_revenue":     grand_collected - grand_govt,
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
				{"name": "Total Collected",  "values": [r["total_collected"] for r in rows]},
				{"name": "Net Revenue",      "values": [r["net_revenue"]     for r in rows]},
				{"name": "Govt Fee Portion", "values": [r["govt_fee"]        for r in rows]},
			],
		},
		"type":   "bar",
		"colors": ["#2c5f2e", "#4a9e4d", "#f0a500"],
	}


def get_summary(data):
	if not data:
		return []

	total_row = next((r for r in data if r.get("period") == "TOTAL"), None)
	if not total_row:
		return []

	total_collected = flt(total_row.get("total_collected"))
	total_govt_fee = flt(total_row.get("govt_fee"))
	net_revenue = flt(total_row.get("net_revenue"))
	govt_ratio = (total_govt_fee / total_collected * 100) if total_collected else 0

	return [
		{"label": "Total Payments", "value": int(total_row.get("payment_count") or 0), "datatype": "Int", "indicator": "Blue"},
		{"label": "Total Collected", "value": total_collected, "datatype": "Currency", "indicator": "Blue"},
		{"label": "Government Portion", "value": total_govt_fee, "datatype": "Currency", "indicator": "Orange"},
		{"label": "Net Revenue", "value": net_revenue, "datatype": "Currency", "indicator": "Green"},
		{"label": "Government Share %", "value": govt_ratio, "datatype": "Percent", "indicator": "Orange"},
	]
