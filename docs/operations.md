# Pharmacy and service operations

Pharmacy serves hospital-issued prescriptions. Registration Desk records payment; Pharmacy records physical handover. There is one stock location. Prices use INR and dispensing quantities are whole units.

## OPD visit flow

1. **Registration / appointment** — register a new patient or book an existing patient at the appointment desk. The visit gets one pre-visit bill for registration/appointment and consultation fees, linked to the appointment.
2. **Payment → OPD Desk** — Registration Desk collects the pre-visit bill. The paid visit appears at the OPD Desk as **Assessment**. Unpaid visits stay in **Payment Pending** and cannot be assessed; emergency registrations bypass this gate.
3. **Initial assessment (OPD assistant)** — the OPD assistant records vitals, complaints and history directly: one click from the patient queue opens the triage workstation and claims the visit's triage ticket. Completing the assessment returns the visit to the OPD Desk as **Doctor Allocation**. In-progress drafts and emergency walk-ins are visible on the desk's assessment queue.
4. **OPD Desk → Doctor** — the OPD assistant assigns the doctor from the pool, **or leaves the visit in the unassigned pool** where it appears on each doctor's consultation queue ("Unassigned — waiting for a doctor") and any doctor can pick it themselves. The doctor queue shows assigned, assessed, paid visits.
5. **Doctor consultation** — the doctor records findings, diagnosis, orders and prescriptions. Service orders are priced from the service catalogue and create **service bills**. **Send to Pharmacy** releases prescriptions for pharmacy preparation.
6. **Registration Desk post-consultation billing** — the cashier opens **Post-consultation billing**, reviews the visit’s service and pharmacy charges, records the payment method/reference and collects payment. Paying releases service orders to the service desks and pharmacy issues to the handover queue.
7. **Services / pharmacy** — service staff process only paid orders and publish reports. Pharmacy hands over only paid issues. The OPD flow updates to Investigations, Procedures or Pharmacy, then Completed.

Service prices are configured in **Service catalogue & routing** (Service Staff/Admin). Orders snapshot the catalogue price at ordering time. A service with no price is treated as no-charge. Pharmacy pricing continues to use the actual reserved batch selling price. Payment metadata (cashier, method, reference and payment group) is stored on each bill, and every handoff writes an OPD audit event.

## First use

1. Run `.venv/bin/python manage.py migrate` and `.venv/bin/python manage.py collectstatic --noinput`.
2. In Staff Accounts, assign the Pharmacy designation to dispensers. Assign Service Staff one or more service departments. An account without department assignments cannot process any department.
3. Pharmacy → Medicines: create the exact medicine name, strength, form, unit and pack size. A tablet is one unit; bottles, tubes and vials are whole containers. Once stock exists, unit and pack size cannot change.
4. Pharmacy → Receive stock: record a physical opening count, supplier and invoice/count reference, batch, expiry, pack count and per-unit costs/prices. Receiving 3 packs of 10 tablets adds 30 tablets. No stock is inferred from old prescriptions.
5. Doctor’s Desk: enter an explicit total quantity and use **Send to Pharmacy**. Saving or completing a consultation alone does not release draft prescriptions. The medicine search includes the pharmacy catalogue.
6. Pharmacy → Prescriptions: open a visit and enter the available quantities to prepare. Exact catalogue identity is required; substitutions require a doctor amendment. Earliest-expiring valid batches are reserved first and an itemized bill is created.
7. Registration Desk → Pharmacy Billing: review items, open the payment slip and mark the bill paid.
8. Pharmacy: open the issue and confirm medicine handover. This deducts physical stock, releases reservations and records each batch. A smaller handover leaves the prescription partially dispensed with its remaining balance outstanding.

## Reservations, corrections and history

- Only the linked pharmacy bill blocks handover; unrelated patient dues are shown separately.
- An unpaid issue can be cancelled to release its reservations and cancel its bill. Paid or handed-over issues cannot be cancelled; refunds and returns are outside this version.
- Unpaid reservations older than 24 hours are flagged for review. They are not automatically released.
- Expired batches cannot be received or issued. An issue held past its batch expiry can be refreshed onto valid stock of the same medicine without changing its billed prices.
- Inventory → Stock adjustment records a signed unit correction and reason. It cannot remove reserved stock. The stock ledger is append-only.
- Repeated submissions are idempotent. If the database is busy, refresh and retry the same issue; no additional handover is deducted.
- Released prescriptions are read-only. Cancel their unreserved balance and add an amendment. Paid reservations must be resolved first.
- Historical dispense totals remain visible as legacy records, without invented batch movements. Legacy requests missing quantities need clarification and a new doctor amendment.
- Stock alerts use usable, unreserved quantities. Near-expiry means within 90 days. Expired quantities remain visible in physical stock but are excluded from availability.

## Service departments

- Administrators manage the service catalogue and route ambiguous historical orders in **Service catalogue & routing**. Normal queues use explicit department IDs, not free-text keywords.
- Radiology stays one department with **X-Ray / USG / Unspecified** tabs. Catalogue services carry a modality; historical orders can be tagged from the workstation. Preparation (fasting, full bladder), body part and view are recorded per order.
- Laboratory records the specimen timeline (collected → received), stamps the collection time, and can flag a **critical result**. Critical orders carry a red badge on the doctor’s reports page.
- Cardiology (ECG) records acquisition status and stamps the acquisition time; urgent chest-pain requests stay at the top of the queue.
- **Physiotherapy and counselling use session plans.** Set the planned number of sessions, then schedule, reschedule, and record attendance/no-show with progress notes. The order moves to In Progress on the first attended session and completes automatically when the attended count reaches the plan.
- Counselling content stays private: session notes are visible only to the assigned service staff, the requesting doctor and administrators, never to the OPD coordination desk. Audit events record that a note exists, never its content.
- Assigned Service Staff see their department worklists, request priority, waiting time, staff assignment, and the department-specific workstation. The service desk shows workload, in-progress work, review flags and recent activity.
- The doctor’s Orders tab shows nurse-proposed requests separately. Checking a proposed request and saving confirms it into a service order.
- Staff publish a PDF/JPEG/PNG report, up to 10 MB, and summary against an order. Publication and doctor review are separate from operational completion.
- Only assigned departments, the requesting doctor, hospital administrators, and the OPD coordination desk can access report downloads. Counselling report content is excluded from the OPD coordination desk.
- Completed Today uses the recorded completion timestamp and staff member. Attended sessions are listed separately for the same day. Historical completions with no reliable timestamp remain historical rather than being relabelled as today.

## Deployment and verification

Report files are stored in `private_reports/`, outside public `MEDIA_ROOT`, and downloaded through authenticated views. Keep this directory persistent and include it in infrastructure backups alongside the database. The existing per-patient export is not a complete operational/database backup. `PRIVATE_REPORT_ROOT` can override the private storage directory in Django settings.

On the local Homebrew setup, run `DATABASE_URL='postgresql:///hms?host=/tmp' .venv/bin/python manage.py test --noinput` for regression and workflow tests. This uses the local PostgreSQL administrator account to create and remove Django's temporary test database without granting that permission to the application role. Concurrency tests cover competing stock reservations using conditional database writes. Run deployment checks against a disposable PostgreSQL test database.

Purchasing, supplier accounts, walk-in sales, automatic dosing, medicine substitutions, returns/refunds, structured laboratory panels and equipment integrations are not implemented.
