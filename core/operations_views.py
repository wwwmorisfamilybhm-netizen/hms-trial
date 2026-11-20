"""Role-specific pharmacy and service workspaces."""
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4
from pathlib import Path
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db import OperationalError, IntegrityError, transaction
from django.db.models import F, Q, Sum, Min
from django.db.models.functions import Coalesce
from django.urls import reverse
from django.http import FileResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST
from patients.models import (
    Medicine, MedicineBatch, StockMovement, PharmacyIssue, PrescriptionItem,
    ConsultationSession, PatientBill, ClinicalOrder, ServiceDepartment, ServiceDefinition,
    DiagnosticReport, OPDAuditEvent,
)
from .operations import (
    require_pharmacy, is_admin, designation, scoped_orders, receive_stock, adjust_stock,
    reserve_issue, cancel_issue, handover_issue, reallocate_issue, cancel_prescription,
    set_bill_status, event,
)
from .operations_forms import MedicineForm, ReceiptForm, AdjustmentForm, ServiceDefinitionForm, ReportForm


def error_message(exc):
    return " ".join(exc.messages) if isinstance(exc, ValidationError) else "Another update is in progress. Refresh and try again; this submission can be retried safely."


def pharmacy_context():
    return {"page_title": "Pharmacy", "today": timezone.localdate()}


def stocked_medicines(today):
    return Medicine.objects.filter(is_active=True).annotate(
        usable=Sum(F("batches__on_hand") - F("batches__reserved"),
                   filter=Q(batches__expiry_date__gte=today), default=0)
    )


def expiring_batches(today):
    return MedicineBatch.objects.filter(on_hand__gt=0, expiry_date__range=(today, today + timedelta(days=90)))


def searched_visits(query):
    visits = ConsultationSession.objects.select_related("patient", "doctor")
    for term in query.split():
        visits = visits.filter(Q(patient__first_name__icontains=term)
                              | Q(patient__last_name__icontains=term) | Q(patient__uhid__icontains=term)
                              | Q(visit_token__icontains=term))
    return visits


@login_required(login_url="login")
def pharmacy_dashboard(request):
    require_pharmacy(request.user)
    return dispensing_desk(request, default_tab="ready")


def dispensing_desk(request, default_tab):
    today = timezone.localdate()
    query = request.GET.get("q", "").strip()
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
    tab = request.GET.get("tab", default_tab)
    if tab not in queues:
        tab = default_tab
    label, records = queues[tab]
    stale_only = tab == "payment" and request.GET.get("stale") == "1"
    if stale_only:
        records = records.filter(pk__in=stale)
    page = Paginator(records.order_by("wait_since", "pk"), 20).get_page(request.GET.get("page"))
    state = request.GET.copy()
    state["tab"] = tab
    state["page"] = page.number
    return render(request, "core/pharmacy_dashboard.html", {
        **pharmacy_context(), "page_title": "Dispensing desk", "query": query, "tab": tab, "label": label,
        "tabs": [(key, title, rows.count()) for key, (title, rows) in queues.items()],
        "records": page, "queue_state": state.urlencode(), "stale_only": stale_only,
        "low_count": stocked_medicines(today).filter(usable__lte=F("reorder_level")).count(),
        "expiry_count": expiring_batches(today).count(), "stale_count": stale.count(),
        "has_stock": StockMovement.objects.exists(),
    })


@login_required(login_url="login")
def medicine_catalogue(request, medicine_id=None):
    require_pharmacy(request.user)
    medicine = get_object_or_404(Medicine, pk=medicine_id) if medicine_id else None
    form = MedicineForm(request.POST or None, instance=medicine)
    if request.method == "POST" and form.is_valid():
        original = Medicine.objects.filter(pk=medicine_id).first() if medicine_id else None
        if original and original.batches.exists() and any(form.cleaned_data[f] != getattr(original, f) for f in ("name", "strength", "form", "unit", "pack_size")):
            form.add_error(None, "Unit and pack size cannot change after receiving stock; medicine identity is also fixed. Create a separate catalogue entry.")
        else:
            saved = form.save()
            messages.success(request, "Medicine catalogue saved. You can now add a product photo.")
            return redirect("medicine_photo", medicine_id=saved.pk)
    medicines = Medicine.objects.all()
    query = request.GET.get("q", "").strip()
    if query:
        medicines = medicines.filter(Q(name__icontains=query) | Q(generic_name__icontains=query))
    return render(request, "core/pharmacy_catalogue.html", {**pharmacy_context(), "page_title": "Medicine catalogue", "form": form, "medicines": Paginator(medicines, 25).get_page(request.GET.get("page")), "query": query, "editing": medicine})


@login_required(login_url="login")
def pharmacy_receive(request):
    require_pharmacy(request.user)
    form = ReceiptForm(request.POST or None, initial={"request_key": uuid4()})
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
    require_pharmacy(request.user)
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
    elif stock_filter == "expiring":
        batches = batches.filter(pk__in=expiring_batches(today))
    elif stock_filter == "expired":
        batches = batches.filter(expiry_date__lt=today)
    unstocked = low.filter(batches__isnull=True, name__icontains=query)
    rows = list(batches)
    for batch in rows:
        batch.expired = batch.expiry_date < today
        batch.expiring = today <= batch.expiry_date <= today + timedelta(days=90)
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
    return render(request, "core/pharmacy_stock_report.html", {**pharmacy_context(), "page_title": "Batch inventory", "batches": rows, "query": query, "low_medicines": low.filter(name__icontains=query), "unstocked": unstocked if stock_filter == "low" else [], "stock_filter": stock_filter, "movements": Paginator(StockMovement.objects.select_related("batch__medicine", "actor"), 50).get_page(request.GET.get("page"))})


@login_required(login_url="login")
def pharmacy_queue(request, consultation_id=None):
    require_pharmacy(request.user)
    if consultation_id is None:
        prescription = request.GET.get("prescription", "")
        if prescription.isdigit():
            rx = get_object_or_404(PrescriptionItem, pk=prescription, released_at__isnull=False)
            return redirect("pharmacy_consultation", consultation_id=rx.consultation_id)
        return dispensing_desk(request, default_tab="all" if request.GET.get("status") == "all" else "prepare")
    selected = get_object_or_404(
        ConsultationSession.objects.select_related("patient", "doctor").filter(prescriptions__released_at__isnull=False).distinct(),
        pk=consultation_id,
    )
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
        rx.selected_match = request.POST.get(f"medicine_{rx.pk}", "")
        rx.entered_quantity = request.POST.get(f"quantity_{rx.pk}", "0")
        stock = stock_by_id.get(rx.medicine_id, 0)
        if not rx.medicine_id:
            stock = next((m.usable for m in rx.matches if str(m.pk) == rx.selected_match), None)
        rx.available_stock = stock
        rx.prepare_limit = min(rx.remaining_quantity, 9999)
        if stock is not None:
            rx.prepare_limit = min(rx.prepare_limit, stock)
    if request.method == "POST":
        entries = [{"prescription_id": rx.pk, "medicine_id": request.POST.get(f"medicine_{rx.pk}"), "quantity": request.POST.get(f"quantity_{rx.pk}")} for rx in rows if request.POST.get(f"quantity_{rx.pk}", "").strip() not in ("", "0")]
        try:
            issue = reserve_issue(user=request.user, consultation=selected, entries=entries, request_key=request.POST.get("request_key"))
        except (ValidationError, OperationalError, IntegrityError) as exc:
            messages.error(request, error_message(exc))
        else:
            return redirect(reverse("pharmacy_issue", args=[issue.pk], query=request.GET))
    can_prepare = selected.status != "cancelled" and any(
        rx.remaining_quantity and (rx.prepare_limit if rx.medicine_id else any(m.usable for m in rx.matches))
        and rx.dispense_status not in ("cancelled", "dispensed")
        for rx in rows
    )
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
            if not handler:
                raise ValidationError("Choose a valid issue action.")
            handler(user=request.user, issue=issue)
        except (ValidationError, OperationalError, IntegrityError) as exc:
            messages.error(request, error_message(exc))
        else:
            messages.success(request, {"handover": "Medicines handed over and stock updated.", "cancel": "Unpaid issue cancelled; reservations released.", "reallocate": "Valid batches allocated; billed prices preserved."}[action])
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
            from django.http import JsonResponse
            return JsonResponse({"ok": False, "error": error_message(exc)}, status=400)
        messages.error(request, error_message(exc))
    else:
        if request.headers.get("Accept") == "application/json":
            from django.http import JsonResponse
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
    queues = [{"slug": d.slug, "title": d.name, "open_count": orders.filter(department=d).exclude(status__in=["Completed", "Cancelled"]).count()} for d in departments]
    return render(request, "core/service_operations.html", {"page_title": "Service operations", "queues": queues, "orders": orders.exclude(status__in=["Completed", "Cancelled"]).order_by("-priority", "created_at")[:20], "completed_count": orders.filter(completed_at__date=timezone.localdate()).count(), "is_admin": is_admin(request.user), "unassigned_count": ClinicalOrder.objects.filter(department__isnull=True).exclude(status="Cancelled").count() if is_admin(request.user) else 0})


@login_required(login_url="login")
def service_queue(request, service):
    if service == "pharmacy":
        return pharmacy_queue(request)
    department = get_object_or_404(ServiceDepartment, slug=service)
    if not is_admin(request.user) and (designation(request.user) not in ("doctor", "opd_assistant", "service_staff") or (designation(request.user) == "service_staff" and not request.user.profile.service_departments.filter(pk=department.pk).exists())):
        raise PermissionDenied
    orders = scoped_orders(request.user).filter(department=department).select_related("consultation__patient", "consultation__doctor", "assigned_staff")
    if not is_admin(request.user) and designation(request.user) == "service_staff":
        orders = orders.filter(
            Q(bill__isnull=True) | Q(bill__status=PatientBill.Status.PAID)
        )
    query = request.GET.get("q", "").strip()
    if query:
        orders = orders.filter(Q(name__icontains=query) | Q(consultation__patient__first_name__icontains=query) | Q(consultation__patient__uhid__icontains=query) | Q(consultation__visit_token__icontains=query))
    status = request.GET.get("status", "")
    if status:
        orders = orders.filter(status=status)
    selected = get_object_or_404(orders, pk=request.GET["order"]) if request.GET.get("order", "").isdigit() else None
    report_form = ReportForm(initial={"title": selected.name, "summary": selected.result_summary}) if selected else None
    staff = get_user_model().objects.filter(is_active=True, profile__designation="service_staff", profile__service_departments=department)
    return render(request, "core/service_queue.html", {"page_title": department.name, "department": department, "orders": Paginator(orders.order_by("-priority", "created_at"), 25).get_page(request.GET.get("page")), "selected": selected, "selected_reports": accessible_reports(request.user).filter(order=selected) if selected else [], "query": query, "status_filter": status, "statuses": ["Ordered", "In Progress", "Review Required", "Completed", "Cancelled"], "staff": staff, "report_form": report_form, "can_update": is_admin(request.user) or designation(request.user) in ("opd_assistant", "service_staff"), "can_publish": is_admin(request.user) or designation(request.user) == "service_staff"})


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
            for field in ("body_part", "preparation_status", "preparation_note", "specimen_status", "acquisition_status", "session_datetime", "room", "post_instructions"):
                if field in request.POST:
                    details[field] = request.POST[field].strip()[:255]
            if order.department.slug == "procedure-room":
                details["consent_confirmed"] = request.POST.get("consent_confirmed") == "on"
                details["preparation_complete"] = request.POST.get("preparation_complete") == "on"
            order.operational_details = details
            now = timezone.now()
            if new_status in ("In Progress", "Completed", "Review Required") and not order.started_at:
                order.started_at = now
            if new_status == "Completed" and not order.completed_at:
                order.completed_at = now
                order.completed_by = request.user
            order.updated_by = request.user
            order.save()
            OPDAuditEvent.objects.create(patient=order.consultation.patient, order=order, actor=request.user, action="service_order_updated", summary=f"{order.name}: {previous} to {new_status}"[:255], details={"previous_status": previous, "status": new_status, **details})
    except (ValidationError, OperationalError) as exc:
        messages.error(request, error_message(exc))
    else:
        messages.success(request, "Service order updated.")
    return redirect("opd_service_queue", service=order.department.slug)


@login_required(login_url="login")
def service_completed_today(request):
    if not is_admin(request.user) and designation(request.user) != "service_staff":
        raise PermissionDenied
    orders = scoped_orders(request.user).filter(completed_at__date=timezone.localdate()).select_related("consultation__patient", "consultation__doctor", "updated_by", "department")
    return render(request, "core/service_completed_today.html", {"page_title": "Completed today", "orders": orders.order_by("-completed_at"), "today": timezone.localdate()})


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
