import frappe
from frappe.model.document import Document
from frappe.utils import flt

from landms.landms.doctype.land_acquisition.land_acquisition import (
	sync_land_acquisition_plot_summary,
)



class PlotMaster(Document):

	def validate(self):
		self.validate_land_acquisition()
		self.fill_acquisition_name()
		self.fill_sales_defaults()
		self.fill_location_coordinates()
		self.fill_financials()
		self.validate_coordinate_pair()
		self.validate_duplicate_plot_number()
		self.validate_selling_price()

	def validate_land_acquisition(self):
		"""Plot Master can only hang off an Approved or Subdivided LA."""
		if not self.land_acquisition:
			return
		status = frappe.db.get_value("Land Acquisition", self.land_acquisition, "status")
		if status not in ("Approved", "Subdivided"):
			frappe.throw(
				f"Land Acquisition {self.land_acquisition} is not ready for subdivision "
				f"(current status: {status}). Only Approved or Subdivided acquisitions can be used."
			)

	def fill_acquisition_name(self):
		if not self.land_acquisition:
			self.acquisition_name = ""
			return
		self.acquisition_name = frappe.db.get_value(
			"Land Acquisition", self.land_acquisition, "acquisition_name"
		) or ""

	def fill_sales_defaults(self):
		"""Snapshot the sales-default fields from the LA so they survive
		even if the LA is later edited."""
		if not self.land_acquisition:
			self.booking_fee_percent = 0
			self.government_share_percent = 0
			self.payment_completion_days = 0
			self.selling_price_per_sqm = 0
			return

		la = frappe.db.get_value(
			"Land Acquisition",
			self.land_acquisition,
			["booking_fee_percent", "government_share_percent", "payment_completion_days"],
			as_dict=True,
		) or {}

		self.booking_fee_percent      = flt(la.get("booking_fee_percent"))
		self.government_share_percent = flt(la.get("government_share_percent"))
		self.payment_completion_days  = int(la.get("payment_completion_days") or 0)
		self.selling_price_per_sqm    = get_plot_type_selling_rate(self.land_acquisition, self.plot_type)

	def fill_location_coordinates(self):
		"""If the plot has no coordinates, fall back to the LA's coordinates."""
		if not self.land_acquisition:
			return

		coords = frappe.db.get_value(
			"Land Acquisition",
			self.land_acquisition,
			["latitude", "longitude"],
			as_dict=True,
		) or {}

		if self.latitude in (None, "", 0) and coords.get("latitude") not in (None, "", 0):
			self.latitude = flt(coords.get("latitude"))
		if self.longitude in (None, "", 0) and coords.get("longitude") not in (None, "", 0):
			self.longitude = flt(coords.get("longitude"))

	def fill_financials(self):
		"""Cost is sourced from the LA's per-sqm cost; selling price from the
		per-plot-type rate. Both are recomputed every save in case the LA is
		edited (snapshotting only happens for the cosmetic snapshot fields)."""
		if not self.land_acquisition:
			self.cost_per_sqm = 0
			self.allocated_cost = 0
			self.selling_price = 0
			return

		la = frappe.get_doc("Land Acquisition", self.land_acquisition)
		total_sqm = flt(la.total_area_sqm)
		plot_sqm  = flt(self.plot_size_sqm)

		self.cost_per_sqm = 0
		self.allocated_cost = 0

		if flt(la.acquisition_cost_tzs) > 0 and total_sqm > 0:
			self.cost_per_sqm = (
				flt(la.get("cost_per_sqm_tzs"))
				or (flt(la.acquisition_cost_tzs) / total_sqm)
			)
			if plot_sqm > 0:
				self.allocated_cost = self.cost_per_sqm * plot_sqm

		if flt(self.selling_price_per_sqm) > 0 and plot_sqm > 0:
			self.selling_price = flt(self.selling_price_per_sqm) * plot_sqm
		else:
			self.selling_price = 0

		self._fill_govt_fees(plot_sqm)

	def _fill_govt_fees(self, plot_sqm):
		land_rent_rate = flt(frappe.db.get_value("Plot Type", self.plot_type, "land_rent_rate"))
		settings = frappe.get_single("LandMS Settings")
		appl      = flt(settings.govt_appl_fee_amount) or 5000
		reg_fee   = flt(settings.govt_registration_fee_amount) or 25000
		prem_rate = flt(settings.govt_premium_rate) or 0.0025

		self.land_rent_rate      = land_rent_rate
		self.land_rent           = flt(land_rent_rate * plot_sqm)
		self.govt_application_fee = appl
		self.premium             = flt(prem_rate * flt(self.selling_price))
		self.registration_prep_fee = flt(0.2 * self.land_rent)
		self.govt_registration_fee = reg_fee
		self.stamp_duty          = flt(((self.land_rent - 2000) / 20) + 500) if self.land_rent > 0 else 0
		self.total_govt_fees     = flt(
			self.govt_application_fee +
			self.premium +
			self.land_rent +
			self.registration_prep_fee +
			self.govt_registration_fee +
			self.stamp_duty
		)
		self.total_plot_amount   = flt(self.selling_price) + self.total_govt_fees

	def validate_coordinate_pair(self):
		"""Either both lat/lon are set, or both are blank."""
		has_lat = self.latitude not in (None, "", 0)
		has_lon = self.longitude not in (None, "", 0)
		if has_lat != has_lon:
			frappe.throw("Latitude and Longitude must be provided together.")

	def validate_duplicate_plot_number(self):
		if not self.plot_number or not self.land_acquisition:
			return
		existing = frappe.db.get_value(
			"Plot Master",
			{
				"land_acquisition": self.land_acquisition,
				"plot_number": self.plot_number,
				"name": ("!=", self.name),
				"docstatus": ("!=", 2),
			},
			"name",
		)
		if existing:
			frappe.throw(
				f"Plot number '{self.plot_number}' already exists for "
				f"Land Acquisition {self.land_acquisition} (see {existing})."
			)

	def validate_selling_price(self):
		if flt(self.selling_price_per_sqm) <= 0:
			frappe.throw(
				f"No selling rate found for plot type '{self.plot_type}' on "
				f"Land Acquisition {self.land_acquisition}. "
				"Add a row for this plot type in the Plot Type Rates table on the Land Acquisition."
			)
		if flt(self.selling_price) <= 0:
			frappe.throw("Selling Price must be greater than zero.")

	def on_submit(self):
		self.create_stock_entry()
		sync_land_acquisition_plot_summary(self.land_acquisition)

	def on_cancel(self):
		self.cancel_stock_entry()
		sync_land_acquisition_plot_summary(self.land_acquisition)

	def create_stock_entry(self):
		"""Create a Material Receipt Stock Entry that moves `allocated_cost`
		from the Land Under Development account into the Plot Inventory
		warehouse, against a freshly minted Serial No tied to this plot."""
		settings = frappe.get_single("LandMS Settings")

		item_code = get_plot_item_code(self.plot_type)

		warehouse = settings.plot_inventory_warehouse
		if not warehouse:
			frappe.throw("Plot Inventory Warehouse is not set in LandMS Settings.")

		land_account = settings.land_under_development_account
		if not land_account:
			frappe.throw("Land Under Development Account is not set in LandMS Settings.")

		# Use the plot name (e.g. PLT-2026-0001) as the serial number — globally
		# unique and lets the plot ↔ serial ↔ stock-entry chain be traced by name.
		serial_number = self.name

		# Pre-create the Serial No record. ERPNext's validate_serialized_batch()
		# runs on Stock Entry insert (i.e. *before* on_submit), so the Serial No
		# must already exist by the time we hand it to the SE.
		if not frappe.db.exists("Serial No", serial_number):
			frappe.get_doc({
				"doctype":   "Serial No",
				"serial_no": serial_number,
				"item_code": item_code,
				"company":   settings.company,
			}).insert(ignore_permissions=True)

		se = frappe.get_doc(
			_build_plot_stock_entry_doc(
				company=settings.company,
				plot_number=self.plot_number,
				land_acquisition=self.land_acquisition,
				item_code=item_code,
				basic_rate=flt(self.allocated_cost),
				target_warehouse=warehouse,
				serial_number=serial_number,
				difference_account=land_account,
			)
		)
		se.insert(ignore_permissions=True)
		se.submit()

		self.db_set("stock_entry", se.name)
		self.db_set("serial_no",   serial_number)

		frappe.msgprint(
			f"Plot entered inventory. Stock Entry: {se.name} | Serial No: {serial_number}",
			indicator="green",
			alert=True,
		)

	def cancel_stock_entry(self):
		if not self.stock_entry:
			return
		se = frappe.get_doc("Stock Entry", self.stock_entry)
		if se.docstatus == 1:
			se.cancel()
		self.db_set("stock_entry", None)
		self.db_set("serial_no", None)


def get_plot_item_code(plot_type):
	"""Return the stock item code for the given plot type from the Plot Type doctype."""
	if not plot_type:
		frappe.throw("Plot Type is required.")
	item_code = frappe.db.get_value("Plot Type", plot_type, "item_code")
	if not item_code:
		frappe.throw(
			f"No stock item is set for plot type '{plot_type}'. "
			"Go to Plot Type → open the record and set the Stock Item."
		)
	return item_code


def get_plot_type_selling_rate(land_acquisition, plot_type):
	"""Look up the per-sqm selling rate for the given plot type from the LA's rate table."""
	if not land_acquisition or not plot_type:
		return 0
	result = frappe.db.get_value(
		"Land Acquisition Plot Type Rate",
		{"parent": land_acquisition, "plot_type": plot_type},
		"selling_price_per_sqm",
	)
	return flt(result)


def _build_plot_stock_entry_doc(
	*,
	company: str,
	plot_number: str,
	land_acquisition: str,
	item_code: str,
	basic_rate: float,
	target_warehouse: str,
	serial_number: str,
	difference_account: str,
) -> dict:
	"""Build the Stock Entry payload used when a plot enters inventory.

	The plot items use a delivery-side item default expense account (COGS) for
	handover. Plot creation must not inherit that value; it needs to post against
	the Land Under Development asset account instead.
	"""
	return {
		"doctype": "Stock Entry",
		"stock_entry_type": "Material Receipt",
		"posting_date": frappe.utils.today(),
		"company": company,
		"remarks": f"Plot {plot_number} from {land_acquisition}",
		"land_acquisition": land_acquisition,
		"difference_account": difference_account,
		"items": [
			{
				"item_code": item_code,
				"qty": 1,
				"basic_rate": flt(basic_rate),
				"t_warehouse": target_warehouse,
				"serial_no": serial_number,
				"use_serial_batch_fields": 1,
				# Prevent Stock Entry from inheriting the item default COGS account.
				"expense_account": difference_account,
			}
		],
	}
