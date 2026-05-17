import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import today, flt, cint


class LandAcquisition(Document):
    def validate(self):
        self._validate_area()
        self._validate_coordinates()
        self._validate_sales_defaults()
        self._validate_plot_type_rates()

    def before_submit(self):
        if self.status != "Approved":
            frappe.throw(
                _("Land Acquisition must be Approved through the workflow before submission.")
            )
        self.approval_date = today()
        self.approved_by = frappe.session.user

    def on_submit(self):
        sync_land_acquisition_cost_summary(self.name)
        sync_land_acquisition_plot_summary(self.name)

    def before_cancel(self):
        self._block_if_active_plots()
        self._block_or_cancel_purchase_documents()

    def _block_if_active_plots(self):
        if not frappe.db.exists("DocType", "Plot Master"):
            return
        active_plots = frappe.db.count(
            "Plot Master", {"land_acquisition": self.name, "docstatus": 1}
        )
        if not active_plots:
            return
        sample = frappe.db.get_all(
            "Plot Master",
            filters={"land_acquisition": self.name, "docstatus": 1},
            fields=["name"],
            limit_page_length=3,
        )
        names = ", ".join(r.name for r in sample)
        extra = (
            f" and {active_plots - len(sample)} more"
            if active_plots > len(sample)
            else ""
        )
        frappe.throw(
            _(
                "Cannot cancel — submitted plots still exist: {0}{1}. "
                "Cancel those Plot Master records first."
            ).format(names, extra)
        )

    def _block_or_cancel_purchase_documents(self):
        """Block cancellation if any PI or PO has payments against it.
        Auto-cancel unpaid submitted PIs and POs so the LA can be cancelled cleanly.
        """
        for doctype, item_doctype in (
            ("Purchase Invoice", "Purchase Invoice Item"),
            ("Purchase Order", "Purchase Order Item"),
        ):
            linked = frappe.db.sql(
                f"""
                SELECT DISTINCT p.name
                FROM `tab{doctype}` p
                INNER JOIN `tab{item_doctype}` pi ON pi.parent = p.name
                WHERE pi.land_acquisition = %s AND p.docstatus = 1
                """,
                self.name,
                as_dict=True,
            )

            for row in linked:
                has_payment = frappe.db.sql(
                    """
                    SELECT COUNT(*) FROM `tabPayment Entry Reference` per
                    INNER JOIN `tabPayment Entry` pe ON pe.name = per.parent
                    WHERE per.reference_doctype = %s
                      AND per.reference_name = %s
                      AND pe.docstatus = 1
                    """,
                    (doctype, row.name),
                )[0][0]

                if has_payment:
                    frappe.throw(
                        f"Cannot cancel — {doctype} {row.name} has payments posted. "
                        "Cancel the Payment Entry first."
                    )

            for row in linked:
                doc = frappe.get_doc(doctype, row.name)
                doc.flags.ignore_permissions = True
                doc.cancel()

    def on_cancel(self):
        self.db_set("status", "Cancelled")
        sync_land_acquisition_cost_summary(self.name)
        sync_land_acquisition_plot_summary(self.name)
        self._clear_land_acquisition_references()

    def _clear_land_acquisition_references(self):
        """Clear the land_acquisition dimension from all linked doctypes after cancel.

        Allows the cancelled LA to be deleted without ERPNext's link-check blocking it.
        land_acquisition is a reporting dimension — clearing it does not affect
        debit/credit amounts or financial totals.
        """
        doctypes = [
            "GL Entry",
            "Payment Ledger Entry",
            "Purchase Invoice",
            "Purchase Invoice Item",
            "Purchase Order",
            "Purchase Order Item",
            "Payment Entry",
            "Journal Entry Account",
            "Stock Entry",
            "Stock Entry Detail",
            "Purchase Receipt",
            "Purchase Receipt Item",
            "Sales Invoice",
            "Sales Invoice Item",
            "Sales Order",
            "Sales Order Item",
            "Delivery Note",
            "Delivery Note Item",
        ]
        for doctype in doctypes:
            try:
                names = frappe.get_all(
                    doctype,
                    filters={"land_acquisition": self.name},
                    pluck="name",
                )
                for name in names:
                    frappe.db.set_value(
                        doctype, name, "land_acquisition", None, update_modified=False
                    )
            except Exception:
                pass

    # ── Private validators ───────────────────────────────────────────────────

    def _validate_area(self):
        if not self.total_area_sqm or flt(self.total_area_sqm) <= 0:
            frappe.throw(_("Total Area must be greater than zero."))

    def _validate_coordinates(self):
        has_lat = self.latitude not in (None, "")
        has_lng = self.longitude not in (None, "")
        if has_lat != has_lng:
            frappe.throw(_("Enter both Latitude and Longitude, or leave both blank."))
        if has_lat:
            lat = flt(self.latitude)
            lng = flt(self.longitude)
            if lat < -90 or lat > 90:
                frappe.throw(_("Latitude must be between -90 and 90."))
            if lng < -180 or lng > 180:
                frappe.throw(_("Longitude must be between -180 and 180."))

    def _validate_sales_defaults(self):
        if not (0 <= flt(self.booking_fee_percent) <= 100):
            frappe.throw(_("Booking Fee % must be between 0 and 100."))
        if not (0 <= flt(self.government_share_percent) <= 100):
            frappe.throw(_("Government Share % must be between 0 and 100."))
        if cint(self.payment_completion_days) <= 0:
            frappe.throw(_("Payment Completion Days must be greater than zero."))

    def _validate_plot_type_rates(self):
        seen = set()
        for row in self.get("plot_type_rates") or []:
            if not row.plot_type:
                frappe.throw(f"Row {row.idx} in Plot Type Rates is missing a Plot Type.")
            if row.plot_type in seen:
                frappe.throw(
                    f"Plot Type '{row.plot_type}' appears more than once in the rates table. "
                    "Each plot type can only have one rate per Land Acquisition."
                )
            seen.add(row.plot_type)


# =============================================================================
# Cost summary — single source of truth for PO/PI/PE rollups per supplier
# =============================================================================

@frappe.whitelist()
def sync_land_acquisition_cost_summary(land_acquisition):
    """Recompute the cost summary and persist totals on the Land Acquisition."""
    if not land_acquisition or not frappe.db.exists("Land Acquisition", land_acquisition):
        return {}

    summary = get_land_acquisition_cost_summary(land_acquisition)
    t = summary["totals"]
    frappe.db.set_value(
        "Land Acquisition",
        land_acquisition,
        {
            "acquisition_cost_tzs": t["acquisition_cost_tzs"],
            "cost_per_sqm_tzs": t["cost_per_sqm_tzs"],
            "total_committed_tzs": t["committed"],
            "total_paid_tzs": t["paid"],
            "total_outstanding_tzs": t["outstanding"],
            "total_unbilled_po_tzs": t["unbilled_po"],
        },
        update_modified=False,
    )
    return summary


@frappe.whitelist()
def get_land_acquisition_cost_summary(land_acquisition):
    """Return the consolidated PO/PI/PE rollup for one Land Acquisition.

    Shape:
        {
          "land_acquisition": str,
          "total_area_sqm":   float,
          "lud_account":      str,
          "sellers":          [supplier_row, ...],
          "others":           [supplier_row, ...],
          "totals":           { ...numbers... },
        }

    supplier_row:
        {
          "supplier": str, "supplier_name": str, "is_land_seller": int,
          "committed":   float,  # PO total tagged to this LA
          "billed":      float,  # PI total tagged to this LA hitting LUD
          "paid":        float,  # PE allocations against those PIs
          "outstanding": float,  # billed - paid
          "unbilled_po": float,  # max(committed - billed, 0)
          "po_docs": [...], "pi_docs": [...], "pe_docs": [...],
        }
    """
    if not land_acquisition or not frappe.db.exists("Land Acquisition", land_acquisition):
        return _empty_summary(land_acquisition)

    la = frappe.db.get_value(
        "Land Acquisition",
        land_acquisition,
        ["company", "total_area_sqm"],
        as_dict=True,
    )
    lud_account = _get_lud_account(la.company)

    po_rows = _fetch_committed(land_acquisition)
    pi_rows = _fetch_billed(land_acquisition, lud_account)
    pe_rows, paid_per_pi = _fetch_paid([r["pi"] for r in pi_rows])
    advance_rows = _fetch_paid_advance(land_acquisition)

    # Annotate per-PI paid/outstanding for the drill-down view
    for row in pi_rows:
        row["paid"] = flt(paid_per_pi.get(row["pi"], 0))
        row["outstanding"] = flt(row["amount"]) - row["paid"]

    suppliers = _build_supplier_rollup(po_rows, pi_rows, pe_rows, advance_rows)
    sellers = [s for s in suppliers if s["is_land_seller"]]
    others = [s for s in suppliers if not s["is_land_seller"]]
    totals = _compute_totals(sellers, others, flt(la.total_area_sqm))

    return {
        "land_acquisition": land_acquisition,
        "total_area_sqm": flt(la.total_area_sqm),
        "lud_account": lud_account,
        "sellers": sellers,
        "others": others,
        "totals": totals,
    }


def _get_lud_account(company):
    """Resolve the Land Under Development account for the given company."""
    account = frappe.db.get_single_value(
        "LandMS Settings", "land_under_development_account"
    )
    if account:
        return account
    # Fallback by account number on the company
    return frappe.db.get_value(
        "Account",
        {"company": company, "account_number": "1411"},
        "name",
    )


def _fetch_committed(la_name):
    """Submitted Purchase Order items tagged to this Land Acquisition."""
    if not frappe.db.has_column("Purchase Order Item", "land_acquisition"):
        return []
    return frappe.db.sql(
        """
        SELECT
            po.name             AS po,
            po.supplier         AS supplier,
            po.transaction_date AS date,
            po.status           AS status,
            SUM(poi.base_net_amount) AS amount
        FROM `tabPurchase Order Item` poi
        INNER JOIN `tabPurchase Order` po ON po.name = poi.parent
        WHERE poi.land_acquisition = %(la)s
          AND po.docstatus = 1
        GROUP BY po.name
        HAVING ABS(SUM(poi.base_net_amount)) > 0.0001
        ORDER BY po.transaction_date DESC, po.creation DESC
        """,
        {"la": la_name},
        as_dict=True,
    )


def _fetch_billed(la_name, lud_account):
    """Submitted Purchase Invoice items tagged to this LA hitting the LUD account."""
    if not frappe.db.has_column("Purchase Invoice Item", "land_acquisition"):
        return []
    if not lud_account:
        return []
    return frappe.db.sql(
        """
        SELECT
            pi.name           AS pi,
            pi.supplier       AS supplier,
            pi.posting_date   AS date,
            pi.status         AS status,
            SUM(pii.base_net_amount) AS amount
        FROM `tabPurchase Invoice Item` pii
        INNER JOIN `tabPurchase Invoice` pi ON pi.name = pii.parent
        WHERE pii.land_acquisition = %(la)s
          AND pi.docstatus = 1
          AND pii.expense_account = %(lud)s
        GROUP BY pi.name
        HAVING ABS(SUM(pii.base_net_amount)) > 0.0001
        ORDER BY pi.posting_date DESC, pi.creation DESC
        """,
        {"la": la_name, "lud": lud_account},
        as_dict=True,
    )


def _fetch_paid(pi_names):
    """Payment Entry allocations against the given PI names.

    Returns (pe_rows, {pi_name: total_paid}).
    """
    if not pi_names:
        return [], {}
    rows = frappe.db.sql(
        """
        SELECT
            pe.name            AS pe,
            pe.party           AS supplier,
            pe.posting_date    AS date,
            per.reference_name AS pi,
            SUM(per.allocated_amount) AS amount
        FROM `tabPayment Entry Reference` per
        INNER JOIN `tabPayment Entry` pe ON pe.name = per.parent
        WHERE per.reference_doctype = 'Purchase Invoice'
          AND per.reference_name IN %(pis)s
          AND pe.docstatus = 1
        GROUP BY pe.name, per.reference_name
        ORDER BY pe.posting_date DESC, pe.creation DESC
        """,
        {"pis": tuple(pi_names)},
        as_dict=True,
    )
    paid_per_pi = {}
    for r in rows:
        paid_per_pi[r["pi"]] = paid_per_pi.get(r["pi"], 0) + flt(r["amount"])
    return rows, paid_per_pi


def _fetch_paid_advance(la_name):
    """Payment Entry allocations made directly against Purchase Orders tagged
    to this Land Acquisition (i.e. supplier advances paid before any PI exists).

    A single PO can hold items for multiple Land Acquisitions, so each
    allocation is pro-rated by this LA's share of the PO total. The result
    looks the same shape as `_fetch_paid` rows so the rollup can consume both.
    """
    if not frappe.db.has_column("Purchase Order Item", "land_acquisition"):
        return []
    rows = frappe.db.sql(
        """
        SELECT
            pe.name             AS pe,
            pe.party            AS supplier,
            pe.posting_date     AS date,
            per.reference_name  AS po,
            per.allocated_amount AS allocated,
            (
                SELECT SUM(poi.base_net_amount)
                FROM `tabPurchase Order Item` poi
                WHERE poi.parent = per.reference_name
                  AND poi.land_acquisition = %(la)s
            ) AS la_share,
            (
                SELECT SUM(poi.base_net_amount)
                FROM `tabPurchase Order Item` poi
                WHERE poi.parent = per.reference_name
            ) AS po_total
        FROM `tabPayment Entry Reference` per
        INNER JOIN `tabPayment Entry` pe ON pe.name = per.parent
        WHERE per.reference_doctype = 'Purchase Order'
          AND pe.docstatus = 1
          AND EXISTS (
              SELECT 1 FROM `tabPurchase Order Item` poi
              WHERE poi.parent = per.reference_name
                AND poi.land_acquisition = %(la)s
          )
        ORDER BY pe.posting_date DESC, pe.creation DESC
        """,
        {"la": la_name},
        as_dict=True,
    )

    result = []
    for r in rows:
        po_total = flt(r["po_total"])
        ratio = (flt(r["la_share"]) / po_total) if po_total else 0.0
        amount = flt(r["allocated"]) * ratio
        if abs(amount) <= 0.0001:
            continue
        result.append({
            "pe": r["pe"],
            "supplier": r["supplier"],
            "date": r["date"],
            "po": r["po"],
            "amount": amount,
        })
    return result


def _build_supplier_rollup(po_rows, pi_rows, pe_rows, advance_rows=None):
    """Aggregate per-supplier metrics from the three doc-type row sets."""
    advance_rows = advance_rows or []
    supplier_names = (
        {r["supplier"] for r in po_rows}
        | {r["supplier"] for r in pi_rows}
        | {r["supplier"] for r in pe_rows}
        | {r["supplier"] for r in advance_rows}
    )
    supplier_meta = {}
    if supplier_names:
        meta_rows = frappe.db.get_all(
            "Supplier",
            filters={"name": ("in", list(supplier_names))},
            fields=["name", "supplier_name", "is_land_seller"],
        )
        supplier_meta = {r.name: r for r in meta_rows}

    suppliers = {}

    def _row(name):
        if name not in suppliers:
            meta = supplier_meta.get(name) or frappe._dict()
            suppliers[name] = {
                "supplier": name,
                "supplier_name": meta.get("supplier_name") or name,
                "is_land_seller": int(meta.get("is_land_seller") or 0),
                "committed": 0.0,
                "billed": 0.0,
                "paid": 0.0,
                "outstanding": 0.0,
                "unbilled_po": 0.0,
                "po_docs": [],
                "pi_docs": [],
                "pe_docs": [],
            }
        return suppliers[name]

    for r in po_rows:
        s = _row(r["supplier"])
        s["committed"] += flt(r["amount"])
        s["po_docs"].append({
            "name": r["po"],
            "amount": flt(r["amount"]),
            "date": str(r["date"]) if r["date"] else None,
            "status": r["status"],
        })

    for r in pi_rows:
        s = _row(r["supplier"])
        s["billed"] += flt(r["amount"])
        s["paid"] += flt(r["paid"])
        s["pi_docs"].append({
            "name": r["pi"],
            "amount": flt(r["amount"]),
            "paid": flt(r["paid"]),
            "outstanding": flt(r["outstanding"]),
            "date": str(r["date"]) if r["date"] else None,
            "status": r["status"],
        })

    for r in pe_rows:
        s = _row(r["supplier"])
        s["pe_docs"].append({
            "name": r["pe"],
            "amount": flt(r["amount"]),
            "date": str(r["date"]) if r["date"] else None,
            "pi": r["pi"],
            "against": "pi",
        })

    for r in advance_rows:
        s = _row(r["supplier"])
        s["paid"] += flt(r["amount"])
        s["pe_docs"].append({
            "name": r["pe"],
            "amount": flt(r["amount"]),
            "date": str(r["date"]) if r["date"] else None,
            "po": r["po"],
            "against": "po",
        })

    for s in suppliers.values():
        s["outstanding"] = max(s["billed"] - s["paid"], 0.0)
        s["unbilled_po"] = max(s["committed"] - s["billed"], 0.0)

    return sorted(
        suppliers.values(),
        key=lambda s: (-s["is_land_seller"], -(s["committed"] + s["billed"])),
    )


def _compute_totals(sellers, others, total_area_sqm):
    def _sum(rows, key):
        return sum(flt(r[key]) for r in rows)

    seller_billed = _sum(sellers, "billed")
    other_billed = _sum(others, "billed")
    acquisition_cost = seller_billed + other_billed
    cost_per_sqm = (acquisition_cost / total_area_sqm) if total_area_sqm else 0.0

    return {
        # split by supplier type
        "seller_committed": _sum(sellers, "committed"),
        "seller_billed": seller_billed,
        "seller_paid": _sum(sellers, "paid"),
        "seller_outstanding": _sum(sellers, "outstanding"),
        "seller_unbilled_po": _sum(sellers, "unbilled_po"),
        "other_committed": _sum(others, "committed"),
        "other_billed": other_billed,
        "other_paid": _sum(others, "paid"),
        "other_outstanding": _sum(others, "outstanding"),
        "other_unbilled_po": _sum(others, "unbilled_po"),
        # grand totals
        "committed": _sum(sellers, "committed") + _sum(others, "committed"),
        "billed": acquisition_cost,
        "paid": _sum(sellers, "paid") + _sum(others, "paid"),
        "outstanding": _sum(sellers, "outstanding") + _sum(others, "outstanding"),
        "unbilled_po": _sum(sellers, "unbilled_po") + _sum(others, "unbilled_po"),
        # cost-basis used by Plot Master allocation
        "acquisition_cost_tzs": acquisition_cost,
        "cost_per_sqm_tzs": cost_per_sqm,
    }


def _empty_summary(la_name):
    return {
        "land_acquisition": la_name,
        "total_area_sqm": 0.0,
        "lud_account": None,
        "sellers": [],
        "others": [],
        "totals": {
            k: 0.0
            for k in (
                "seller_committed", "seller_billed", "seller_paid",
                "seller_outstanding", "seller_unbilled_po",
                "other_committed", "other_billed", "other_paid",
                "other_outstanding", "other_unbilled_po",
                "committed", "billed", "paid", "outstanding", "unbilled_po",
                "acquisition_cost_tzs", "cost_per_sqm_tzs",
            )
        },
    }


# =============================================================================
# Plot summary — counts of submitted Plot Masters per status
# =============================================================================

@frappe.whitelist()
def sync_land_acquisition_plot_summary(land_acquisition):
    if not land_acquisition or not frappe.db.exists("Land Acquisition", land_acquisition):
        return {}

    # Plot Master is created in Phase 3 — skip until then
    if not frappe.db.exists("DocType", "Plot Master"):
        return {
            "total_plots": 0,
            "available_plots": 0,
            "reserved_plots": 0,
            "delivered_plots": 0,
        }

    counts = frappe.db.sql("""
        SELECT
            COUNT(*) AS total,
            SUM(status = 'Available') AS available,
            SUM(status IN ('Pending Fee', 'Pending Advance', 'Reserved', 'Ready for Handover')) AS reserved,
            SUM(status IN ('Delivered', 'Title Closed')) AS delivered
        FROM `tabPlot Master`
        WHERE land_acquisition = %s AND docstatus = 1
    """, land_acquisition, as_dict=True)[0]

    total = int(counts.total or 0)
    available = int(counts.available or 0)
    reserved = int(counts.reserved or 0)
    delivered = int(counts.delivered or 0)

    frappe.db.set_value("Land Acquisition", land_acquisition, {
        "total_plots": total,
        "available_plots": available,
        "reserved_plots": reserved,
        "delivered_plots": delivered,
    }, update_modified=False)

    current_status = frappe.db.get_value("Land Acquisition", land_acquisition, "status")
    if current_status in ("Approved", "Subdivided"):
        new_status = "Subdivided" if total > 0 else "Approved"
        if new_status != current_status:
            frappe.db.set_value(
                "Land Acquisition", land_acquisition, "status", new_status,
                update_modified=False
            )

    return {
        "total_plots": total,
        "available_plots": available,
        "reserved_plots": reserved,
        "delivered_plots": delivered,
    }


# =============================================================================
# Doc event handlers — wire PO/PI/PE submits to the LA cost summary
# =============================================================================

def _sync_many(la_names):
    for name in {n for n in (la_names or []) if n}:
        sync_land_acquisition_cost_summary(name)


def sync_costs_from_purchase_order(doc, method=None):
    _sync_many({
        row.land_acquisition
        for row in (doc.get("items") or [])
        if row.get("land_acquisition")
    })


def sync_costs_from_purchase_invoice(doc, method=None):
    _sync_many({
        row.land_acquisition
        for row in (doc.get("items") or [])
        if row.get("land_acquisition")
    })


def autoset_land_acquisition_on_payment_entry(doc, method=None):
    """Tag a Payment Entry with the LA dimension by following its references.

    Walks both Purchase Invoice and Purchase Order references — the latter
    catches advance payments made before any invoice exists. Only sets the
    dimension on the PE header when all referenced docs resolve to the same
    LA. Mixed-LA payments are left untagged so the operator reviews them.
    """
    if doc.get("payment_type") != "Pay" or doc.get("party_type") != "Supplier":
        return
    if doc.get("land_acquisition"):
        return
    la_names = set()
    for r in (doc.get("references") or []):
        if r.reference_doctype == "Purchase Invoice":
            child_dt = "Purchase Invoice Item"
        elif r.reference_doctype == "Purchase Order":
            child_dt = "Purchase Order Item"
        else:
            continue
        item_rows = frappe.db.get_all(
            child_dt,
            filters={"parent": r.reference_name},
            fields=["land_acquisition"],
        )
        for row in item_rows:
            if row.land_acquisition:
                la_names.add(row.land_acquisition)
    if len(la_names) == 1:
        doc.land_acquisition = la_names.pop()


def sync_costs_from_payment_entry(doc, method=None):
    """Refresh LA cost summaries when a PE is submitted or cancelled.

    A single PE can settle PIs and/or pay advances against POs across multiple
    Land Acquisitions, so we walk every reference (both kinds) and resync each
    affected LA.
    """
    la_names = set()
    if doc.get("land_acquisition"):
        la_names.add(doc.land_acquisition)
    for r in (doc.get("references") or []):
        if r.reference_doctype == "Purchase Invoice":
            child_dt = "Purchase Invoice Item"
        elif r.reference_doctype == "Purchase Order":
            child_dt = "Purchase Order Item"
        else:
            continue
        item_rows = frappe.db.get_all(
            child_dt,
            filters={"parent": r.reference_name},
            fields=["land_acquisition"],
        )
        for row in item_rows:
            if row.land_acquisition:
                la_names.add(row.land_acquisition)
    _sync_many(la_names)


def set_land_acquisition_expense_account(doc, method=None):
    """Auto-fill land_acquisition on item rows from the header, then set
    expense_account to Land Under Development on all tagged items.

    ERPNext's accounting dimension already copies land_acquisition from header
    to items. This hook is a safety net — it ensures the expense_account is
    always Land Under Development regardless of the item's default, so survey
    fees, legal fees, and land price all capitalise correctly.
    """
    header_la = doc.get("land_acquisition")

    # Propagate header land_acquisition to any item rows that missed it
    if header_la:
        for item in doc.get("items") or []:
            if not item.get("land_acquisition"):
                item.land_acquisition = header_la

    has_la_items = any(
        row.get("land_acquisition") for row in (doc.get("items") or [])
    )
    if not has_la_items:
        return

    land_account = frappe.db.get_single_value(
        "LandMS Settings", "land_under_development_account"
    )
    if not land_account:
        return

    cost_center = frappe.db.get_single_value("LandMS Settings", "cost_center")

    for item in doc.get("items") or []:
        if item.get("land_acquisition"):
            item.expense_account = land_account
            if cost_center:
                item.cost_center = cost_center
