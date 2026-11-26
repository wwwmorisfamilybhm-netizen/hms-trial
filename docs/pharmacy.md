# Pharmacy discovery and scope map

## Ownership
Pharmacy-specific code in this repository lives in:
- `core/operations.py` — pharmacy transactions
- `core/operations_views.py` — pharmacy views and workspace context
- `core/operations_forms.py` — pharmacy forms
- `core/pharmacy_imports.py`, `core/pharmacy_import_views.py`, `core/pharmacy_photos.py`
- `core/urls.py` — pharmacy routes only
- `templates/core/pharmacy_*.html`, `templates/core/_pharmacy_sidebar.html`
- `static/css/pharmacy.css`, `static/js/pharmacy*.js`
- pharmacy models inside `patients/models.py`, additive migrations, pharmacy tests
- `templates/base.html` pharmacy nav branch only

## Read-only contracts
Patients, consultations, prescriptions, patient bills, audit events, authentication,
permissions, doctor/OPD/nursing/service workflows, global billing and other designations.

## Baseline
- Django 6.1, PostgreSQL, server-rendered templates.
- Existing prototype: dispensing desk, prescription prepare/payment/handover, FEFO,
  batch reservations, catalogue, CSV/Excel/PDF import, product photos, receive/adjust,
  batch inventory, stock ledger, dispensing history.

## Workspace
The designation now has a single working workspace with a pharmacy-branded shell
(teal hero, live status capsules, bento cards, KPI rails) built only from
`.pharmacy-page` scoped rules in `static/css/pharmacy.css`. All 18 sidebar
destinations are real routes; none are placeholders.

### Overview and patient services
- `/pharmacy/` — KPI dashboard: today's money, queue capsules, handover desk,
  today's trade, stock/expiry alerts, refunds due, quarantine, live audit feed,
  desk shortcuts.
- `/pharmacy/queue/` — four prescription worklists (to prepare, ready, awaiting
  payment, all released). The hero capsules and the queue tabs share one query
  builder, so their counts cannot drift. Stale unpaid issues (>24h) are a filter.
- `/pharmacy/consultation/<id>/` — prepare a visit; reserve FEFO stock and send
  the bill to the cashier.
- `/pharmacy/issues/<id>/` — issue detail, batch allocations, handover, reallocate
  and cancel actions, patient/bill/audit cross-links.
- `/pharmacy/dispense-history/` — handover register with date, staff and search filters.

### Sales
- `/pharmacy/sales/` — POS: barcode/brand/generic search, OTC-only results, patient
  lookup, session basket, per-line and bill discounts, live payable recalculation,
  controlled-medicine authorisation, payment and receipt.
- `/pharmacy/sales/history/` — sales register with status, payment-method and credit
  balance filters.
- `/pharmacy/sales/<id>/` — invoice, batch/HSN/GST detail, audited cancel, and a
  direct link into a return for that sale.

### Inventory
- `/pharmacy/catalogue/` — medicine master: identity, dispensing rules, stock
  control, photo, import.
- `/pharmacy/stock-report/` — stock KPIs, batch balances, filterable stock ledger
  with links to the sale/issue/purchase that caused each movement, reorder watch.
- `/pharmacy/receive/`, `/pharmacy/adjust/` — opening receipts and stock corrections.
- `/pharmacy/receive/` also links to `/pharmacy/import/?kind=stock`: recognised PDF
  invoices are parsed into medicine rows automatically (IVAANS and common column
  layouts), then reviewed once before an atomic inventory commit. Liquids are
  tracked as bottles; solid pack expressions such as `10X1X10` become 100
  dispensing units. Invoice totals, parser warnings and the original document
  remain available for review. This stock import does not create a payable.
- `/pharmacy/expiry/` — critical/watch/30/60/90/expired windows, days-left,
  supplier-return shortcut per batch.
- `/pharmacy/batches/<id>/` — fact grid, movement ledger with source links,
  transfers, quarantine/returns, batch actions.
- `/pharmacy/batches/action/` — damage, quarantine, release and transfer.

### Procurement
- `/pharmacy/suppliers/`, `/pharmacy/suppliers/new/`, `/pharmacy/suppliers/<id>/` —
  supplier master, licences, terms and live outstanding balance.
- `/pharmacy/purchases/` — purchase register with ordered/partial/received/cancelled
  tabs and supplier-scoped views.
- `/pharmacy/purchases/new/` — one form with two honest modes:
  **Post goods receipt** (stock enters inventory immediately) or
  **Record purchase order** (no stock movement, no payable until received).
- `/pharmacy/purchases/<id>/` — line-level ordered/received/outstanding, batch details
  for open lines, `Receive all lines with batch details` (POST on the same URL),
  and guarded supplier payments.

### Returns and finance
- `/pharmacy/returns/` — register plus two intake forms. `?sale=` limits the patient
  return selector to that invoice; `?batch=` prefills a supplier return. Refunds are
  settled separately.
- `/pharmacy/reports/` — period KPIs (sales, dispensing, stock value, pending,
  supplier balance) with drill links into every register and a sales CSV export.
- `/pharmacy/settings/` — thresholds and sales policy, current policy summary,
  effect preview links, and validation that the critical window cannot exceed the
  watch window.
- `/pharmacy/audit/` — append-only audit trail with entity deep links and a
  before/after payload disclosure.

### Prescription desk
- `/pharmacy/queue/` — all released prescriptions in one worklist.
- `/pharmacy/consultation/<id>/` — prepare/reserve; the bill is sent to the cashier.
- `/pharmacy/issues/<id>/` — handover after payment.
- `/pharmacy/dispense-history/` — completed handovers with filters.

## Stock and money rules
- `available = on_hand - reserved - damaged - quarantined`, forced to 0 unless the
  batch is `active` and in date.
- `StockMovement` and `PharmacyAudit` are append-only; every mutation also writes an
  audit row.
- Direct sales refuse `prescription_required` medicines, validate discounts against
  `max_discount_percent` and require full payment unless the sale is authorised credit
  for a registered patient.
- Patient returns increase `on_hand` and `quarantined` together, so returned stock is
  sellable only after release; the cash refund is settled separately and is blocked
  while the return is still pending.
- Supplier returns take held units first (quarantine, then damaged), only move
  unreserved physical stock, and record the purchase-cost credit on the return.
- Goods receipts post a `PharmacyPurchase` with lines, increment stock and record the
  supplier balance. Purchase orders record the same lines with no stock or payable
  until goods are received; receiving posts only lines that already carry a batch
  number and future expiry. A partially received order can only be paid up to the
  value actually received, so the purchase and supplier ledgers cannot diverge.
- Controlled medicines (`Medicine.is_controlled`) need a recorded pharmacist
  authorisation on the sale whenever `PharmacySettings.require_controlled_authorisation`
  is on; the reference is stored on the sale and audit row.
- All money and stock checks use conditional `UPDATE ... WHERE` guards so concurrent
  edits cannot oversell or overpay a supplier.
- Duplicate submissions are idempotent through `request_key` on sales, receipts,
  orders, returns, transfers and settings-sensitive actions.

## Cross-feature integration
- Dashboard capsules → queue tabs → issue detail → invoice/ledger.
- Invoice → return (prefilled sale) → quarantine → batch release → refund settle.
- Expiry → batch/supplier return (prefilled batch) → stock ledger.
- Purchase order → batch details → receive → stock/ledger/supplier balance → payment.
- Stock movements link back to their sale, issue or purchase.
- Audit rows link to the sale, purchase, batch, return, supplier or settings they
  describe, including a before/after payload.
- Reports drill into sales history (payment filter), catalogue, returns, suppliers,
  stock and prescriptions.

## Migrations
`0035_pharmacyaudit_pharmacypurchase_pharmacypurchaseline_and_more` (additive: new
tables and columns only, no deletes or renames),
`0036_pharmacy_sale_controlled_authorisation` (one text column, default empty string),
and `0037_alter_patient_payment_type`. `0038_medicine_import_document` adds the
invoice metadata JSON field used by PDF stock imports.
Apply them with `manage.py migrate patients`; they have only been run against the
local development database. The workspace revamp adds no other migrations.

## Tests
`core/tests_pharmacy_workspace.py` covers stock/money guards, FEFO, POS, returns,
procurement (including order-only receiving), expiry windows, the queue/dashboard
count parity and every new route. `core/tests.py` keeps the designation-access test.
Latest run: 42 pharmacy-focused tests, 187 tests overall — all passing.

## Known limitation
IPD/inpatient ordering has no pharmacy contract in this repository, so admissions-based
pharmacy requests are out of scope and were not invented. Prescriptions continue to
arrive from OPD consultations.
