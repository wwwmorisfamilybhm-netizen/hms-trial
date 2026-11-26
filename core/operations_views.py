"""Role-specific pharmacy and service workspaces."""
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode
from uuid import uuid4
from pathlib import Path
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db import OperationalError, IntegrityError, transaction
from django.db.models import F, Q, Sum, Min, Count, DecimalField, Value
from django.db.models.functions import Coalesce, Greatest
from django.urls import reverse
from django.http import FileResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST
from patients.models import (
    Medicine, MedicineBatch, StockMovement, PharmacyIssue, PrescriptionItem,
    ConsultationSession, PatientBill, ClinicalOrder, ServiceDepartment, ServiceDefinition,
    DiagnosticReport, OPDAuditEvent, ServiceSession, PharmacySettings, PharmacySupplier,
    PharmacyPurchase, PharmacySale, PharmacySaleLine, PharmacyReturn, PharmacyAudit,
)
from .operations import (
    require_pharmacy, require_admin, is_admin, designation, scoped_orders, receive_stock, adjust_stock,
    reserve_issue, cancel_issue, handover_issue, reallocate_issue, cancel_prescription, update_batch_prices,
    set_bill_status, event, pharmacy_audit, usable_batches, batch_usable_quantity,
    create_sale, cancel_sale, receive_purchase, create_patient_return, release_quarantine,
    hold_quarantine, record_damage, transfer_stock, return_to_supplier, settle_patient_return,
    record_purchase_order, receive_ordered_purchase, set_purchase_line_details,
)
from .operations_forms import (
    MedicineForm, BatchPriceForm, ReceiptForm, AdjustmentForm, ServiceDefinitionForm, ReportForm,
    PharmacySupplierForm, PharmacySettingsForm, StockHoldForm, QuarantineReleaseForm,
    StockTransferForm, PatientReturnForm, SupplierReturnForm, PatientRefundForm,
    PurchaseLineFormSet, PurchaseReceiptForm, PurchaseLineDetailsForm,
)


def error_message(exc):
    return " ".join(exc.messages) if isinstance(exc, ValidationError) else "Another update is in progress. Refresh and try again; this submission can be retried safely."


def pharmacy_context(include_vitals=False, vitals=None):
    """Shared pharmacy template context.

    Vitals are live status capsules in the workspace hero. They are only built
    for the desks that act on them (dashboard and the prescription queue) so
    that list and form pages stay at their current query cost.
    """
    context = {"page_title": "Pharmacy", "today": timezone.localdate()}
    if vitals is not None:
        context["vitals"] = vitals
    elif include_vitals:
        context["vitals"] = pharmacy_vitals()
    return context


def discharge_totals(today):
    """Bill value and discount of prescriptions handed over today."""
    return PharmacyIssue.objects.filter(
        status=PharmacyIssue.Status.DISPENSED, dispensed_at__date=today
    ).aggregate(
        billed=Coalesce(Sum("bill__amount"), Decimal("0")),
        discount=Coalesce(Sum("bill__discount"), Decimal("0")),
        net=Coalesce(Sum(F("bill__amount") - F("bill__discount")), Decimal("0")),
    )


def dispensing_queues(query=""):
    """The four prescription worklists, built once and shared by desk and dashboard."""
    visits = searched_visits(query)
    released = Q(prescriptions__released_at__isnull=False)
    outstanding = (released & Q(prescriptions__dispense_status__in=["ordered", "partial", "unavailable"])
                   & (Q(prescriptions__prescribed_quantity__isnull=True)
                      | Q(prescriptions__prescribed_quantity=0)
                      | Q(prescriptions__prescribed_quantity__gt=F("prescriptions__dispensed_quantity") + F("prescriptions__reserved_quantity"))))
    issues = PharmacyIssue.objects.filter(status="awaiting_payment").select_related("bill", "consultation__patient")
    pending = issues.filter(bill__status="pending")
    stale = pending.filter(created_at__lt=timezone.now() - timedelta(hours=24))
    queues = {
        "ready": ("Ready for handover", issues.filter(bill__status="paid", consultation__in=visits)
                  .annotate(wait_since=Coalesce("bill__paid_at", "created_at"))),
        "prepare": ("To prepare", visits.exclude(status="cancelled").annotate(
            wait_since=Min("prescriptions__released_at", filter=outstanding)).filter(wait_since__isnull=False)),
        "payment": ("Awaiting payment", pending.filter(consultation__in=visits).annotate(wait_since=F("created_at"))),
        "all": ("All released visits", visits.annotate(wait_since=Min("prescriptions__released_at")).filter(wait_since__isnull=False)),
    }
    return queues, stale


def pharmacy_vitals(queues=None, stale=None):
    """Live counts for the hero capsules, reused by the pages they link to."""
    today = timezone.localdate()
    if queues is None:
        queues, stale = dispensing_queues()
    elif stale is None:
        _, stale = dispensing_queues()
    counts = {key: rows.count() for key, (_, rows) in queues.items()}
    settings = PharmacySettings.current()
    critical = MedicineBatch.objects.filter(
        on_hand__gt=0, status=MedicineBatch.Status.ACTIVE,
        expiry_date__range=(today, today + timedelta(days=settings.near_expiry_warn_days),
    )).count()

    low = stocked_medicines(today).filter(usable__lte=F("reorder_level")).count()
    pending_orders = PharmacyPurchase.objects.exclude(status__in=["received", "cancelled"]).count()
    return [
        {"key": "prepare", "label": "To prepare", "value": counts["prepare"], "tone": "teal",
         "meta": "Released prescriptions", "url": reverse("pharmacy_queue") + "?tab=prepare"},
        {"key": "ready", "label": "Ready to hand over", "value": counts["ready"], "tone": "lime",
         "meta": "Paid and prepared", "url": reverse("pharmacy_queue") + "?tab=ready"},
        {"key": "payment", "label": "Awaiting payment", "value": counts["payment"], "tone": "amber",
         "meta": f"{stale.count()} over 24 hours", "url": reverse("pharmacy_queue") + "?tab=payment"},
        {"key": "", "label": "Near expiry", "value": critical, "tone": "rose",
         "meta": f"Within {settings.near_expiry_warn_days} days",
         "url": reverse("pharmacy_expiry") + "?window=critical"},
        {"key": "", "label": "Low stock", "value": low, "tone": "slate", "meta": "At or below reorder",
         "url": reverse("pharmacy_stock_report") + "?filter=low"},
        {"key": "", "label": "Open orders", "value": pending_orders, "tone": "violet",
         "meta": "Supplier orders to receive",
         "url": reverse("pharmacy_purchases") + "?status=ordered"},
    ]


def stocked_medicines(today):
    return Medicine.objects.filter(is_active=True).annotate(
        usable=Sum(F("batches__on_hand") - F("batches__reserved") - F("batches__damaged") - F("batches__quarantined"),
                   filter=Q(batches__expiry_date__gte=today, batches__status=MedicineBatch.Status.ACTIVE), default=0)
    )


def expiring_batches(today):
    settings = PharmacySettings.current()
    return MedicineBatch.objects.filter(
        on_hand__gt=0, status=MedicineBatch.Status.ACTIVE,
        expiry_date__range=(today, today + timedelta(days=settings.expiry_warning_days))
    )


def searched_visits(query):
    visits = ConsultationSession.objects.select_related("patient", "doctor")
    for term in query.split():
        visits = visits.filter(Q(patient__first_name__icontains=term)
                              | Q(patient__last_name__icontains=term) | Q(patient__uhid__icontains=term)
                              | Q(visit_token__icontains=term))
    return visits


def prescription_preparation(selected, post_data=None):
    """Build the released prescription rows shared by the queue and POS desks."""
    post_data = post_data or {}
    rows = list(selected.prescriptions.filter(released_at__isnull=False).select_related("medicine"))
    medicines = stocked_medicines(timezone.localdate())
    if all(rx.medicine_id for rx in rows):
        medicines = medicines.filter(pk__in=[rx.medicine_id for rx in rows])
    stock_by_id = {}
    catalogue = {}
    for medicine in medicines:
        stock_by_id[medicine.pk] = medicine.usable
        catalogue.setdefault(" ".join(medicine.name.casefold().split()), []).append(medicine)
    for rx in rows:
        rx.matches = catalogue.get(" ".join(rx.medicine_name.casefold().split()), [])
        rx.selected_match = post_data.get(f"medicine_{rx.pk}", "")
        rx.entered_quantity = post_data.get(f"quantity_{rx.pk}", "0")
        stock = stock_by_id.get(rx.medicine_id, 0)
        if not rx.medicine_id:
            stock = next((m.usable for m in rx.matches if str(m.pk) == rx.selected_match), None)
        rx.available_stock = stock
        rx.prepare_limit = min(rx.remaining_quantity, 9999)
        if stock is not None:
            rx.prepare_limit = min(rx.prepare_limit, stock)
    can_prepare = selected.status != "cancelled" and any(
        rx.remaining_quantity and (rx.prepare_limit if rx.medicine_id else any(m.usable for m in rx.matches))
        and rx.dispense_status not in ("cancelled", "dispensed")
        for rx in rows
    )
    return rows, can_prepare


@login_required(login_url="login")
def pharmacy_dashboard(request):
    require_pharmacy(request.user)
    today = timezone.localdate()
    settings = PharmacySettings.current()

    # Real, database-backed operational metrics for today.
    sales_today = PharmacySale.objects.filter(
        status=PharmacySale.Status.COMPLETED, completed_at__date=today)
    sales_agg = sales_today.aggregate(
        gross=Coalesce(Sum("total"), Decimal("0")),
        discount=Coalesce(Sum("discount"), Decimal("0")),
        received=Coalesce(Sum("amount_received"), Decimal("0")),
    )
    issue_agg = discharge_totals(today)
    gross = sales_agg["gross"] + issue_agg["billed"]
    discounts = sales_agg["discount"] + issue_agg["discount"]
    collections = sales_agg["received"] + issue_agg["net"]

    queues, stale = dispensing_queues()
    vitals = pharmacy_vitals(queues, stale)
    queue_counts = {cap["key"]: cap["value"] for cap in vitals if cap.get("key")}

    released = PrescriptionItem.objects.filter(released_at__isnull=False, consultation__status__in=["draft", "in_progress", "completed"])
    pending_qs = released.exclude(dispense_status__in=["cancelled", "dispensed"]).filter(
        Q(prescribed_quantity__isnull=True) | Q(prescribed_quantity=0)
        | Q(prescribed_quantity__gt=F("dispensed_quantity") + F("reserved_quantity")))
    urgent = pending_qs.filter(consultation__orders__priority="urgent").distinct()

    low_stock = stocked_medicines(today).filter(usable__lte=F("reorder_level"))
    out_of_stock = stocked_medicines(today).filter(usable=0)
    expiring = expiring_batches(today)
    critical = MedicineBatch.objects.filter(
        on_hand__gt=0, status=MedicineBatch.Status.ACTIVE,
        expiry_date__range=(today, today + timedelta(days=settings.near_expiry_warn_days))
    )
    expired = MedicineBatch.objects.filter(on_hand__gt=0, expiry_date__lt=today)
    pending_purchases = PharmacyPurchase.objects.exclude(status__in=["received", "cancelled"])
    supplier_balance = PharmacySupplier.objects.aggregate(total=Coalesce(Sum("outstanding_balance"), Decimal("0")))["total"]
    recent_sales = PharmacySale.objects.filter(status=PharmacySale.Status.COMPLETED).select_related("patient", "completed_by")[:5]
    recent_issues = PharmacyIssue.objects.filter(status=PharmacyIssue.Status.DISPENSED).select_related(
        "consultation__patient", "dispensed_by")[:5]
    recent_purchases = PharmacyPurchase.objects.select_related("supplier")[:5]
    ready_rows = queues["ready"][1].select_related("consultation__patient")[:4]
    refunds_due = PharmacyReturn.objects.filter(
        kind=PharmacyReturn.Kind.PATIENT, status=PharmacyReturn.Status.QUARANTINED
    ).count()
    held = MedicineBatch.objects.filter(quarantined__gt=0).aggregate(
        units=Coalesce(Sum("quarantined"), 0), batches=Count("pk"))
    recent_audit = PharmacyAudit.objects.select_related("actor")[:5]
    critical_rows = critical.select_related("medicine").order_by("expiry_date")[:4]
    alerts = []
    if expired.exists():
        alerts.append(("expired", "Expired batches still on hand", expired.count(), reverse("pharmacy_expiry") + "?window=expired"))
    if critical.exists():
        alerts.append(("expiring", f"Batches expiring within {settings.near_expiry_warn_days} days", critical.count(), reverse("pharmacy_expiry") + "?window=critical"))
    elif expiring.exists():
        alerts.append(("expiring", f"Batches expiring within {settings.expiry_warning_days} days", expiring.count(), reverse("pharmacy_expiry") + "?window=watch"))
    if low_stock.exists():
        alerts.append(("low", "Medicines at or below reorder level", low_stock.count(), reverse("pharmacy_stock_report") + "?filter=low"))
    if out_of_stock.exists():
        alerts.append(("out", "Medicines out of stock", out_of_stock.count(), reverse("pharmacy_stock_report") + "?filter=out"))
    return render(request, "core/pharmacy_dashboard.html", {
        **pharmacy_context(vitals=vitals),
        "page_title": "Pharmacy overview",
        "queue_counts": queue_counts,
        "metrics": {
            "gross": gross, "net": gross - discounts, "discounts": discounts, "collections": collections,
            "pending": pending_qs.count(), "urgent": urgent.count(),
            "partial": released.filter(dispense_status="partial").count(),
            "completed": released.filter(dispense_status="dispensed").count(),
            "active_medicines": Medicine.objects.filter(is_active=True).count(),
            "low": low_stock.count(), "out": out_of_stock.count(),
            "expiring": expiring.count(), "critical": critical.count(), "expired": expired.count(),
            "pending_purchases": pending_purchases.count(), "supplier_balance": supplier_balance,
            "refunds_due": refunds_due, "held_units": held["units"], "held_batches": held["batches"],
        },
        "alerts": alerts, "recent_sales": recent_sales, "recent_issues": recent_issues,
        "recent_purchases": recent_purchases, "recent_audit": recent_audit,
        "ready_rows": ready_rows, "critical_rows": critical_rows, "settings": settings,
    })


def dispensing_desk(request, default_tab):
    today = timezone.localdate()
    query = request.GET.get("q", "").strip()
    queues, stale = dispensing_queues(query)
    tab = request.GET.get("tab", default_tab)
    if tab not in queues:
        tab = default_tab
    label, records = queues[tab]
    stale_only = tab == "payment" and request.GET.get("stale") == "1"
    if stale_only:
        records = records.filter(pk__in=stale)
    counts = {key: rows.count() for key, (_, rows) in queues.items()}
    page = Paginator(records.order_by("wait_since", "pk"), 20).get_page(request.GET.get("page"))
    state = request.GET.copy()
    state["tab"] = tab
    state["page"] = page.number
    return render(request, "core/pharmacy_queue_desk.html", {
        **pharmacy_context(), "page_title": "Dispensing desk", "query": query, "tab": tab,
        "label": label, "queue_counts": counts,
        "tabs": [(key, title, counts[key]) for key, (title, _) in queues.items()],
        "records": page, "queue_state": state.urlencode(), "stale_only": stale_only,
        "low_count": stocked_medicines(today).filter(usable__lte=F("reorder_level")).count(),
        "expiry_count": expiring_batches(today).count(), "stale_count": stale.count(),
        "has_stock": StockMovement.objects.exists(),
    })


CATALOGUE_IDENTITY_FIELDS = ("name", "strength", "form", "unit", "pack_size")


@login_required(login_url="login")
def medicine_catalogue(request, medicine_id=None):
    require_pharmacy(request.user)
    medicine = get_object_or_404(Medicine, pk=medicine_id) if medicine_id else None
    form = MedicineForm(request.POST or None, instance=medicine)
    locked = bool(medicine and medicine.batches.exists())
    if locked:
        # Identity is fixed once stock exists; lock the widgets up front and let
        # the post-save guard below stay as the authoritative backstop.
        for field_name in CATALOGUE_IDENTITY_FIELDS:
            if field_name == "unit":
                form.fields[field_name].widget.attrs["disabled"] = True
            else:
                form.fields[field_name].widget.attrs["readonly"] = True
    if request.method == "POST" and form.is_valid():
        original = Medicine.objects.filter(pk=medicine_id).first() if medicine_id else None
        if original and original.batches.exists() and any(form.cleaned_data[f] != getattr(original, f) for f in CATALOGUE_IDENTITY_FIELDS):
            form.add_error(None, "Unit and pack size cannot change after receiving stock; medicine identity is also fixed. Create a separate catalogue entry.")
        else:
            saved = form.save()
            messages.success(request, "Medicine catalogue saved.")
            state = request.GET.copy()
            state.pop("page", None)
            state["saved"] = saved.pk
            return redirect(f"{reverse('medicine_catalogue')}?{state.urlencode()}")
    today = timezone.localdate()
    warn_days = PharmacySettings.current().expiry_warning_days
    medicines = Medicine.objects.annotate(
        usable=Sum(F("batches__on_hand") - F("batches__reserved") - F("batches__damaged") - F("batches__quarantined"),
                   filter=Q(batches__expiry_date__gte=today, batches__status=MedicineBatch.Status.ACTIVE), default=0),
        batch_count=Count("batches"),
        expiring_count=Count("batches", filter=Q(
            batches__on_hand__gt=0, batches__status=MedicineBatch.Status.ACTIVE,
            batches__expiry_date__range=(today, today + timedelta(days=warn_days))),
        ),
    )
    query = request.GET.get("q", "").strip()
    if query:
        medicines = medicines.filter(Q(name__icontains=query) | Q(generic_name__icontains=query)
                                      | Q(manufacturer__icontains=query) | Q(barcode__icontains=query))
    active_form = request.GET.get("form", "").strip()
    if active_form:
        medicines = medicines.filter(form__iexact=active_form)
    counts = {
        "total": medicines.count(),
        "active": medicines.filter(is_active=True).count(),
        "low": medicines.filter(is_active=True, usable__lte=F("reorder_level")).count(),
        "unstocked": medicines.filter(batch_count=0).count(),
        "expiring": medicines.filter(expiring_count__gt=0).count(),
    }
    status_filter = request.GET.get("status", "").strip()
    if status_filter == "active":
        medicines = medicines.filter(is_active=True)
    elif status_filter == "inactive":
        medicines = medicines.filter(is_active=False)
    stock_filter = request.GET.get("stock", "").strip()
    if stock_filter == "low":
        medicines = medicines.filter(is_active=True, usable__lte=F("reorder_level"))
    elif stock_filter == "unstocked":
        medicines = medicines.filter(batch_count=0)
    elif stock_filter == "expiring":
        medicines = medicines.filter(expiring_count__gt=0)
    sort = request.GET.get("sort", "").strip()
    if sort == "stock":
        medicines = medicines.order_by("usable", "name")
    elif sort == "recent":
        medicines = medicines.order_by("-pk")
    else:
        medicines = medicines.order_by("name")
    try:
        saved_pk = int(request.GET.get("saved", ""))
    except (TypeError, ValueError):
        saved_pk = 0
    kpi_current = "total"
    if stock_filter in ("low", "unstocked", "expiring"):
        kpi_current = stock_filter
    elif status_filter == "active":
        kpi_current = "active"
    return render(request, "core/pharmacy_catalogue.html", {**pharmacy_context(),
        "page_title": "Medicine catalogue", "form": form, "editing": medicine, "locked": locked,
        "price_batches": [{"batch": batch, "request_key": uuid4()} for batch in medicine.batches.order_by("expiry_date", "id")] if medicine else [],
        "medicines": Paginator(medicines, 25).get_page(request.GET.get("page")), "query": query,
        "form_options": Medicine.objects.values_list("form", flat=True).distinct().order_by("form"),
        "active_form": active_form, "status_filter": status_filter, "stock_filter": stock_filter, "sort": sort,
        "counts": counts, "saved_pk": saved_pk, "kpi_current": kpi_current, "warn_days": warn_days})


@login_required(login_url="login")
@require_POST
def medicine_batch_prices(request, medicine_id):
    require_pharmacy(request.user)
    medicine = get_object_or_404(Medicine, pk=medicine_id)
    form = BatchPriceForm(request.POST)
    if form.is_valid() and form.cleaned_data["batch"].medicine_id == medicine.pk:
        try:
            update_batch_prices(user=request.user, **{k: v for k, v in form.cleaned_data.items() if k != "batch"},
                                batch=form.cleaned_data["batch"])
        except (ValidationError, OperationalError, IntegrityError) as exc:
            messages.error(request, error_message(exc))
        else:
            messages.success(request, "Batch prices updated.")
    else:
        messages.error(request, "Check the batch prices and try again.")
    return redirect(f"{reverse('medicine_edit', args=[medicine.pk])}#medicine-form")


@login_required(login_url="login")
def pharmacy_receive(request):
    require_pharmacy(request.user)
    initial = {"request_key": uuid4()}
    # Catalogue cards link here with ?medicine=<pk> to prefill the selector.
    prefill = request.GET.get("medicine", "").strip()
    if prefill.isdigit() and Medicine.objects.filter(pk=prefill, is_active=True).exists():
        initial["medicine"] = int(prefill)
    form = ReceiptForm(request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        try:
            receive_stock(user=request.user, **form.cleaned_data)
        except (ValidationError, OperationalError, IntegrityError) as exc:
            form.add_error(None, error_message(exc))
        else:
            messages.success(request, "Stock receipt recorded.")
            return redirect("pharmacy_stock_report")
    return render(request, "core/pharmacy_stock_form.html", {**pharmacy_context(),
        "pack_sizes": dict(form.fields["medicine"].queryset.values_list("pk", "pack_size")), "page_title": "Receive stock", "intro": "Record an opening stock count or a delivery. Prices are per dispensing unit.", "form": form, "submit_label": "Record receipt"})


@login_required(login_url="login")
def pharmacy_adjust(request):
    require_admin(request.user)
    form = AdjustmentForm(request.POST or None, initial={"request_key": uuid4()})
    if request.method == "POST" and form.is_valid():
        try:
            adjust_stock(user=request.user, **form.cleaned_data)
        except (ValidationError, OperationalError, IntegrityError) as exc:
            form.add_error(None, error_message(exc))
        else:
            messages.success(request, "Stock correction recorded in the ledger.")
            return redirect("pharmacy_stock_report")
    balances = {batch.pk: {"on_hand": batch.on_hand, "reserved": batch.reserved,
                           "available": batch.available if batch.expiry_date >= timezone.localdate() else 0}
                for batch in form.fields["batch"].queryset}
    return render(request, "core/pharmacy_stock_form.html", {**pharmacy_context(), "balances": balances, "page_title": "Adjust stock", "intro": "Record a stock-taking correction with a reason. Reserved stock cannot be removed.", "form": form, "submit_label": "Record adjustment"})


@login_required(login_url="login")
def pharmacy_stock_report(request):
    require_pharmacy(request.user)
    today = timezone.localdate()
    batches = MedicineBatch.objects.select_related("medicine")
    query = request.GET.get("q", "").strip()
    if query:
        batches = batches.filter(Q(medicine__name__icontains=query) | Q(batch_number__icontains=query))
    stock_filter = request.GET.get("filter", "")
    low = stocked_medicines(today).filter(usable__lte=F("reorder_level"))
    if stock_filter == "low":
        batches = batches.filter(medicine__in=low)
    elif stock_filter == "out":
        out = stocked_medicines(today).filter(usable=0)
        batches = batches.filter(medicine__in=out)
    elif stock_filter == "quarantined":
        batches = batches.filter(quarantined__gt=0)
    elif stock_filter == "expiring":
        batches = batches.filter(pk__in=expiring_batches(today))
    elif stock_filter == "expired":
        batches = batches.filter(expiry_date__lt=today)
    unstocked = low.filter(batches__isnull=True, name__icontains=query)
    warn_days = PharmacySettings.current().expiry_warning_days
    rows = list(batches)
    for batch in rows:
        batch.expired = batch.expiry_date < today
        batch.expiring = today <= batch.expiry_date <= today + timedelta(days=warn_days)
    counts = {
        "all": MedicineBatch.objects.count(),
        "low": low.count(),
        "out": stocked_medicines(today).filter(usable=0).count(),
        "quarantined": MedicineBatch.objects.filter(quarantined__gt=0).count(),
        "expiring": expiring_batches(today).count(),
        "expired": MedicineBatch.objects.filter(expiry_date__lt=today).count(),
    }
    if request.GET.get("export") == "csv":
        import csv
        from django.http import HttpResponse
        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = 'attachment; filename="pharmacy-batch-stock.csv"'
        writer = csv.writer(response)
        writer.writerow(["Medicine", "Batch", "Expiry", "On hand", "Reserved", "Available", "Unit price"])
        for batch in rows:
            name = ("'" + batch.medicine.name) if batch.medicine.name[:1] in ("=", "+", "-", "@") else batch.medicine.name
            number = ("'" + batch.batch_number) if batch.batch_number[:1] in ("=", "+", "-", "@") else batch.batch_number
            writer.writerow([name, number, batch.expiry_date, batch.on_hand, batch.reserved, 0 if batch.expired else batch.available, batch.selling_price])
        if stock_filter == "low":
            for medicine in unstocked:
                name = medicine.name
                writer.writerow(["'" + name if name[:1] in ("=", "+", "-", "@") else name, "", "", 0, 0, 0, ""])
        return response
    movements = StockMovement.objects.select_related(
        "batch__medicine", "actor", "sale", "issue", "purchase")
    movement_kind = request.GET.get("movement_kind", "")
    kinds = dict(StockMovement._meta.get_field("kind").choices)
    if movement_kind in kinds:
        movements = movements.filter(kind=movement_kind)
    else:
        movement_kind = ""
    movement_q = request.GET.get("movement_q", "").strip()
    if movement_q:
        movements = movements.filter(
            Q(batch__medicine__name__icontains=movement_q)
            | Q(batch__batch_number__icontains=movement_q)
            | Q(reason__icontains=movement_q))
    return render(request, "core/pharmacy_stock_report.html", {
        **pharmacy_context(), "page_title": "Batch inventory", "batches": rows, "query": query,
        "low_medicines": low.filter(name__icontains=query),
        "unstocked": unstocked if stock_filter == "low" else [], "stock_filter": stock_filter,
        "counts": counts, "movement_kinds": StockMovement._meta.get_field("kind").choices,
        "movement_kind": movement_kind, "movement_q": movement_q,
        "movements": Paginator(movements, 50).get_page(request.GET.get("page")),
    })


@login_required(login_url="login")
def pharmacy_queue(request, consultation_id=None):
    require_pharmacy(request.user)
    if consultation_id is None:
        prescription = request.GET.get("prescription", "")
        if prescription.isdigit():
            rx = get_object_or_404(PrescriptionItem, pk=prescription, released_at__isnull=False)
            return redirect("pharmacy_consultation", consultation_id=rx.consultation_id)
        return dispensing_desk(request, default_tab="all" if request.GET.get("status") == "all" else "ready")
    selected = get_object_or_404(
        ConsultationSession.objects.select_related("patient", "doctor").filter(prescriptions__released_at__isnull=False).distinct(),
        pk=consultation_id,
    )
    rows, can_prepare = prescription_preparation(selected, request.POST if request.method == "POST" else None)
    if request.method == "POST":
        entries = [{"prescription_id": rx.pk, "medicine_id": request.POST.get(f"medicine_{rx.pk}"), "quantity": request.POST.get(f"quantity_{rx.pk}")} for rx in rows if request.POST.get(f"quantity_{rx.pk}", "").strip() not in ("", "0")]
        try:
            issue = reserve_issue(user=request.user, consultation=selected, entries=entries, request_key=request.POST.get("request_key"))
        except (ValidationError, OperationalError, IntegrityError) as exc:
            messages.error(request, error_message(exc))
        else:
            if request.GET.get("next") == "pos":
                return redirect(reverse("pharmacy_pos") + f"?issue={issue.pk}")
            return redirect(reverse("pharmacy_issue", args=[issue.pk], query=request.GET))
    return render(request, "core/pharmacy_queue.html", {
        **pharmacy_context(), "page_title": "Prepare prescription", "can_prepare": can_prepare,
        "selected": selected, "rows": rows, "request_key": request.POST.get("request_key") or uuid4(), "issues": selected.pharmacy_issues.select_related("bill") if selected else []})


@login_required(login_url="login")
def pharmacy_issue(request, issue_id):
    if not is_admin(request.user) and designation(request.user) not in ("pharmacy", "registration_desk"):
        raise PermissionDenied
    issue = get_object_or_404(PharmacyIssue.objects.select_related("consultation__patient", "consultation__doctor", "bill", "dispensed_by"), pk=issue_id)
    if request.method == "POST":
        require_pharmacy(request.user)
        action = request.POST.get("action")
        handler = {"handover": handover_issue, "cancel": cancel_issue, "reallocate": reallocate_issue}.get(action)
        try:
            if action == "pay":
                set_bill_status(
                    user=request.user,
                    bill=issue.bill,
                    next_status="paid",
                    payment_method=request.POST.get("payment_method", ""),
                    payment_reference=request.POST.get("payment_reference", ""),
                )
            elif handler:
                handler(user=request.user, issue=issue)
            else:
                raise ValidationError("Choose a valid issue action.")
        except (ValidationError, OperationalError, IntegrityError) as exc:
            messages.error(request, error_message(exc))
        else:
            messages.success(request, {
                "pay": "Payment recorded.",
                "handover": "Medicines handed over and stock updated.",
                "cancel": "Unpaid issue cancelled; reservations released.",
                "reallocate": "Valid batches allocated; billed prices preserved.",
            }[action])
        if request.GET.get("next") == "pos":
            return redirect(reverse("pharmacy_pos") + f"?issue={issue.pk}")
        return redirect(reverse("pharmacy_issue", args=[issue.pk], query=request.GET))
    return render(request, "core/pharmacy_issue.html", {**pharmacy_context(), "page_title": f"Pharmacy issue #{issue.pk}", "issue": issue, "lines": issue.lines.select_related("prescription", "medicine").prefetch_related("allocations__batch"), "can_dispense": is_admin(request.user) or designation(request.user) == "pharmacy", "other_dues": PatientBill.objects.filter(patient=issue.consultation.patient, status="pending").exclude(pk=issue.bill_id)})


@login_required(login_url="login")
def pharmacy_dispense_history(request):
    require_pharmacy(request.user)
    issues = PharmacyIssue.objects.filter(status="dispensed").select_related("consultation__patient", "dispensed_by", "bill")
    legacy = PrescriptionItem.objects.filter(legacy_dispensed_quantity__gt=0).select_related("consultation__patient", "dispensed_by")
    query = request.GET.get("q", "").strip()
    if query:
        visits = searched_visits(query)
        issues = issues.filter(consultation__in=visits)
        legacy = legacy.filter(consultation__in=visits)
    mine = request.GET.get("scope") == "mine"
    if mine:
        issues = issues.filter(dispensed_by=request.user)
    for field, lookup in (("from", "dispensed_at__date__gte"), ("to", "dispensed_at__date__lte")):
        value = request.GET.get(field)
        if value:
            from django.utils.dateparse import parse_date
            try:
                parsed = parse_date(value)
            except ValueError:
                parsed = None
            if parsed:
                issues = issues.filter(**{lookup: parsed})
                legacy = legacy.filter(**{lookup.replace("dispensed_at", "updated_at"): parsed})
    if mine:
        legacy = legacy.filter(dispensed_by=request.user)
    return render(request, "core/pharmacy_dispense_history.html", {**pharmacy_context(), "page_title": "Dispensing history", "query": query, "issues": Paginator(issues.order_by("-dispensed_at"), 25).get_page(request.GET.get("page")), "legacy": Paginator(legacy.order_by("-updated_at"), 50).get_page(request.GET.get("legacy_page")), "mine_only": mine})


@login_required(login_url="login")
def pharmacy_billing(request):
    if not is_admin(request.user) and designation(request.user) != "registration_desk":
        raise PermissionDenied
    issues = PharmacyIssue.objects.select_related("bill", "consultation__patient")
    query = request.GET.get("q", "").strip()
    if query:
        issues = issues.filter(Q(consultation__patient__first_name__icontains=query) | Q(consultation__patient__uhid__icontains=query))
    return render(request, "core/pharmacy_billing.html", {"page_title": "Pharmacy billing", "issues": Paginator(issues, 25).get_page(request.GET.get("page")), "query": query})


@login_required(login_url="login")
@require_POST
def bill_status(request, bill_id):
    bill = get_object_or_404(PatientBill, pk=bill_id)
    try:
        set_bill_status(
            user=request.user,
            bill=bill,
            next_status=request.POST.get("status"),
            payment_method=request.POST.get("payment_method", ""),
            payment_reference=request.POST.get("payment_reference", ""),
        )
    except (ValidationError, OperationalError, IntegrityError) as exc:
        messages.error(request, error_message(exc))
    else:
        messages.success(request, "Payment status saved.")
    return redirect("patient_bill_slip", bill_id=bill.pk)


@login_required(login_url="login")
@require_POST
def prescription_cancel(request, item_id):
    rx = get_object_or_404(PrescriptionItem.objects.select_related("consultation__patient"), pk=item_id)
    try:
        cancel_prescription(user=request.user, prescription=rx)
    except (ValidationError, OperationalError, IntegrityError) as exc:
        if request.headers.get("Accept") == "application/json":
            return JsonResponse({"ok": False, "error": error_message(exc)}, status=400)
        messages.error(request, error_message(exc))
    else:
        if request.headers.get("Accept") == "application/json":
            return JsonResponse({"ok": True})
    return redirect(f"/doctor/consultation/?patient={rx.consultation.patient_id}")


@login_required(login_url="login")
@require_POST
def legacy_dispense_update(request, item_id):
    require_pharmacy(request.user)
    rx = get_object_or_404(PrescriptionItem, pk=item_id)
    messages.error(request, "Prepare an itemized pharmacy issue, collect payment, then record handover. Direct status updates cannot deduct stock.")
    return redirect("pharmacy_consultation", consultation_id=rx.consultation_id)


@login_required(login_url="login")
def service_dashboard(request):
    if not is_admin(request.user) and designation(request.user) != "service_staff":
        raise PermissionDenied
    orders = scoped_orders(request.user).select_related("consultation__patient", "consultation__doctor", "department", "assigned_staff")
    if not is_admin(request.user) and designation(request.user) == "service_staff":
        orders = orders.filter(
            Q(bill__isnull=True) | Q(bill__status=PatientBill.Status.PAID)
        )
    departments = ServiceDepartment.objects.all() if is_admin(request.user) else request.user.profile.service_departments.all()
    open_orders = orders.exclude(status__in=["Completed", "Cancelled"])
    queues = []
    for department in departments:
        meta = DEPARTMENT_META.get(department.slug, {})
        queues.append({
            "slug": department.slug,
            "title": department.name,
            "open_count": open_orders.filter(department=department).count(),
            "kind": meta.get("kind", "Service desk"),
            "tone": meta.get("tone", "slate"),
        })
    busiest = max(queues, key=lambda queue: queue["open_count"]) if queues else None
    today = timezone.localdate()
    stats = {
        "total_open": open_orders.count(),
        "in_progress": open_orders.filter(status="In Progress").count(),
        "review_required": open_orders.filter(status="Review Required").count(),
        "completed_today": orders.filter(completed_at__date=today).count(),
        "departments": departments.count() if hasattr(departments, "count") else len(queues),
        "urgent": open_orders.filter(priority="urgent").count(),
    }
    session_orders = orders.filter(department__slug__in=SESSION_DEPARTMENTS)
    stats["sessions_today"] = ServiceSession.objects.filter(
        status=ServiceSession.Status.ATTENDED,
        recorded_at__date=today,
        order__in=session_orders,
    ).count()
    recent_activity = (
        OPDAuditEvent.objects.select_related("actor")
        .filter(order__in=orders)
        .order_by("-occurred_at")[:8]
    )
    return render(request, "core/service_dashboard.html", {
        "page_title": "Service operations",
        "today": today,
        "queues": queues,
        "orders": open_orders.order_by("-priority", "created_at")[:20],
        "in_progress_orders": open_orders.filter(status="In Progress").order_by("-updated_at")[:5],
        "review_orders": open_orders.filter(status="Review Required").order_by("-updated_at")[:5],
        "recent_activity": recent_activity,
        "busiest": busiest,
        "stats": stats,
        "is_admin": is_admin(request.user),
        "unassigned_count": ClinicalOrder.objects.filter(department__isnull=True).exclude(status="Cancelled").count() if is_admin(request.user) else 0,
    })


DEPARTMENT_META = {
    "laboratory": {"kind": "Samples · results · critical flags", "tone": "teal"},
    "radiology": {"kind": "X-Ray and USG studies", "tone": "lav"},
    "cardiology": {"kind": "ECG acquisition", "tone": "rose"},
    "physiotherapy": {"kind": "Session plans and progress", "tone": "amber"},
    "counselling": {"kind": "Private sessions", "tone": "violet"},
    "procedure-room": {"kind": "Minor procedures", "tone": "slate"},
    "procedures": {"kind": "Nursing procedures", "tone": "slate"},
}

SESSION_DEPARTMENTS = ("physiotherapy", "counselling")


def can_process_department(user, department):
    return is_admin(user) or (
        designation(user) == "service_staff"
        and department is not None
        and user.profile.service_departments.filter(pk=department.pk).exists()
    )


def require_service_manager(user, order):
    if not can_process_department(user, order.department):
        raise PermissionDenied("Only staff assigned to this department can manage sessions.")


def paid_or_unbilled(order):
    return not order.bill_id or order.bill.status == PatientBill.Status.PAID


def parse_local_datetime(value):
    from django.utils.dateparse import parse_datetime
    parsed = parse_datetime((value or "").strip())
    if parsed is None:
        return None
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed)
    return parsed


def radiology_modality_q(key):
    """Match an order's study by catalogue modality, falling back to the
    staff-recorded modality for historical free-text orders."""
    if key == "unspecified":
        return ~(
            Q(service_definition__modality__in=["xray", "usg"])
            | Q(operational_details__modality__in=["xray", "usg"])
        )
    return Q(service_definition__modality=key) | (
        Q(service_definition__isnull=True) & Q(operational_details__modality=key)
    )


@login_required(login_url="login")
def service_queue(request, service):
    if service == "pharmacy":
        return pharmacy_queue(request)
    department = get_object_or_404(ServiceDepartment, slug=service)
    if not is_admin(request.user) and (designation(request.user) not in ("doctor", "opd_assistant", "service_staff") or (designation(request.user) == "service_staff" and not request.user.profile.service_departments.filter(pk=department.pk).exists())):
        raise PermissionDenied
    orders = scoped_orders(request.user).filter(department=department).select_related("consultation__patient", "consultation__doctor", "assigned_staff", "service_definition")
    if not is_admin(request.user) and designation(request.user) == "service_staff":
        orders = orders.filter(
            Q(bill__isnull=True) | Q(bill__status=PatientBill.Status.PAID)
        )

    modality_filter = request.GET.get("modality", "").strip()
    modality_tabs = []
    if department.slug == "radiology":
        open_orders_for_tabs = orders.exclude(status__in=["Completed", "Cancelled"])
        if modality_filter in ("xray", "usg"):
            orders = orders.filter(radiology_modality_q(modality_filter))
        elif modality_filter == "unspecified":
            orders = orders.filter(radiology_modality_q("unspecified"))
        else:
            modality_filter = ""
        modality_tabs = [
            {"key": "", "label": "All studies", "count": open_orders_for_tabs.count()},
            {"key": "xray", "label": "X-Ray", "count": open_orders_for_tabs.filter(radiology_modality_q("xray")).count()},
            {"key": "usg", "label": "USG", "count": open_orders_for_tabs.filter(radiology_modality_q("usg")).count()},
            {"key": "unspecified", "label": "Unspecified", "count": open_orders_for_tabs.filter(radiology_modality_q("unspecified")).count()},
        ]

    query = request.GET.get("q", "").strip()
    if query:
        orders = orders.filter(Q(name__icontains=query) | Q(consultation__patient__first_name__icontains=query) | Q(consultation__patient__uhid__icontains=query) | Q(consultation__visit_token__icontains=query))
    status = request.GET.get("status", "")
    if status:
        orders = orders.filter(status=status)
    selected = get_object_or_404(orders, pk=request.GET["order"]) if request.GET.get("order", "").isdigit() else None
    report_form = ReportForm(initial={"title": selected.name, "summary": selected.result_summary}) if selected else None
    staff = get_user_model().objects.filter(is_active=True, profile__designation="service_staff", profile__service_departments=department)
    sessions = selected.sessions.select_related("recorded_by") if selected else []
    can_view_session_notes = (
        is_admin(request.user)
        or designation(request.user) == "doctor"
        or (designation(request.user) == "service_staff" and can_process_department(request.user, department))
    )
    return render(request, "core/service_queue.html", {
        "page_title": department.name,
        "department": department,
        "department_meta": DEPARTMENT_META.get(department.slug, {}),
        "supports_sessions": department.slug in SESSION_DEPARTMENTS,
        "orders": Paginator(orders.order_by("-priority", "created_at"), 25).get_page(request.GET.get("page")),
        "selected": selected,
        "selected_reports": accessible_reports(request.user).filter(order=selected) if selected else [],
        "sessions": sessions,
        "can_view_session_notes": can_view_session_notes,
        "can_manage_sessions": bool(selected) and can_process_department(request.user, department),
        "query": query,
        "status_filter": status,
        "statuses": ["Ordered", "In Progress", "Review Required", "Completed", "Cancelled"],
        "staff": staff,
        "report_form": report_form,
        "modality_tabs": modality_tabs,
        "modality_filter": modality_filter,
        "can_update": is_admin(request.user) or designation(request.user) in ("opd_assistant", "service_staff"),
        "can_publish": is_admin(request.user) or designation(request.user) == "service_staff",
        "can_manage_plan": is_admin(request.user) or designation(request.user) == "service_staff",
        "today": timezone.localdate(),
    })


@login_required(login_url="login")
@require_POST
def service_order_update(request, order_id):
    if not is_admin(request.user) and designation(request.user) not in ("opd_assistant", "service_staff"):
        raise PermissionDenied
    order = get_object_or_404(scoped_orders(request.user).select_related("consultation__patient", "department", "bill"), pk=order_id)
    if not order.department_id:
        raise PermissionDenied("Assign a department before processing this order.")
    if order.bill_id and order.bill.status != PatientBill.Status.PAID:
        raise PermissionDenied(
            "This service must be paid at the registration desk before processing."
        )
    new_status = request.POST.get("status", "")
    try:
        with transaction.atomic():
            ClinicalOrder.objects.filter(pk=order.pk).update(status=F("status"))
            order.refresh_from_db()
            if new_status not in ["Ordered", "In Progress", "Review Required", "Completed", "Cancelled"]:
                raise ValidationError("Choose a valid service status.")
            if order.status in ("Completed", "Cancelled") and new_status != order.status:
                raise ValidationError("A finished order cannot be reopened. Request a new service order.")
            staff_id = request.POST.get("assigned_staff", "")
            if staff_id:
                staff = get_user_model().objects.filter(pk=staff_id, is_active=True, profile__designation="service_staff", profile__service_departments=order.department).first() if staff_id.isdigit() else None
                if not staff:
                    raise ValidationError("Choose a staff member assigned to this department.")
                order.assigned_staff = staff
            elif "assigned_staff" in request.POST:
                order.assigned_staff = None
            previous = order.status
            order.status = new_status
            priority = request.POST.get("priority", order.priority)
            if priority not in ("routine", "urgent"):
                raise ValidationError("Choose routine or urgent priority.")
            order.priority = priority
            if order.department.slug != "counselling" or is_admin(request.user) or designation(request.user) == "service_staff":
                order.result_summary = request.POST.get("result_summary", order.result_summary).strip()[:255]
            details = dict(order.operational_details or {})
            for field in ("body_part", "preparation_status", "preparation_note", "specimen_status", "acquisition_status", "session_datetime", "room", "post_instructions", "view_position"):
                if field in request.POST:
                    details[field] = request.POST[field].strip()[:255]
            now = timezone.now()
            if order.department.slug == "laboratory":
                if "critical_flag" in request.POST or "specimen_status" in request.POST:
                    details["critical_flag"] = request.POST.get("critical_flag") == "on"
                if details.get("specimen_status") == "collected" and not details.get("collected_at"):
                    details["collected_at"] = now.isoformat()
            if order.department.slug == "radiology":
                modality = request.POST.get("modality", details.get("modality", "")).strip()
                if modality not in ("", "xray", "usg"):
                    raise ValidationError("Choose X-Ray, USG, or unspecified.")
                details["modality"] = modality
                for key, label in (("prep_none", "No preparation"), ("prep_fasting", "Fasting"), ("prep_full_bladder", "Full bladder")):
                    if key in request.POST:
                        details[key] = request.POST.get(key) == "on"
            if order.department.slug == "cardiology":
                if "acquisition_status" in request.POST:
                    details["acquisition_status"] = request.POST.get("acquisition_status", "").strip()[:255]
                if details.get("acquisition_status") == "acquired" and not details.get("acquired_at"):
                    details["acquired_at"] = now.isoformat()
            if order.department.slug == "procedure-room":
                details["consent_confirmed"] = request.POST.get("consent_confirmed") == "on"
                details["preparation_complete"] = request.POST.get("preparation_complete") == "on"
                if "post_instructions" in request.POST:
                    details["post_instructions"] = request.POST.get("post_instructions", "").strip()[:255]
            if order.department.slug in SESSION_DEPARTMENTS and ("sessions_planned" in request.POST):
                if not can_process_department(request.user, order.department):
                    raise PermissionDenied("Only service staff can change the session plan.")
                planned = request.POST.get("sessions_planned", "").strip()
                if planned == "":
                    order.sessions_planned = None
                elif planned.isdigit() and 1 <= int(planned) <= 100:
                    order.sessions_planned = int(planned)
                else:
                    raise ValidationError("Planned sessions must be a whole number from 1 to 100.")
            order.operational_details = details
            if new_status in ("In Progress", "Completed", "Review Required") and not order.started_at:
                order.started_at = now
            if new_status == "Completed" and not order.completed_at:
                order.completed_at = now
                order.completed_by = request.user
            order.updated_by = request.user
            order.save()
            OPDAuditEvent.objects.create(patient=order.consultation.patient, order=order, actor=request.user, action="service_order_updated", summary=f"{order.name}: {previous} to {new_status}"[:255], details={"previous_status": previous, "status": new_status, **details})
            from core.nursing import sync_task_from_order
            sync_task_from_order(order, request.user)
    except (ValidationError, OperationalError) as exc:
        messages.error(request, error_message(exc))
    else:
        messages.success(request, "Service order updated.")
    return redirect("opd_service_queue", service=order.department.slug)


def maybe_complete_session_plan(order, user):
    """Complete the order once every planned session has been attended."""
    if not order.sessions_planned or order.status in ("Completed", "Cancelled"):
        return False
    if order.attended_session_count < order.sessions_planned:
        return False
    now = timezone.now()
    previous = order.status
    order.status = "Completed"
    order.started_at = order.started_at or now
    order.completed_at = order.completed_at or now
    order.completed_by = order.completed_by or user
    order.updated_by = user
    order.save(update_fields=["status", "started_at", "completed_at", "completed_by", "updated_by", "updated_at"])
    event(
        user,
        order.consultation.patient,
        "service_plan_completed",
        f"{order.name}: plan completed · {order.attended_session_count}/{order.sessions_planned} sessions",
        order_id=order.pk,
        previous_status=previous,
        status=order.status,
    )
    return True


@login_required(login_url="login")
@require_POST
def service_session_add(request, order_id):
    order = get_object_or_404(
        scoped_orders(request.user).select_related("consultation__patient", "department", "bill"),
        pk=order_id,
    )
    require_service_manager(request.user, order)
    if order.department.slug not in SESSION_DEPARTMENTS:
        raise PermissionDenied("Sessions are only used by physiotherapy and counselling.")
    if order.status in ("Completed", "Cancelled"):
        messages.error(request, "A finished plan cannot be rescheduled. Request a new service order.")
        return redirect(f"{reverse('opd_service_queue', args=[order.department.slug])}?order={order.pk}")
    if not paid_or_unbilled(order):
        messages.error(request, "Sessions can be scheduled after the bill is paid at the registration desk.")
        return redirect(f"{reverse('opd_service_queue', args=[order.department.slug])}?order={order.pk}")
    scheduled_at = parse_local_datetime(request.POST.get("scheduled_at"))
    room = request.POST.get("room", "").strip()[:100]
    note = request.POST.get("progress_note", "").strip()
    try:
        with transaction.atomic():
            ServiceSession.objects.create(
                order=order,
                scheduled_at=scheduled_at,
                room=room,
                progress_note=note,
                created_by=request.user,
            )
            if not order.sessions_planned:
                order.sessions_planned = 1
                order.updated_by = request.user
                order.save(update_fields=["sessions_planned", "updated_by", "updated_at"])
            event(
                request.user,
                order.consultation.patient,
                "service_session_scheduled",
                f"{order.name}: session scheduled",
                order_id=order.pk,
                has_note=bool(note),
            )
    except (ValidationError, OperationalError) as exc:
        messages.error(request, error_message(exc))
    else:
        messages.success(request, "Session scheduled.")
    return redirect(f"{reverse('opd_service_queue', args=[order.department.slug])}?order={order.pk}")


@login_required(login_url="login")
@require_POST
def service_session_update(request, session_id):
    session = get_object_or_404(
        ServiceSession.objects.select_related("order__consultation__patient", "order__department"),
        pk=session_id,
    )
    order = session.order
    if order not in scoped_orders(request.user):
        raise PermissionDenied
    require_service_manager(request.user, order)
    status = request.POST.get("status", session.status)
    if status not in ServiceSession.Status.values:
        messages.error(request, "Choose a valid session status.")
        return redirect(f"{reverse('opd_service_queue', args=[order.department.slug])}?order={order.pk}")
    try:
        with transaction.atomic():
            session.status = status
            scheduled_at = parse_local_datetime(request.POST.get("scheduled_at"))
            if scheduled_at:
                session.scheduled_at = scheduled_at
            if "room" in request.POST:
                session.room = request.POST.get("room", "").strip()[:100]
            if "progress_note" in request.POST:
                session.progress_note = request.POST.get("progress_note", "").strip()
            if status in (ServiceSession.Status.ATTENDED, ServiceSession.Status.NO_SHOW) and not session.recorded_at:
                session.recorded_by = request.user
                session.recorded_at = timezone.now()
            session.save()
            if order.status == "Ordered" and status == ServiceSession.Status.ATTENDED:
                order.status = "In Progress"
                order.started_at = order.started_at or timezone.now()
                order.updated_by = request.user
                order.save(update_fields=["status", "started_at", "updated_by", "updated_at"])
            event(
                request.user,
                order.consultation.patient,
                "service_session_recorded",
                f"{order.name}: session {session.get_status_display().lower()}",
                order_id=order.pk,
                session_id=session.pk,
                has_note=bool(session.progress_note),
            )
            if status == ServiceSession.Status.ATTENDED:
                maybe_complete_session_plan(order, request.user)
    except (ValidationError, OperationalError) as exc:
        messages.error(request, error_message(exc))
    else:
        messages.success(request, "Session updated.")
    return redirect(f"{reverse('opd_service_queue', args=[order.department.slug])}?order={order.pk}")


@login_required(login_url="login")
def service_completed_today(request):
    if not is_admin(request.user) and designation(request.user) != "service_staff":
        raise PermissionDenied
    orders = scoped_orders(request.user).filter(completed_at__date=timezone.localdate()).select_related("consultation__patient", "consultation__doctor", "updated_by", "department")
    sessions = ServiceSession.objects.filter(
        status=ServiceSession.Status.ATTENDED,
        recorded_at__date=timezone.localdate(),
        order__in=scoped_orders(request.user),
    ).select_related("order__consultation__patient", "order__department", "recorded_by")
    return render(request, "core/service_completed_today.html", {
        "page_title": "Completed today",
        "orders": orders.order_by("-completed_at"),
        "sessions": sessions.order_by("-recorded_at"),
        "session_count": sessions.count(),
        "today": timezone.localdate(),
    })


@login_required(login_url="login")
def service_setup(request):
    if not is_admin(request.user):
        raise PermissionDenied
    definition = ServiceDefinition.objects.filter(pk=request.GET.get("edit")).first() if request.GET.get("edit", "").isdigit() else None
    form = ServiceDefinitionForm(request.POST or None, instance=definition)
    if request.method == "POST":
        if request.POST.get("action") == "route":
            order = get_object_or_404(ClinicalOrder, pk=request.POST.get("order_id"), department__isnull=True)
            department = get_object_or_404(ServiceDepartment, pk=request.POST.get("department_id"))
            order.department = department
            order.save(update_fields=["department"])
            event(request.user, order.consultation.patient, "service_routed", f"{order.name} assigned to {department.name}", order_id=order.pk, department_id=department.pk)
            return redirect("service_setup")
        if form.is_valid():
            form.save()
            messages.success(request, "Service catalogue saved.")
            return redirect("service_setup")
    return render(request, "core/service_setup.html", {"page_title": "Service catalogue & routing", "form": form, "definitions": ServiceDefinition.objects.select_related("department"), "departments": ServiceDepartment.objects.all(), "unassigned": ClinicalOrder.objects.filter(department__isnull=True).exclude(status="Cancelled").select_related("consultation__patient"), "editing": definition})


@login_required(login_url="login")
@require_POST
def report_publish(request, order_id):
    if not is_admin(request.user) and designation(request.user) != "service_staff":
        raise PermissionDenied
    order = get_object_or_404(scoped_orders(request.user).select_related("department", "consultation__patient"), pk=order_id)
    if order.status == "Cancelled" or not order.department_id:
        raise PermissionDenied("A report needs an active, routed order.")
    form = ReportForm(request.POST, request.FILES)
    if form.is_valid():
        data = form.cleaned_data
        report = DiagnosticReport(patient=order.consultation.patient, order=order, title=data["title"], summary=data["summary"], author=request.user, published_at=timezone.now(), attachment=data["attachment"], category=order.department.slug if order.department.slug in ("laboratory", "radiology", "cardiology") else "documents")
        try:
            with transaction.atomic():
                report.save()
                event(request.user, report.patient, "service_report_published", f"Report published: {report.title}", report_id=report.pk, order_id=order.pk)
        except Exception:
            if report.attachment.name:
                report.attachment.delete(save=False)
            raise
        messages.success(request, "Report published for the requesting doctor's review.")
        return redirect("service_reports")
    messages.error(request, " ".join(str(e) for errors in form.errors.values() for e in errors))
    return redirect(f"/opd-assistant/services/{order.department.slug}/?order={order.pk}")


def accessible_reports(user):
    if is_admin(user):
        return DiagnosticReport.objects.all()
    if designation(user) in ("service_staff", "doctor", "opd_assistant"):
        qs = DiagnosticReport.objects.filter(order__in=scoped_orders(user), published_at__isnull=False)
        if designation(user) == "doctor":
            qs = qs | DiagnosticReport.objects.filter(order__isnull=True, patient__consultations__doctor=user)
        return qs.exclude(order__department__slug="counselling") if designation(user) == "opd_assistant" else qs.distinct()
    return DiagnosticReport.objects.none()


@login_required(login_url="login")
def service_reports(request):
    if not is_admin(request.user) and designation(request.user) not in ("service_staff", "doctor", "opd_assistant"):
        raise PermissionDenied
    reports = accessible_reports(request.user).select_related("patient", "order__consultation__doctor", "author", "reviewed_by")
    return render(request, "core/service_reports.html", {"page_title": "Service reports", "reports": Paginator(reports.order_by("-published_at", "-report_date"), 25).get_page(request.GET.get("page")), "can_review": is_admin(request.user) or designation(request.user) == "doctor"})


@login_required(login_url="login")
def report_download(request, report_id):
    report = get_object_or_404(accessible_reports(request.user), pk=report_id)
    if not report.attachment:
        from django.http import Http404
        raise Http404
    import mimetypes
    response = FileResponse(report.attachment.open("rb"), as_attachment=True, filename=f"report-{report.pk}{Path(report.attachment.name).suffix}", content_type=mimetypes.guess_type(report.attachment.name)[0] or "application/octet-stream")
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response


@login_required(login_url="login")
@require_POST
def report_review(request, report_id):
    if not is_admin(request.user) and designation(request.user) != "doctor":
        raise PermissionDenied
    report = get_object_or_404(accessible_reports(request.user), pk=report_id, published_at__isnull=False)
    with transaction.atomic():
        if DiagnosticReport.objects.filter(pk=report.pk, reviewed_at__isnull=True).update(reviewed_by=request.user, reviewed_at=timezone.now()):
            event(request.user, report.patient, "service_report_reviewed", f"Report reviewed: {report.title}", report_id=report.pk, order_id=report.order_id)
    return redirect("service_reports")


# ── POS and direct sales ─────────────────────────────────────────────────────

def _decimal(value, default="0"):
    try:
        return Decimal(str(value or default))
    except (InvalidOperation, TypeError, ValueError):
        raise ValidationError("Enter a valid amount.")


def _sale_cart(request):
    """Cart is stored in the session so checkout is one atomic request."""
    return request.session.get("pharmacy_sale_cart", [])


def _save_cart(request, cart):
    request.session["pharmacy_sale_cart"] = cart
    request.session.modified = True


def _sale_patient(request):
    """The patient selected for this cart survives add/update requests."""
    from patients.models import Patient
    patient_id = request.session.get("pharmacy_sale_patient")
    return Patient.objects.filter(pk=patient_id).first() if patient_id else None


def _set_sale_patient(request, patient_id):
    try:
        selected = int(patient_id) if patient_id else None
    except (TypeError, ValueError):
        selected = None
    request.session["pharmacy_sale_patient"] = selected
    request.session.modified = True


def _cart_preview(cart):
    """Totals shown in the POS basket, using the same tax fallback as checkout."""
    lines, subtotal, tax, discount = [], Decimal("0"), Decimal("0"), Decimal("0")
    fallback_rate = PharmacySettings.current().default_gst_rate or Decimal("0")
    for item in cart:
        batch = MedicineBatch.objects.select_related("medicine").filter(pk=item.get("batch_id")).first()
        if not batch:
            continue
        medicine = batch.medicine
        qty = int(item.get("quantity", 0))
        line_discount = Decimal(str(item.get("discount", "0")))
        value = batch.selling_price * qty - line_discount
        rate = medicine.gst_rate or fallback_rate
        line_tax = value * rate / Decimal("100")
        lines.append({"batch": batch, "medicine": medicine, "quantity": qty,
                      "unit_price": batch.selling_price, "tax": line_tax, "total": value + line_tax,
                      "discount": line_discount,
                      "available": batch_usable_quantity(batch), "sellable": batch.sellable})
        subtotal += batch.selling_price * qty
        discount += line_discount
        tax += line_tax
    return lines, subtotal, discount, tax


@login_required(login_url="login")
def pharmacy_pos(request):
    require_pharmacy(request.user)
    settings = PharmacySettings.current()
    cart = _sale_cart(request)
    query = request.GET.get("q", "").strip()
    queues, _ = dispensing_queues()
    queue_keys = ("ready", "prepare", "payment")
    queue_counts = {key: queues[key][1].count() for key in queue_keys}
    queue_labels = {"ready": "Ready", "prepare": "To prepare", "payment": "Awaiting payment"}
    queue_tabs = [(key, queue_labels[key], queue_counts[key]) for key in queue_keys]

    selected_consultation = None
    prescription_rows = []
    can_prepare = False
    consultation_issues = []
    consultation_id = request.GET.get("consultation", "").strip()
    if consultation_id.isdigit():
        selected_consultation = get_object_or_404(
            ConsultationSession.objects.select_related("patient", "doctor").filter(
                prescriptions__released_at__isnull=False).distinct(), pk=consultation_id)
        prescription_rows, can_prepare = prescription_preparation(selected_consultation)
        consultation_issues = selected_consultation.pharmacy_issues.select_related("bill")
    selected_issue = None
    issue_lines = []
    other_dues = PatientBill.objects.none()
    issue_id = request.GET.get("issue", "").strip()
    if issue_id.isdigit():
        selected_issue = get_object_or_404(
            PharmacyIssue.objects.select_related(
                "consultation__patient", "consultation__doctor", "bill", "dispensed_by"
            ).prefetch_related("lines__medicine", "lines__prescription", "lines__allocations__batch"),
            pk=issue_id,
        )
        issue_lines = selected_issue.lines.select_related("prescription", "medicine").prefetch_related("allocations__batch")
        other_dues = PatientBill.objects.filter(
            patient=selected_issue.consultation.patient, status="pending").exclude(pk=selected_issue.bill_id)

    active_queue_tab = request.GET.get("tab", "")
    if active_queue_tab not in queue_counts:
        if selected_issue:
            active_queue_tab = "payment" if selected_issue.bill.status == "pending" else "ready"
        else:
            active_queue_tab = "prepare" if selected_consultation else "ready"
    queue_records = Paginator(
        queues[active_queue_tab][1].order_by("wait_since", "pk"), 12
    ).get_page(request.GET.get("queue_page"))

    # The POS only sells over-the-counter medicines; prescriptions use the queue.
    medicines = Medicine.objects.filter(is_active=True, prescription_required=False)
    if query:
        medicines = medicines.filter(Q(name__icontains=query) | Q(generic_name__icontains=query)
                                     | Q(barcode__icontains=query) | Q(composition__icontains=query))
    results = []
    for medicine in medicines[:20]:
        batch = usable_batches(medicine).first()
        results.append({"medicine": medicine, "batch": batch, "available": batch_usable_quantity(batch) if batch else 0})
    rx_matches = 0
    if query:
        rx_matches = Medicine.objects.filter(is_active=True, prescription_required=True).filter(
            Q(name__icontains=query) | Q(generic_name__icontains=query)
            | Q(barcode__icontains=query) | Q(composition__icontains=query)).count()
    # "patient" present in the query (even empty) is an explicit selection change.
    if "patient" in request.GET:
        _set_sale_patient(request, request.GET.get("patient"))
    else:
        selected_patient = (selected_consultation.patient if selected_consultation else
                            selected_issue.consultation.patient if selected_issue else None)
        if selected_patient and not _sale_patient(request):
            _set_sale_patient(request, selected_patient.pk)
    patient = _sale_patient(request)
    from patients.models import Patient
    patient_q = request.GET.get("patient_q", "").strip()
    patient_results = []
    if patient_q and not patient:
        patient_results = Patient.objects.filter(
            Q(first_name__icontains=patient_q) | Q(last_name__icontains=patient_q) | Q(uhid__icontains=patient_q)
        )[:8]
    lines, subtotal, line_discount, tax = _cart_preview(cart)
    has_controlled = any(line["medicine"].is_controlled for line in lines)
    discount = _decimal(request.GET.get("discount", "0"))
    total = max(subtotal - line_discount - discount + tax, Decimal("0"))
    return render(request, "core/pharmacy_pos.html", {
        **pharmacy_context(), "page_title": "Pharmacy POS", "query": query, "results": results,
        "cart": lines, "subtotal": subtotal, "line_discount": line_discount, "bill_discount": discount,
        "tax": tax, "total": total, "settings": settings, "patient": patient,
        "has_controlled": has_controlled,
        "rx_matches": rx_matches, "max_discount_percent": settings.max_discount_percent,
        "patient_q": patient_q, "patient_results": patient_results, "request_id": uuid4(),
        "queue_tabs": queue_tabs, "queue_counts": queue_counts,
        "active_queue_tab": active_queue_tab, "queue_records": queue_records,
        "selected_consultation": selected_consultation, "consultation": selected_consultation,
        "prescription_rows": prescription_rows, "prescriptions": prescription_rows,
        "consultation_issues": consultation_issues, "can_prepare": can_prepare,
        "prepare_request_key": uuid4(),
        "selected_issue": selected_issue, "issue": selected_issue,
        "lines": issue_lines, "other_dues": other_dues, "can_dispense": True,
    })


@login_required(login_url="login")
@require_POST
def pharmacy_pos_add(request):
    require_pharmacy(request.user)
    query = request.POST.get("q", "").strip()

    def back():
        # Keep the operator's search so repeated items do not need re-typing.
        return redirect(reverse("pharmacy_pos") + (f"?{urlencode({'q': query})}" if query else ""))

    if not PharmacySettings.current().allow_direct_sales:
        messages.error(request, "Direct sales are disabled in Pharmacy settings.")
        return back()
    batch = get_object_or_404(MedicineBatch.objects.select_related("medicine"), pk=request.POST.get("batch"))
    try:
        quantity = int(request.POST.get("quantity", "1"))
    except ValueError:
        messages.error(request, "Enter a whole quantity.")
        return back()
    if not batch.sellable or quantity < 1 or quantity > batch_usable_quantity(batch):
        messages.error(request, f"{batch.medicine.name}: only {batch_usable_quantity(batch)} sellable units are available in this batch.")
        return back()
    if batch.medicine.prescription_required:
        messages.error(request, "Prescription medicines must go through the prescription queue.")
        return back()
    cart = _sale_cart(request)
    existing = next((item for item in cart if item["batch_id"] == batch.pk), None)
    if existing and existing["quantity"] + quantity > batch_usable_quantity(batch):
        messages.error(request, f"{batch.medicine.name}: the basket already holds {existing['quantity']} of the {batch_usable_quantity(batch)} sellable units in this batch.")
        return back()
    if existing:
        existing["quantity"] += quantity
    else:
        cart.append({"batch_id": batch.pk, "quantity": quantity, "discount": "0"})
    _save_cart(request, cart)
    return back()


@login_required(login_url="login")
@require_POST
def pharmacy_pos_update(request):
    require_pharmacy(request.user)
    cart = _sale_cart(request)
    action = request.POST.get("action")
    batch_id = request.POST.get("batch")
    if action == "remove":
        cart = [item for item in cart if str(item["batch_id"]) != str(batch_id)]
    elif action == "clear":
        cart = []
    else:
        for item in cart:
            if str(item["batch_id"]) == str(batch_id):
                try:
                    quantity = max(1, int(request.POST.get("quantity", item["quantity"])))
                    batch = MedicineBatch.objects.select_related("medicine").filter(pk=item["batch_id"]).first()
                    limit = batch_usable_quantity(batch) if batch else 0
                    if quantity > limit:
                        messages.error(request, f"{batch.medicine.name}: only {limit} sellable units are available in this batch." if batch else "This batch is no longer available.")
                    else:
                        item["quantity"] = quantity
                        item["discount"] = str(max(_decimal(request.POST.get("discount")), Decimal("0")))
                except (ValueError, ValidationError):
                    messages.error(request, "Enter valid cart values.")
    _save_cart(request, cart)
    return redirect("pharmacy_pos")


@login_required(login_url="login")
@require_POST
def pharmacy_pos_checkout(request):
    require_pharmacy(request.user)
    cart = _sale_cart(request)
    if not cart:
        messages.error(request, "The sale cart is empty.")
        return redirect("pharmacy_pos")
    patient = _sale_patient(request)
    # Build specs from the batch so the service always validates against live stock.
    line_specs = []
    for item in cart:
        batch = MedicineBatch.objects.filter(pk=item["batch_id"]).first()
        if batch:
            line_specs.append({"medicine_id": batch.medicine_id, "batch_id": batch.pk,
                               "quantity": item["quantity"], "discount": item.get("discount", "0")})
    try:
        sale = create_sale(
            user=request.user, patient=patient,
            walk_in_name=request.POST.get("walk_in_name", ""),
            line_specs=line_specs,
            discount=request.POST.get("discount", "0"),
            amount_received=request.POST.get("amount_received", "0"),
            payment_method=request.POST.get("payment_method", "cash"),
            payment_reference=request.POST.get("payment_reference", ""),
            controlled_authorisation=request.POST.get("controlled_authorisation", ""),
            request_key=request.POST.get("request_key") or uuid4(),
        )
    except ValidationError as exc:
        messages.error(request, error_message(exc))
        return redirect("pharmacy_pos")
    _save_cart(request, [])
    _set_sale_patient(request, None)
    messages.success(request, f"Sale {sale.reference} completed.")
    return redirect("pharmacy_sale_invoice", pk=sale.pk)


@login_required(login_url="login")
def pharmacy_sale_invoice(request, pk):
    require_pharmacy(request.user)
    sale = get_object_or_404(PharmacySale.objects.select_related("patient", "completed_by"), pk=pk)
    return render(request, "core/pharmacy_sale_invoice.html", {
        **pharmacy_context(), "page_title": f"Sale {sale.reference}", "sale": sale,
        "lines": sale.lines.select_related("medicine", "batch"),
    })


@login_required(login_url="login")
def pharmacy_sales_history(request):
    require_pharmacy(request.user)
    sales = PharmacySale.objects.select_related("patient", "completed_by")
    query = request.GET.get("q", "").strip()
    if query:
        sales = sales.filter(Q(reference__icontains=query) | Q(walk_in_name__icontains=query)
                             | Q(patient__first_name__icontains=query) | Q(patient__uhid__icontains=query))
    status = request.GET.get("status", "")
    if status in dict(PharmacySale.Status.choices):
        sales = sales.filter(status=status)
    else:
        status = ""
    payment = request.GET.get("payment", "")
    if payment in dict(PharmacySale.PaymentMethod.choices):
        sales = sales.filter(payment_method=payment)
    else:
        payment = ""
    return render(request, "core/pharmacy_sales_history.html", {
        **pharmacy_context(), "page_title": "Sales history", "query": query, "status": status,
        "payment": payment, "payment_methods": PharmacySale.PaymentMethod.choices,
        "sales": Paginator(sales, 25).get_page(request.GET.get("page")),
        # Tab counts describe the whole register, not the filtered tab.
        "counts": {
            "all": PharmacySale.objects.count(),
            "completed": PharmacySale.objects.filter(status=PharmacySale.Status.COMPLETED).count(),
            "cancelled": PharmacySale.objects.filter(status=PharmacySale.Status.CANCELLED).count(),
            "credit_balance": PharmacySale.objects.filter(
                status=PharmacySale.Status.COMPLETED, payment_method=PharmacySale.PaymentMethod.CREDIT
            ).aggregate(total=Coalesce(Sum(Greatest(F("total") - F("amount_received"), Value(Decimal("0")),
                                                   output_field=DecimalField(max_digits=12, decimal_places=2))),
                                       Decimal("0")))["total"],
        },
    })


@login_required(login_url="login")
@require_POST
def pharmacy_sale_cancel(request, pk):
    require_pharmacy(request.user)
    sale = get_object_or_404(PharmacySale, pk=pk)
    try:
        cancel_sale(user=request.user, sale=sale, reason=request.POST.get("reason", ""))
    except ValidationError as exc:
        messages.error(request, error_message(exc))
    else:
        messages.success(request, "Sale cancelled and stock restored.")
    return redirect("pharmacy_sale_invoice", pk=pk)


# ── Expiry, holds and transfers ───────────────────────────────────────────────

@login_required(login_url="login")
def pharmacy_expiry(request):
    require_pharmacy(request.user)
    today = timezone.localdate()
    settings = PharmacySettings.current()
    window = request.GET.get("window", "all")
    if window not in ("all", "expired", "critical", "watch", "30", "60", "90"):
        window = "all"
    stocked = MedicineBatch.objects.filter(on_hand__gt=0)
    # Window counts describe the whole pharmacy, not just the current search.
    counts = {
        "all": stocked.count(),
        "expired": stocked.filter(expiry_date__lt=today, status=MedicineBatch.Status.ACTIVE).count(),
        "critical": stocked.filter(
            status=MedicineBatch.Status.ACTIVE,
            expiry_date__range=(today, today + timedelta(days=settings.near_expiry_warn_days))).count(),
        "watch": stocked.filter(
            status=MedicineBatch.Status.ACTIVE,
            expiry_date__range=(today, today + timedelta(days=settings.expiry_warning_days))).count(),
    }
    for days in (30, 60, 90):
        counts[str(days)] = stocked.filter(expiry_date__range=(today, today + timedelta(days=days))).count()
    batches = stocked.select_related("medicine")
    if window == "expired":
        batches = batches.filter(expiry_date__lt=today)
    elif window == "critical":
        batches = batches.filter(expiry_date__range=(today, today + timedelta(days=settings.near_expiry_warn_days)))
    elif window == "watch":
        batches = batches.filter(expiry_date__range=(today, today + timedelta(days=settings.expiry_warning_days)))
    elif window in ("30", "60", "90"):
        batches = batches.filter(expiry_date__range=(today, today + timedelta(days=int(window))))
    query = request.GET.get("q", "").strip()
    if query:
        batches = batches.filter(Q(medicine__name__icontains=query) | Q(batch_number__icontains=query))
    return render(request, "core/pharmacy_expiry.html", {
        **pharmacy_context(), "page_title": "Expiry management",
        "batches": Paginator(batches.order_by("expiry_date"), 50).get_page(request.GET.get("page")),
        "counts": counts,
        "window": window, "query": query, "today": today, "settings": settings,
    })


@login_required(login_url="login")
def pharmacy_batch_detail(request, pk):
    require_pharmacy(request.user)
    batch = get_object_or_404(MedicineBatch.objects.select_related("medicine"), pk=pk)
    return render(request, "core/pharmacy_batch.html", {
        **pharmacy_context(), "page_title": f"Batch {batch.batch_number}", "batch": batch,
        "movements": batch.movements.select_related("actor", "sale", "issue", "purchase").all()[:50],
        "transfers": batch.transfers.select_related("requested_by", "completed_by").all()[:10],
        "available": batch_usable_quantity(batch),
    })


@login_required(login_url="login")
def pharmacy_batch_action(request):
    require_pharmacy(request.user)
    action = request.GET.get("action", "damage")
    forms = {"damage": StockHoldForm, "quarantine": StockHoldForm,
             "release": QuarantineReleaseForm, "transfer": StockTransferForm}
    form_class = forms.get(action, StockHoldForm)
    form = form_class(request.POST or None, initial={"request_key": uuid4()})
    if request.GET.get("batch"):
        form.fields["batch"].initial = request.GET.get("batch")
    if request.method == "POST" and form.is_valid():
        try:
            if action == "damage":
                record_damage(user=request.user, batch=form.cleaned_data["batch"],
                              quantity=form.cleaned_data["quantity"], reason=form.cleaned_data["reason"],
                              request_key=form.cleaned_data["request_key"])
            elif action == "quarantine":
                hold_quarantine(user=request.user, batch=form.cleaned_data["batch"],
                                quantity=form.cleaned_data["quantity"],
                                reason=form.cleaned_data["reason"],
                                request_key=form.cleaned_data["request_key"])
            elif action == "release":
                release_quarantine(user=request.user, batch=form.cleaned_data["batch"],
                                   quantity=form.cleaned_data["quantity"], reason=form.cleaned_data["reason"],
                                   request_key=form.cleaned_data["request_key"],
                                   target=form.cleaned_data["outcome"])
            else:
                transfer_stock(user=request.user, batch=form.cleaned_data["batch"],
                               quantity=form.cleaned_data["quantity"], to_location=form.cleaned_data["to_location"],
                               reason=form.cleaned_data["reason"], request_key=form.cleaned_data["request_key"])
        except ValidationError as exc:
            form.add_error(None, error_message(exc))
        else:
            messages.success(request, "Batch action recorded.")
            return redirect("pharmacy_stock_report")
    return render(request, "core/pharmacy_batch_action.html", {
        **pharmacy_context(), "page_title": {"damage": "Record damaged stock", "quarantine": "Quarantine stock",
                                              "release": "Release quarantine", "transfer": "Transfer stock"}.get(action, "Batch action"),
        "form": form, "action": action,
    })


# ── Suppliers and procurement ────────────────────────────────────────────────

@login_required(login_url="login")
def pharmacy_suppliers(request):
    require_admin(request.user)
    suppliers = PharmacySupplier.objects.all()
    query = request.GET.get("q", "").strip()
    if query:
        suppliers = suppliers.filter(Q(name__icontains=query) | Q(contact_person__icontains=query)
                                     | Q(phone__icontains=query) | Q(gstin__icontains=query))
    return render(request, "core/pharmacy_suppliers.html", {
        **pharmacy_context(), "page_title": "Suppliers", "query": query,
        "suppliers": Paginator(suppliers, 25).get_page(request.GET.get("page")),
        "total_outstanding": PharmacySupplier.objects.aggregate(total=Coalesce(Sum("outstanding_balance"), Decimal("0")))["total"],
    })


@login_required(login_url="login")
def pharmacy_supplier_edit(request, pk=None):
    require_admin(request.user)
    supplier = get_object_or_404(PharmacySupplier, pk=pk) if pk else None
    form = PharmacySupplierForm(request.POST or None, instance=supplier)
    if request.method == "POST" and form.is_valid():
        saved = form.save()
        pharmacy_audit(request.user, "supplier_saved", f"Supplier {saved.name} saved",
                       entity="PharmacySupplier", entity_id=saved.pk)
        messages.success(request, "Supplier saved.")
        return redirect("pharmacy_suppliers")
    return render(request, "core/pharmacy_supplier_form.html", {
        **pharmacy_context(), "page_title": "Edit supplier" if supplier else "Add supplier",
        "form": form, "supplier": supplier,
    })


@login_required(login_url="login")
def pharmacy_purchases(request):
    require_admin(request.user)
    purchases = PharmacyPurchase.objects.select_related("supplier", "created_by", "received_by")
    query = request.GET.get("q", "").strip()
    if query:
        purchases = purchases.filter(Q(reference__icontains=query) | Q(supplier__name__icontains=query)
                                     | Q(invoice_reference__icontains=query))
    status = request.GET.get("status", "")
    if status in dict(PharmacyPurchase.Status.choices):
        purchases = purchases.filter(status=status)
    else:
        status = ""
    supplier_id = request.GET.get("supplier", "").strip()
    supplier_filter = None
    if supplier_id.isdigit():
        supplier_filter = PharmacySupplier.objects.filter(pk=supplier_id).first()
        if supplier_filter:
            purchases = purchases.filter(supplier=supplier_filter)
    return render(request, "core/pharmacy_purchases.html", {
        **pharmacy_context(), "page_title": "Purchases", "query": query, "status": status,
        "supplier_filter": supplier_filter,
        "purchases": Paginator(purchases, 25).get_page(request.GET.get("page")),
        "total_count": PharmacyPurchase.objects.count(),
        "pending_count": PharmacyPurchase.objects.exclude(status__in=["received", "cancelled"]).count(),
        "received_count": PharmacyPurchase.objects.filter(status=PharmacyPurchase.Status.RECEIVED).count(),
        "total_outstanding": PharmacySupplier.objects.aggregate(
            total=Coalesce(Sum("outstanding_balance"), Decimal("0")))["total"],
    })


@login_required(login_url="login")
def pharmacy_purchase_new(request):
    require_admin(request.user)
    form = PurchaseReceiptForm(request.POST or None, initial={"request_key": uuid4(), "mode": PurchaseReceiptForm.MODE_RECEIVE})
    formset = PurchaseLineFormSet(request.POST or None, prefix="lines")
    if request.method == "POST" and form.is_valid() and formset.is_valid():
        lines = []
        for line in formset:
            data = line.cleaned_data
            if not data or data.get("DELETE"):
                continue
            lines.append({
                "medicine_id": data["medicine"].pk, "batch_number": data["batch_number"],
                "expiry_date": data["expiry_date"], "quantity": data["quantity"],
                "purchase_cost": data["purchase_cost"], "selling_price": data["selling_price"],
                "mrp": data["mrp"],
            })
        mode = form.cleaned_data["mode"]
        if not lines:
            form.add_error(None, "Add at least one purchase line." if mode == PurchaseReceiptForm.MODE_ORDER
                           else "Add at least one goods line before receiving stock.")
        else:
            try:
                if mode == PurchaseReceiptForm.MODE_ORDER:
                    purchase = record_purchase_order(
                        user=request.user, supplier=form.cleaned_data["supplier"],
                        reference=form.cleaned_data["reference"],
                        invoice_reference=form.cleaned_data["invoice_reference"],
                        lines=lines, request_key=form.cleaned_data["request_key"],
                    )
                else:
                    purchase = receive_purchase(
                        user=request.user, supplier=form.cleaned_data["supplier"],
                        reference=form.cleaned_data["reference"],
                        invoice_reference=form.cleaned_data["invoice_reference"],
                        lines=lines, request_key=form.cleaned_data["request_key"],
                    )
            except (ValidationError, OperationalError, IntegrityError) as exc:
                form.add_error(None, error_message(exc))
            else:
                if mode == PurchaseReceiptForm.MODE_ORDER:
                    messages.success(request, f"Purchase order {purchase.reference} recorded. Receive it from the purchase detail page when goods arrive.")
                else:
                    messages.success(request, f"Purchase {purchase.reference} received into stock.")
                return redirect("pharmacy_purchase_detail", pk=purchase.pk)
    return render(request, "core/pharmacy_purchase_form.html", {
        **pharmacy_context(), "page_title": "Procurement", "form": form, "formset": formset,
    })


@login_required(login_url="login")
def pharmacy_purchase_detail(request, pk):
    require_admin(request.user)
    purchase = get_object_or_404(PharmacyPurchase.objects.select_related("supplier"), pk=pk)
    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action == "receive":
                purchase, value, missing = receive_ordered_purchase(user=request.user, purchase=purchase)
                if missing:
                    messages.warning(request, f"Received ₹{value} of goods. {len(missing)} line{len(missing)|pluralize} still need batch details before they can be received.")
                else:
                    messages.success(request, f"Goods received and posted to stock · ₹{value}.")
            elif action == "line":
                line = get_object_or_404(purchase.lines.select_related("medicine"), pk=request.POST.get("line_id"))
                details = PurchaseLineDetailsForm(request.POST)
                if details.is_valid():
                    set_purchase_line_details(user=request.user, line=line,
                                              batch_number=details.cleaned_data["batch_number"],
                                              expiry_date=details.cleaned_data["expiry_date"])
                    messages.success(request, f"Batch details saved for {line.medicine.name}.")
                else:
                    messages.error(request, "Enter a batch number and a future expiry date.")
            else:
                messages.error(request, "Choose a valid purchase action.")
        except (ValidationError, OperationalError, IntegrityError) as exc:
            messages.error(request, error_message(exc))
        return redirect("pharmacy_purchase_detail", pk=purchase.pk)
    lines = list(purchase.lines.select_related("medicine"))
    for line in lines:
        line.remaining = max(line.quantity_ordered - line.quantity_received, 0)
    supplier_balance = purchase.supplier.outstanding_balance
    payable = min(purchase.outstanding, supplier_balance) if purchase.status in (
        PharmacyPurchase.Status.RECEIVED, PharmacyPurchase.Status.PARTIAL) else Decimal("0")
    return render(request, "core/pharmacy_purchase_detail.html", {
        **pharmacy_context(), "page_title": f"Purchase {purchase.reference}", "purchase": purchase,
        "lines": lines, "open_lines": [line for line in lines if line.remaining],
        "can_receive": purchase.status not in (PharmacyPurchase.Status.RECEIVED, PharmacyPurchase.Status.CANCELLED) and any(line.remaining for line in lines),
        "payable": payable, "line_form": PurchaseLineDetailsForm(),
    })


@login_required(login_url="login")
@require_POST
def pharmacy_purchase_pay(request, pk):
    require_admin(request.user)
    purchase = get_object_or_404(PharmacyPurchase, pk=pk)
    try:
        amount = _decimal(request.POST.get("amount"))
        if amount <= 0:
            raise ValidationError("Payment must be a positive amount.")
        # Guarded updates inside one atomic block: an overpayment on either the
        # purchase or the supplier ledger rolls the whole payment back.
        with transaction.atomic():
            paid = PharmacyPurchase.objects.filter(
                pk=pk, paid__lte=F("total") - amount).update(paid=F("paid") + amount)
            if not paid:
                raise ValidationError("This payment exceeds the outstanding balance. Refresh the purchase.")
            supplier_debited = PharmacySupplier.objects.filter(
                pk=purchase.supplier_id, outstanding_balance__gte=amount,
            ).update(outstanding_balance=F("outstanding_balance") - amount)
            if not supplier_debited:
                raise ValidationError("The supplier balance changed. Refresh and retry the payment.")
            pharmacy_audit(request.user, "supplier_payment", f"₹{amount} paid against {purchase.reference}",
                           entity="PharmacyPurchase", entity_id=purchase.pk, after={"amount": str(amount)})
    except ValidationError as exc:
        messages.error(request, error_message(exc))
    else:
        messages.success(request, "Supplier payment recorded.")
    return redirect("pharmacy_purchase_detail", pk=pk)


# ── Patient returns ──────────────────────────────────────────────────────────

@login_required(login_url="login")
def pharmacy_returns(request):
    require_pharmacy(request.user)
    kind = request.GET.get("kind", "")
    sale_id = request.GET.get("sale", "").strip()
    selected_sale = None
    if sale_id:
        selected_sale = PharmacySale.objects.select_related("patient").filter(
            Q(pk=sale_id) if sale_id.isdigit() else Q(reference__iexact=sale_id)
        ).first()
    batch_id = request.GET.get("batch", "").strip()
    selected_batch = None
    if batch_id.isdigit():
        selected_batch = MedicineBatch.objects.select_related("medicine").filter(pk=batch_id).first()
    returns = PharmacyReturn.objects.select_related("sale", "batch__medicine", "created_by", "reviewed_by")
    if kind in (PharmacyReturn.Kind.PATIENT, PharmacyReturn.Kind.SUPPLIER):
        returns = returns.filter(kind=kind)
    if selected_sale is not None:
        returns = returns.filter(sale=selected_sale)
    if selected_batch is not None:
        returns = returns.filter(batch=selected_batch)
    return_id = request.GET.get("return", "").strip()
    if return_id.isdigit():
        returns = returns.filter(pk=return_id)
    counts = {
        "all": PharmacyReturn.objects.count(),
        "patient": PharmacyReturn.objects.filter(kind=PharmacyReturn.Kind.PATIENT).count(),
        "supplier": PharmacyReturn.objects.filter(kind=PharmacyReturn.Kind.SUPPLIER).count(),
        "refund_due": PharmacyReturn.objects.filter(
            kind=PharmacyReturn.Kind.PATIENT, status=PharmacyReturn.Status.QUARANTINED).count(),
    }
    patient_initial = {"request_key": uuid4()}
    if selected_sale is not None:
        first_line = selected_sale.lines.order_by("pk").first()
        if first_line:
            patient_initial["sale_line"] = first_line.pk
    supplier_initial = {"request_key": uuid4()}
    if selected_batch is not None:
        supplier_initial["batch"] = selected_batch.pk
    return render(request, "core/pharmacy_returns.html", {
        **pharmacy_context(), "page_title": "Returns register", "kind": kind, "counts": counts,
        "returns": Paginator(returns, 25).get_page(request.GET.get("page")),
        "selected_sale": selected_sale, "selected_batch": selected_batch,
        "form": PatientReturnForm(request.POST or None, sale=selected_sale, initial=patient_initial),
        "supplier_form": SupplierReturnForm(request.POST or None, initial=supplier_initial),
        "request_id": uuid4(),
    })


@login_required(login_url="login")
@require_POST
def pharmacy_return_new(request):
    require_pharmacy(request.user)
    form = PatientReturnForm(request.POST)
    if form.is_valid():
        try:
            create_patient_return(user=request.user, line=form.cleaned_data["sale_line"],
                                  quantity=form.cleaned_data["quantity"], reason=form.cleaned_data["reason"],
                                  request_key=form.cleaned_data["request_key"])
        except ValidationError as exc:
            messages.error(request, error_message(exc))
        else:
            messages.success(request, "Return recorded and moved to quarantine.")
    else:
        messages.error(request, "Choose a sale line, quantity and reason for the return.")
    return redirect("pharmacy_returns")


@login_required(login_url="login")
@require_POST
def pharmacy_supplier_return(request):
    require_pharmacy(request.user)
    form = SupplierReturnForm(request.POST)
    if form.is_valid():
        try:
            ret = return_to_supplier(user=request.user, batch=form.cleaned_data["batch"],
                                     quantity=form.cleaned_data["quantity"],
                                     reason=form.cleaned_data["reason"],
                                     request_key=form.cleaned_data["request_key"])
        except ValidationError as exc:
            messages.error(request, error_message(exc))
        else:
            messages.success(request, f"Supplier return recorded · ₹{ret.refund} credit expected.")
    else:
        messages.error(request, "Choose a batch, quantity and reason for the supplier return.")
    return redirect("pharmacy_returns")


@login_required(login_url="login")
@require_POST
def pharmacy_refund_settle(request):
    require_pharmacy(request.user)
    form = PatientRefundForm(request.POST)
    if form.is_valid():
        ret = get_object_or_404(PharmacyReturn, pk=form.cleaned_data["return_id"])
        try:
            settle_patient_return(user=request.user, ret=ret,
                                  request_key=form.cleaned_data["request_key"])
        except ValidationError as exc:
            messages.error(request, error_message(exc))
        else:
            messages.success(request, f"Refund of ₹{ret.refund} settled.")
    else:
        messages.error(request, "Choose a return to settle.")
    return redirect("pharmacy_returns")


# ── Reports, settings and audit ──────────────────────────────────────────────

@login_required(login_url="login")
def pharmacy_reports(request):
    require_admin(request.user)
    today = timezone.localdate()
    date_from = request.GET.get("from") or str(today - timedelta(days=30))
    date_to = request.GET.get("to") or str(today)
    from django.utils.dateparse import parse_date
    start, end = parse_date(date_from), parse_date(date_to)
    if not start or not end or start > end:
        start, end, date_from, date_to = today - timedelta(days=30), today, str(today - timedelta(days=30)), str(today)
    sales = PharmacySale.objects.filter(status=PharmacySale.Status.COMPLETED, completed_at__date__range=(start, end))
    issues = PharmacyIssue.objects.filter(status=PharmacyIssue.Status.DISPENSED, dispensed_at__date__range=(start, end))
    sale_lines = PharmacySaleLine.objects.filter(sale__in=sales).select_related("medicine")
    medicine_sales = sale_lines.values("medicine__name").annotate(units=Sum("quantity"), revenue=Sum(F("quantity") * F("unit_price"))).order_by("-revenue")[:20]
    category_sales = sale_lines.values("medicine__category").annotate(units=Sum("quantity"), revenue=Sum(F("quantity") * F("unit_price"))).order_by("-revenue")
    payment = sales.values("payment_method").annotate(count=Count("pk"), amount=Sum("total"))
    if request.GET.get("export") == "csv":
        import csv
        from django.http import HttpResponse
        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = 'attachment; filename="pharmacy-sales-report.csv"'
        writer = csv.writer(response)
        writer.writerow(["Reference", "Date", "Patient / walk-in", "Payment", "Subtotal", "Discount", "Tax", "Total"])
        for sale in sales.order_by("-completed_at"):
            writer.writerow([sale.reference, sale.completed_at, sale.patient.full_name if sale.patient else sale.walk_in_name,
                             sale.payment_method, sale.subtotal, sale.discount, sale.tax, sale.total])
        return response
    return render(request, "core/pharmacy_reports.html", {
        **pharmacy_context(), "page_title": "Reports and analytics", "from": date_from, "to": date_to,
        "sale_totals": sales.aggregate(gross=Coalesce(Sum("total"), Decimal("0")),
                                       discount=Coalesce(Sum("discount"), Decimal("0")),
                                       tax=Coalesce(Sum("tax"), Decimal("0"))),
        "issue_totals": issues.aggregate(
            value=Coalesce(Sum(F("bill__amount") - F("bill__discount")), Decimal("0")),
            billed=Coalesce(Sum("bill__amount"), Decimal("0")),
            count=Count("pk")),
        "medicine_sales": medicine_sales, "category_sales": category_sales, "payment": payment,
        "sales": sales.order_by("-completed_at")[:25],
        "inventory_value": MedicineBatch.objects.filter(on_hand__gt=0, status="active").aggregate(
            value=Coalesce(Sum(F("on_hand") * F("purchase_cost")), Decimal("0"))),
        "pending_prescriptions": PrescriptionItem.objects.filter(released_at__isnull=False,
            dispense_status__in=["ordered", "partial", "unavailable"]).count(),
        "returns": PharmacyReturn.objects.values("kind").annotate(
            count=Count("pk"), refund=Coalesce(Sum("refund"), Decimal("0"))).order_by("kind"),
        "supplier_balance": PharmacySupplier.objects.aggregate(
            total=Coalesce(Sum("outstanding_balance"), Decimal("0"))),
        "purchase_totals": PharmacyPurchase.objects.filter(
            ordered_at__date__range=(start, end)).aggregate(
            total=Coalesce(Sum("total"), Decimal("0")), paid=Coalesce(Sum("paid"), Decimal("0"))),
    })


@login_required(login_url="login")
def pharmacy_settings_view(request):
    require_admin(request.user)
    settings = PharmacySettings.current()
    form = PharmacySettingsForm(request.POST or None, instance=settings)
    if request.method == "POST" and form.is_valid():
        saved = form.save(commit=False)
        saved.updated_by = request.user
        saved.save()
        pharmacy_audit(request.user, "settings_updated", "Pharmacy settings updated", entity="PharmacySettings", entity_id=saved.pk)
        messages.success(request, "Pharmacy settings saved.")
        return redirect("pharmacy_settings")
    return render(request, "core/pharmacy_settings.html", {
        **pharmacy_context(), "page_title": "Pharmacy settings", "form": form, "settings": settings,
    })


@login_required(login_url="login")
def pharmacy_audit_history(request):
    require_admin(request.user)
    audits = PharmacyAudit.objects.select_related("actor")
    query = request.GET.get("q", "").strip()
    if query:
        audits = audits.filter(Q(summary__icontains=query) | Q(action__icontains=query)
                               | Q(entity__icontains=query))
    action = request.GET.get("action", "")
    if action:
        audits = audits.filter(action=action)
    return render(request, "core/pharmacy_audit.html", {
        **pharmacy_context(), "page_title": "Pharmacy audit history", "query": query, "action": action,
        "audits": Paginator(audits, 50).get_page(request.GET.get("page")),
        "actions": PharmacyAudit.objects.values_list("action", flat=True).distinct().order_by("action"),
    })
