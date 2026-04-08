import frappe
from frappe.utils import flt


def execute(filters=None):
	metrics = get_metrics()
	columns = get_columns()
	data = get_data(metrics)
	chart = get_chart(metrics)
	report_summary = get_report_summary(metrics)
	return columns, data, None, chart, report_summary


def get_columns():
	return [
		{"label": "Section", "fieldname": "section", "fieldtype": "Data", "width": 120},
		{"label": "KPI", "fieldname": "kpi", "fieldtype": "Data", "width": 300},
		{"label": "Value", "fieldname": "value", "fieldtype": "Data", "width": 220},
		{"label": "Notes", "fieldname": "notes", "fieldtype": "Data", "width": 350},
	]


def get_metrics():
	# ── Plot counts ──────────────────────────────────────────────────────
	plot_counts = frappe.db.sql("""
		SELECT status, COUNT(name) AS cnt
		FROM `tabPlot Master`
		WHERE docstatus = 1
		GROUP BY status
	""", as_dict=True)
	plot_map = {r.status: r.cnt for r in plot_counts}
	available = plot_map.get("Available", 0)
	pending_fee = plot_map.get("Pending Fee", 0)
	pending_advance = plot_map.get("Pending Advance", 0)
	reserved = plot_map.get("Reserved", 0)
	ready_for_handover = plot_map.get("Ready for Handover", 0)
	delivered = plot_map.get("Delivered", 0)
	total_plots = sum(plot_map.values())

	# ── Contract financials ───────────────────────────────────────────────
	fin = frappe.db.sql("""
		SELECT
			COUNT(name) AS contracts_total,
			SUM(CASE WHEN contract_status = 'Draft' THEN 1 ELSE 0 END) AS draft_contracts,
			SUM(CASE WHEN contract_status = 'Ongoing' THEN 1 ELSE 0 END) AS ongoing_contracts,
			SUM(CASE WHEN contract_status = 'Completed' THEN 1 ELSE 0 END) AS completed_contracts,
			SUM(CASE WHEN contract_status = 'Terminated' THEN 1 ELSE 0 END) AS terminated_contracts,
			SUM(total_paid) AS cash_collected,
			SUM(CASE WHEN contract_status = 'Ongoing' THEN total_paid ELSE 0 END) AS deferred_revenue,
			SUM(CASE WHEN contract_status = 'Completed' THEN selling_price ELSE 0 END) AS recognized_gross,
			SUM(CASE WHEN contract_status = 'Completed' THEN government_fee_withheld ELSE 0 END) AS govt_fees,
			SUM(CASE WHEN contract_status IN ('Ongoing','Completed') THEN selling_price ELSE 0 END) AS active_pipeline
		FROM `tabPlot Contract`
		WHERE docstatus = 1
	""", as_dict=True)[0]

	# ── COGS on completed plots ─────────────────────────────────────────
	cogs_row = frappe.db.sql("""
		SELECT SUM(pm.allocated_cost) AS total_cogs
		FROM `tabPlot Contract` pc
		INNER JOIN `tabPlot Master` pm ON pm.name = pc.plot
		WHERE pc.docstatus = 1 AND pc.contract_status = 'Completed'
	""", as_dict=True)[0]

	cash_collected = flt(fin.cash_collected)
	deferred = flt(fin.deferred_revenue)
	active_pipeline = flt(fin.active_pipeline)
	recognized_gross = flt(fin.recognized_gross)
	govt_fees = flt(fin.govt_fees)
	revenue_recog = recognized_gross - govt_fees
	cogs = flt(cogs_row.total_cogs)
	gross_margin = revenue_recog - cogs
	margin_pct = (gross_margin / revenue_recog * 100) if revenue_recog else 0
	collection_rate = (cash_collected / active_pipeline * 100) if active_pipeline else 0

	return {
		"total_plots": total_plots,
		"available": available,
		"pending_fee": pending_fee,
		"pending_advance": pending_advance,
		"reserved": reserved,
		"ready_for_handover": ready_for_handover,
		"delivered": delivered,
		"contracts_total": int(fin.contracts_total or 0),
		"draft_contracts": int(fin.draft_contracts or 0),
		"ongoing_contracts": int(fin.ongoing_contracts or 0),
		"completed_contracts": int(fin.completed_contracts or 0),
		"terminated_contracts": int(fin.terminated_contracts or 0),
		"cash_collected": cash_collected,
		"deferred_revenue": deferred,
		"active_pipeline": active_pipeline,
		"recognized_revenue": revenue_recog,
		"govt_fees": govt_fees,
		"cogs": cogs,
		"gross_margin": gross_margin,
		"margin_pct": margin_pct,
		"collection_rate": collection_rate,
	}


def get_data(metrics):
	def tzs(n):
		return f"TZS {flt(n):,.0f}"

	def pct(n):
		return f"{flt(n):.1f}%"

	return [
		{
			"section": "PLOTS",
			"kpi": "Available Plots",
			"value": str(metrics["available"]),
			"notes": "Ready for new applications",
		},
		{
			"section": "PLOTS",
			"kpi": "Pending Fee Plots",
			"value": str(metrics["pending_fee"]),
			"notes": "Application submitted, waiting for application-fee payment",
		},
		{
			"section": "PLOTS",
			"kpi": "Pending Advance Plots",
			"value": str(metrics["pending_advance"]),
			"notes": "Fee paid, waiting for first advance",
		},
		{
			"section": "PLOTS",
			"kpi": "Reserved Plots",
			"value": str(metrics["reserved"]),
			"notes": "Linked to active sales/contracts",
		},
		{
			"section": "PLOTS",
			"kpi": "Ready for Handover",
			"value": str(metrics["ready_for_handover"]),
			"notes": "Fully paid, waiting for physical handover",
		},
		{
			"section": "PLOTS",
			"kpi": "Delivered Plots",
			"value": str(metrics["delivered"]),
			"notes": "Fully paid and handed over",
		},
		{
			"section": "PLOTS",
			"kpi": "Total Plots",
			"value": str(metrics["total_plots"]),
			"notes": "All submitted plots in inventory",
		},
		{
			"section": "CONTRACTS",
			"kpi": "Draft Contracts",
			"value": str(metrics["draft_contracts"]),
			"notes": "Created but not active yet",
		},
		{
			"section": "CONTRACTS",
			"kpi": "Ongoing Contracts",
			"value": str(metrics["ongoing_contracts"]),
			"notes": "Active collection lifecycle",
		},
		{
			"section": "CONTRACTS",
			"kpi": "Completed Contracts",
			"value": str(metrics["completed_contracts"]),
			"notes": "Eligible for revenue recognition",
		},
		{
			"section": "CONTRACTS",
			"kpi": "Terminated Contracts",
			"value": str(metrics["terminated_contracts"]),
			"notes": "Stopped before full completion",
		},
		{
			"section": "FINANCIALS",
			"kpi": "Cash Collected",
			"value": tzs(metrics["cash_collected"]),
			"notes": "All customer collections to date",
		},
		{
			"section": "FINANCIALS",
			"kpi": "Deferred Revenue",
			"value": tzs(metrics["deferred_revenue"]),
			"notes": "Cash received on ongoing contracts",
		},
		{
			"section": "FINANCIALS",
			"kpi": "Revenue Recognized",
			"value": tzs(metrics["recognized_revenue"]),
			"notes": "Completed contracts net of government share",
		},
		{
			"section": "FINANCIALS",
			"kpi": "Government Fees Payable",
			"value": tzs(metrics["govt_fees"]),
			"notes": "Liability on completed contracts",
		},
		{
			"section": "FINANCIALS",
			"kpi": "COGS (Completed Contracts)",
			"value": tzs(metrics["cogs"]),
			"notes": "Allocated plot costs for completed contracts",
		},
		{
			"section": "FINANCIALS",
			"kpi": "Gross Margin",
			"value": tzs(metrics["gross_margin"]),
			"notes": "Revenue recognized minus completed-contract COGS",
		},
		{
			"section": "RATIOS",
			"kpi": "Gross Margin %",
			"value": pct(metrics["margin_pct"]),
			"notes": "Gross margin divided by revenue recognized",
		},
		{
			"section": "RATIOS",
			"kpi": "Collection Rate %",
			"value": pct(metrics["collection_rate"]),
			"notes": "Cash collected divided by active pipeline",
		},
	]


def get_chart(metrics):
	return {
		"data": {
				"labels": [
					"Available Plots",
					"Pending Fee Plots",
					"Pending Advance Plots",
					"Reserved Plots",
					"Ready for Handover",
					"Delivered Plots",
				"Draft Contracts",
				"Ongoing Contracts",
				"Completed Contracts",
				"Terminated Contracts",
			],
			"datasets": [
				{
					"name": "Count",
					"values": [
						metrics["available"],
						metrics["pending_fee"],
						metrics["pending_advance"],
						metrics["reserved"],
						metrics["ready_for_handover"],
						metrics["delivered"],
						metrics["draft_contracts"],
						metrics["ongoing_contracts"],
						metrics["completed_contracts"],
						metrics["terminated_contracts"],
					],
				}
			],
		},
		"type": "bar",
		"colors": ["#2c5f2e"],
	}


def get_report_summary(metrics):
	margin_indicator = "Green" if metrics["margin_pct"] >= 0 else "Red"
	collection_indicator = "Green" if metrics["collection_rate"] >= 70 else "Orange"
	return [
		{
			"label": "Total Plots",
			"value": metrics["total_plots"],
			"datatype": "Int",
			"indicator": "Blue",
		},
		{
			"label": "Available Plots",
			"value": metrics["available"],
			"datatype": "Int",
			"indicator": "Green",
		},
		{
			"label": "Pending Advance",
			"value": metrics["pending_advance"],
			"datatype": "Int",
			"indicator": "Yellow",
		},
		{
			"label": "Pending Fee",
			"value": metrics["pending_fee"],
			"datatype": "Int",
			"indicator": "Orange",
		},
		{
			"label": "Reserved Plots",
			"value": metrics["reserved"],
			"datatype": "Int",
			"indicator": "Orange",
		},
		{
			"label": "Ready for Handover",
			"value": metrics["ready_for_handover"],
			"datatype": "Int",
			"indicator": "Cyan",
		},
		{
			"label": "Ongoing Contracts",
			"value": metrics["ongoing_contracts"],
			"datatype": "Int",
			"indicator": "Orange",
		},
		{
			"label": "Cash Collected (TZS)",
			"value": metrics["cash_collected"],
			"datatype": "Float",
			"indicator": "Blue",
		},
		{
			"label": "Revenue Recognized (TZS)",
			"value": metrics["recognized_revenue"],
			"datatype": "Float",
			"indicator": "Green",
		},
		{
			"label": "Gross Margin %",
			"value": metrics["margin_pct"],
			"datatype": "Percent",
			"indicator": margin_indicator,
		},
		{
			"label": "Collection Rate %",
			"value": metrics["collection_rate"],
			"datatype": "Percent",
			"indicator": collection_indicator,
		},
	]
