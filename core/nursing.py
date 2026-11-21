"""Nursing-task assignment and the links into orders each designation already runs."""

from django.contrib.auth import get_user_model
from django.db.models import Count, Q
from django.utils import timezone

# Name fragments from the doctor and triage procedure chips. More specific
# needles come first so "wound dressing" is not classified as a bare dressing
# title when a clearer label exists.
PROCEDURE_RULES = (
    ("nebulisation", "medication", "Give nebulisation"),
    ("nebuliser", "medication", "Nebuliser refill"),
    ("iv fluid", "medication", "Start IV fluids"),
    ("wound dressing", "dressing", "Wound dressing"),
    ("dressing", "dressing", "Wound dressing"),
    ("injection", "injection", "Injection as prescribed"),
)

OPEN = ("pending", "in_progress")


def _department_slug(order):
    if getattr(order, "department_id", None) and order.department_id:
        department = getattr(order, "department", None)
        if department is not None:
            return department.slug
    definition = getattr(order, "service_definition", None)
    if definition is not None and getattr(definition, "department_id", None):
        return definition.department.slug
    return ""


def classify_clinical_order(order):
    """Return a task spec, or None when this order is not nursing work.

    Laboratory orders become a sample-collection task. Imaging, physio and
    counselling stay on their own queues. Procedure-room work and the named
    nursing chips become a doctor-ordered task.
    """
    name = (order.name or "").strip()
    key = name.casefold()
    slug = _department_slug(order)
    for needle, kind, title in PROCEDURE_RULES:
        if needle in key:
            return {"source": "doctor_order", "kind": kind, "title": title}
    if slug == "procedure-room":
        return {"source": "doctor_order", "kind": "other", "title": name or "Procedure room task"}
    if order.order_type == "laboratory" or slug == "laboratory":
        return {
            "source": "lab_collect",
            "kind": "sample",
            "title": f"Collect sample — {name or 'laboratory'}"[:255],
        }
    return None


def task_assignees():
    """People a task may be handed to: OPD assistants and procedure-room staff."""
    User = get_user_model()
    return (
        User.objects.filter(is_active=True)
        .filter(
            Q(profile__designation__in=["opd_assistant", "nurse"])
            | Q(
                profile__designation="service_staff",
                profile__service_departments__slug="procedure-room",
            )
        )
        .distinct()
        .select_related("profile")
        .order_by("first_name", "last_name", "username")
    )


def resolve_assignee(raw, actor):
    """Map a form value to a user. Blank is the shared pool. 'me' is the actor."""
    if raw in (None, "", "pool"):
        return None
    if raw in ("me", "on"):
        return actor
    if not str(raw).isdigit():
        return None
    person = task_assignees().filter(pk=int(raw)).first()
    if person:
        return person
    if actor is not None and int(raw) == actor.pk:
        return actor
    return None


def open_task_counts(patient_ids):
    from patients.models import NurseTask

    if not patient_ids:
        return {}
    return {
        row["patient_id"]: row["n"]
        for row in NurseTask.objects.filter(patient_id__in=list(patient_ids), status__in=OPEN)
        .values("patient_id")
        .annotate(n=Count("id"))
    }


def open_tasks_for(patient, limit=8):
    from patients.models import NurseTask

    return list(
        NurseTask.objects.filter(patient=patient, status__in=OPEN)
        .select_related("assigned_to", "order")
        .order_by("-priority", "due_at", "created_at")[:limit]
    )


def ensure_task_for_order(order, actor):
    """Create or close the nursing task that belongs to this clinical order."""
    from patients.models import NurseTask

    spec = classify_clinical_order(order)
    task = NurseTask.objects.filter(order=order).first()
    if spec is None:
        return task
    if order.status == "Cancelled":
        if task and task.status in OPEN:
            task.status = NurseTask.Status.SKIPPED
            task.outcome = task.outcome or "Order cancelled"
            task.save(update_fields=["status", "outcome", "updated_at"])
        return task
    if task:
        return task
    if order.status in ("Completed", "Cancelled"):
        return None
    patient = order.consultation.patient
    return NurseTask.objects.create(
        patient=patient,
        consultation=order.consultation,
        order=order,
        kind=spec["kind"],
        source=spec["source"],
        title=spec["title"][:255],
        notes=f"From order: {order.name}"[:2000],
        priority=(
            NurseTask.Priority.URGENT if order.priority == "urgent" else NurseTask.Priority.ROUTINE
        ),
        created_by=actor if getattr(actor, "is_authenticated", False) else None,
        assigned_to=None,
    )


def sync_task_from_order(order, actor):
    """Move the linked task when the service queue advances the order."""
    from patients.models import NurseTask

    task = NurseTask.objects.filter(order=order).exclude(status=NurseTask.Status.SKIPPED).first()
    if task is None:
        task = ensure_task_for_order(order, actor)
    if task is None or task.status == NurseTask.Status.DONE:
        return task
    details = order.operational_details or {}
    collected = details.get("specimen_status") == "collected"
    if order.status == "Cancelled":
        task.status = NurseTask.Status.SKIPPED
        task.outcome = task.outcome or "Order cancelled"
        task.save(update_fields=["status", "outcome", "updated_at"])
        return task
    if order.status == "Completed" or (task.source == NurseTask.Source.LAB and collected):
        task.status = NurseTask.Status.DONE
        task.completed_by = actor if getattr(actor, "is_authenticated", False) else task.completed_by
        task.completed_at = timezone.now()
        task.outcome = task.outcome or order.result_summary or (
            "Sample collected" if collected else "Completed with the service order"
        )
        if not task.assigned_to_id and getattr(actor, "is_authenticated", False):
            task.assigned_to = actor
        task.save()
        return task
    if order.status == "In Progress" and task.status == NurseTask.Status.PENDING:
        task.status = NurseTask.Status.IN_PROGRESS
        if not task.assigned_to_id and getattr(actor, "is_authenticated", False):
            task.assigned_to = actor
        task.save(update_fields=["status", "assigned_to", "updated_at"])
    return task


def sync_order_from_task(task, actor):
    """Completing a nursing task closes procedure work, or marks a sample collected.

    A lab order stays open for the result. Cancelling a task does not cancel a
    billed order.
    """
    order = task.order
    if order is None or task.status != "done" or order.status in ("Completed", "Cancelled"):
        return order
    now = timezone.now()
    details = dict(order.operational_details or {})
    if task.kind == "sample" or task.source == "lab_collect":
        details["specimen_status"] = "collected"
        details.setdefault("collected_at", now.isoformat())
        order.operational_details = details
        if order.status == "Ordered":
            order.status = "In Progress"
            order.started_at = order.started_at or now
        order.updated_by = actor if getattr(actor, "is_authenticated", False) else order.updated_by
        order.save()
        return order
    order.status = "Completed"
    order.started_at = order.started_at or now
    order.completed_at = order.completed_at or now
    if getattr(actor, "is_authenticated", False):
        order.completed_by = actor
        order.updated_by = actor
    if task.outcome and not order.result_summary:
        order.result_summary = task.outcome[:255]
    order.save()
    return order
