"""The Customer History tab on a plot (PM-7): every customer who took the plot, newest first.

Read-only and stored nowhere: built each time from the plot's applications, orders, contracts
and handovers, plus money moved off them (Unreconcile Payment, or a journal that only shifts
money between customer balances). Moves made by hand in other ways leave no such record and are
not shown. Each line says what happened in one or two words (operator, 5 Nov 2026: keep it short).

Each viewer sees only what they may read: documents through permission-aware lists (role and
user permissions, e.g. customer group), payment moves only with read on Payment Entry, journal
moves only with read on Journal Entry. The customer is shown by its ID, which is on the document.
"""

import frappe

# plot statuses that mean someone holds the plot
PLOT_HELD = ("Pending Fee", "Pending Advance", "Reserved", "Ready for Handover", "Delivered")
# outcomes that mean the customer still holds the plot
HOLDING = ("Applied", "Paying", "Overdue", "Paid off", "Handed over")
CONTRACT_OUTCOME = {
	"Completed": "Paid off",
	"Ongoing": "Paying",
	"Overdue": "Overdue",
	"Terminated": "Terminated",
}


@frappe.whitelist(methods=["POST"])
def get_customer_history(plot: str):
	"""Lines for the plot's Customer History tab, newest first, the current holder marked."""
	frappe.has_permission("Plot Master", "read", plot, throw=True)
	lines = _turns(plot)
	may_read_payments = frappe.has_permission("Payment Entry", "read")
	may_read_journals = frappe.has_permission("Journal Entry", "read")
	_add_moves(lines, payments=may_read_payments, journals=may_read_journals)
	lines.sort(key=lambda line: str(line["from"] or ""), reverse=True)
	# "Current" only when the plot itself is held, so the tab never contradicts the plot
	plot_held = frappe.db.get_value("Plot Master", plot, "status") in PLOT_HELD
	current = next((line for line in lines if line["outcome"] in HOLDING), None) if plot_held else None
	return [
		{
			"customer": line["customer"],
			"from": line["from"],
			"outcome": line["outcome"],
			"current": line is current,
			"documents": line["documents"],
			"moves": line["moves"],
		}
		for line in lines
	]


def _turns(plot):
	"""One line per application with the order, contract and handover that followed it.

	An order or contract that no application leads to gets a line of its own.
	"""
	live = {"plot": plot, "docstatus": ["!=", 0]}
	turns = {}
	for app in _readable(
		"Plot Application",
		filters=live,
		fields=["name", "customer", "status", "docstatus", "application_date"],
	):
		turns[app.name] = _turn(
			app.customer, app.application_date, app=app, documents=[("Plot Application", app.name)]
		)
	for order in _readable(
		"Sales Order",
		filters=live,
		fields=["name", "customer", "status", "docstatus", "transaction_date", "plot_application"],
	):
		turn = turns.get(order.plot_application) or turns.setdefault(
			order.name, _turn(order.customer, order.transaction_date)
		)
		turn["orders"].append(order)
		turn["documents"].append(("Sales Order", order.name))
	by_order = {order.name: turn for turn in turns.values() for order in turn["orders"]}
	for contract in _readable(
		"Plot Contract",
		filters=live,
		fields=[
			"name",
			"customer",
			"contract_status",
			"docstatus",
			"sales_order",
			"plot_application",
			"contract_date",
		],
	):
		turn = (
			by_order.get(contract.sales_order)
			or turns.get(contract.plot_application)
			or turns.setdefault(contract.name, _turn(contract.customer, contract.contract_date))
		)
		turn["contracts"].append(contract)
		turn["documents"].append(("Plot Contract", contract.name))
	by_contract = {contract.name: turn for turn in turns.values() for contract in turn["contracts"]}
	for handover in _readable(
		"Plot Handover", filters={"plot": plot, "docstatus": 1}, fields=["name", "contract"]
	):
		turn = by_contract.get(handover.contract)
		if turn:
			turn["handed_over"] = True
			turn["documents"].append(("Plot Handover", handover.name))
	lines = list(turns.values())
	for line in lines:
		line["outcome"] = _outcome(line)
	return lines


def _readable(doctype, filters, fields):
	"""The rows of a record type the viewer may read; none if they may not read that type at all.

	frappe.get_list applies role and user permissions but refuses outright when the viewer has no
	read on the whole type (e.g. a Finance Director on Sales Order), which would break the tab.
	"""
	may_read = frappe.has_permission(doctype, "read")
	return frappe.get_list(doctype, filters=filters, fields=fields) if may_read else []


def _turn(customer, start, app=None, documents=None):
	return {
		"customer": customer,
		"from": start,
		"app": app,
		"orders": [],
		"contracts": [],
		"handed_over": False,
		"documents": documents or [],
		"moves": [],
	}


def _outcome(line):
	"""One or two words for what came of this customer's turn on the plot."""
	if line["handed_over"]:
		return "Handed over"
	live_contracts = [c for c in line["contracts"] if c.docstatus == 1]
	if live_contracts:
		return CONTRACT_OUTCOME.get(live_contracts[-1].contract_status, "Paying")
	live_orders = [o for o in line["orders"] if o.docstatus == 1]
	if live_orders:
		return "Forfeited" if live_orders[-1].status == "Closed" else "Paying"
	app = line["app"]
	if app and app.docstatus == 1 and app.status in ("Submitted", "Paid", "Converted"):
		return "Applied"
	if app and app.status == "Expired":
		return "Expired"
	return "Cancelled"  # a cancelled application, whatever its status field still says


def _add_moves(lines, payments=True, journals=True):
	"""Money moved off one turn and on to another document, shown on both sides.

	Two ways a move leaves a record: a payment taken off an invoice with ERPNext's Unreconcile
	Payment, and a journal that only shifts money between customer balances (a plot exchange).
	"""
	for line in lines:
		orders = {order.name for order in line["orders"]}
		invoices = set(_invoices_of(orders))
		if invoices and payments:
			_payment_moves(line, orders, invoices)
		if invoices and journals:
			_journal_moves(line, invoices)


def _payment_moves(line, orders, invoices):
	"""Payments taken off this turn's invoices (out), or now on them after leaving another (in)."""
	for undone in _unlinked(invoices=invoices):
		now_on = sorted(_where_paid(undone.payment) - {("Sales Order", o) for o in orders})
		line["moves"].append(_move("out", undone.amount, now_on[0] if now_on else None))
	paid_by = frappe.get_all(
		"Payment Entry Reference",
		filters={"reference_name": ["in", list(invoices)], "docstatus": 1},
		pluck="parent",
	)
	for undone in _unlinked(payments=paid_by):
		if undone.invoice not in invoices:
			line["moves"].append(_move("in", undone.amount, ("Sales Order", _order_of(undone.invoice))))


def _journal_moves(line, invoices):
	"""Journals that only shift money between customer balances, against this turn's invoices."""
	for row in frappe.get_all(
		"Journal Entry Account",
		filters={"reference_type": "Sales Invoice", "reference_name": ["in", list(invoices)], "docstatus": 1},
		fields=["parent", "debit", "credit"],
	):
		if _is_money_move(row.parent):
			direction = "in" if row.credit else "out"
			line["moves"].append(_move(direction, row.credit or row.debit, ("Journal Entry", row.parent)))


def _move(direction, amount, target):
	return {
		"direction": direction,
		"amount": amount,
		"doctype": target[0] if target else None,
		"name": target[1] if target else None,
	}


def _is_money_move(journal):
	"""A journal that only shifts money between customer balances (every line a Receivable account)."""
	accounts = frappe.get_all("Journal Entry Account", filters={"parent": journal}, pluck="account")
	types = set(frappe.get_all("Account", filters={"name": ["in", accounts]}, pluck="account_type"))
	return types == {"Receivable"}


def _unlinked(invoices=None, payments=None):
	"""Allocations actually removed with Unreconcile Payment: the payment, the invoice it left, the amount."""
	filters = {"docstatus": 1, "unlinked": 1, "reference_doctype": "Sales Invoice"}
	if invoices is not None:
		filters["reference_name"] = ["in", list(invoices)]
	if payments is not None:
		parents = (
			frappe.get_all(
				"Unreconcile Payment",
				filters={"voucher_no": ["in", list(set(payments))], "docstatus": 1},
				pluck="name",
			)
			if payments
			else []
		)
		if not parents:
			return []
		filters["parent"] = ["in", parents]
	rows = frappe.get_all(
		"Unreconcile Payment Entries",
		filters=filters,
		fields=["parent", "reference_name", "allocated_amount"],
	)
	if not rows:
		return []
	payment_of = dict(
		frappe.get_all(
			"Unreconcile Payment",
			filters={"name": ["in", list({r.parent for r in rows})]},
			fields=["name", "voucher_no"],
			as_list=True,
		)
	)
	return [
		frappe._dict(payment=payment_of[r.parent], invoice=r.reference_name, amount=r.allocated_amount)
		for r in rows
	]


def _invoices_of(orders):
	return (
		frappe.get_all("Sales Invoice Item", filters={"sales_order": ["in", list(orders)]}, pluck="parent")
		if orders
		else []
	)


def _order_of(invoice):
	return frappe.db.get_value("Sales Invoice Item", {"parent": invoice}, "sales_order")


def _where_paid(payment):
	"""What a payment is allocated to today: orders (directly or through their invoices) or a journal."""
	targets = set()
	for ref in frappe.get_all(
		"Payment Entry Reference",
		filters={"parent": payment, "docstatus": 1},
		fields=["reference_doctype", "reference_name"],
	):
		if ref.reference_doctype == "Sales Invoice":
			order = _order_of(ref.reference_name)
			targets.add(("Sales Order", order) if order else ("Sales Invoice", ref.reference_name))
		else:
			targets.add((ref.reference_doctype, ref.reference_name))
	return targets
