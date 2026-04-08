import frappe
from frappe.model.document import Document
from frappe.utils import flt

from landms.landms.doctype.land_acquisition.land_acquisition import (
	sync_land_acquisition_plot_summary,
)

# Plot Type → stock Item code (must exist as fixtures with Maintain Stock = Yes)
PLOT_TYPE_TO_ITEM = {
	"Residential": "RESIDENTIAL PLOT",
	"Commercial":  "COMMERCIAL PLOT",
	"Mixed Use":   "MIXED USE PLOT",
}

# Plot Type → Land Acquisition selling-rate field
PLOT_TYPE_TO_RATE_FIELD = {
	"Residential": "residential_selling_price_per_sqm",
	"Commercial":  "commercial_selling_price_per_sqm",
	"Mixed Use":   "mixed_use_selling_price_per_sqm",
}

PLOT_TYPE_RATE_LABEL = {
	"Residential": "Residential Selling Price per Sqm",
	"Commercial":  "Commercial Selling Price per Sqm",
	"Mixed Use":   "Mixed Use Selling Price per Sqm",
}


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
			[
				"booking_fee_percent",
				"government_share_percent",
				"payment_completion_days",
				"residential_selling_price_per_sqm",
				"commercial_selling_price_per_sqm",
				"mixed_use_selling_price_per_sqm",
			],
			as_dict=True,
		) or {}

		self.booking_fee_percent     = flt(la.get("booking_fee_percent"))
		self.government_share_percent = flt(la.get("government_share_percent"))
		self.payment_completion_days = int(la.get("payment_completion_days") or 0)
		self.selling_price_per_sqm   = get_plot_type_selling_rate(la, self.plot_type)

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
			label = PLOT_TYPE_RATE_LABEL.get(self.plot_type, "selling rate")
			frappe.throw(
				f"Set the '{label}' on Land Acquisition {self.land_acquisition} "
				f"before creating this plot."
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

		item_code = PLOT_TYPE_TO_ITEM.get(self.plot_type)
		if not item_code:
			frappe.throw(f"No stock item is mapped for plot type '{self.plot_type}'.")

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

		se = frappe.get_doc({
			"doctype":           "Stock Entry",
			"stock_entry_type":  "Material Receipt",
			"posting_date":      frappe.utils.today(),
			"company":           settings.company,
			"remarks":           f"Plot {self.plot_number} from {self.land_acquisition}",
			"difference_account": land_account,
			"items": [
				{
					"item_code":             item_code,
					"qty":                   1,
					"basic_rate":            flt(self.allocated_cost),
					"t_warehouse":           warehouse,
					"serial_no":             serial_number,
					"use_serial_batch_fields": 1,
				}
			],
		})
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


def get_plot_type_selling_rate(la_values, plot_type):
	"""Look up the per-sqm selling rate for the given plot type on the LA."""
	rate_field = PLOT_TYPE_TO_RATE_FIELD.get(plot_type)
	if not rate_field:
		return 0
	return flt((la_values or {}).get(rate_field))
