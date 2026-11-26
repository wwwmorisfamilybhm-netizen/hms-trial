"""Pharmacy file review and product-photo interfaces."""
import copy
import csv
import io
from pathlib import Path
from uuid import uuid4
from urllib3.exceptions import HTTPError

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core import signing
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.core.paginator import Paginator
from django.db import IntegrityError, OperationalError
from django.db.models import F
from django.http import FileResponse, HttpResponse
import mimetypes
from urllib.parse import urlencode
from django.conf import settings
from patients.storage import private_report_storage
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from patients.models import Medicine, MedicineImport
from .operations import require_pharmacy, is_admin
from .pharmacy_imports import (
    ImportUploadForm, ImportRowForm, ImportReviewError,
    extract_rows, mapped_rows, commit_import, normalized, prepare_invoice_rows,
)
from .pharmacy_invoice_parser import INVOICE_COLUMNS, InvoiceNotRecognised, parse_invoice_pdf
from .pharmacy_photos import (
    photo_candidates, public_get, validated_photo, MAX_PHOTO_BYTES,
    medicine_image_query, linked_photo, stage_photo, read_photo_preview,
)


def draft_for(request, pk):
    require_pharmacy(request.user)
    drafts = MedicineImport.objects.all()
    if not is_admin(request.user):
        drafts = drafts.filter(owner=request.user)
    return get_object_or_404(drafts, pk=pk)


@login_required(login_url="login")
def import_upload(request):
    require_pharmacy(request.user)
    requested_kind = request.GET.get("kind", "catalogue")
    if requested_kind not in ("catalogue", "stock"):
        requested_kind = "catalogue"
    stock_mode = requested_kind == "stock"
    form = ImportUploadForm(request.POST or None, request.FILES or None, initial={"kind": requested_kind})
    if request.method == "POST" and form.is_valid():
        source = form.cleaned_data["source"]
        try:
            document = {}
            if Path(source.name).suffix.lower() == ".pdf":
                try:
                    parsed = parse_invoice_pdf(source)
                except InvoiceNotRecognised:
                    parsed = None
                if parsed:
                    rows, match_warnings = prepare_invoice_rows(parsed["items"])
                    document = parsed["document"]
                    document["warnings"] = [*document.get("warnings", []), *match_warnings]
                    document["auto_mapped"] = True
                    defaults = {key: document.get(key, "") for key in ("supplier", "invoice_reference")}
                    for row in rows:
                        row.update(defaults)
                    columns = [label for _, label in INVOICE_COLUMNS]
                    raw = [[row.get(field, "") for field, _ in INVOICE_COLUMNS] for row in rows]
                    mapping = {field: str(index) for index, (field, _) in enumerate(INVOICE_COLUMNS)}
                    fingerprint = parsed["fingerprint"]
                else:
                    columns, raw, fingerprint = extract_rows(source, form.cleaned_data["header"])
                    rows, mapping = [], {}
            else:
                columns, raw, fingerprint = extract_rows(source, form.cleaned_data["header"])
                rows, mapping = [], {}
            previous = MedicineImport.objects.filter(fingerprint=fingerprint, committed_at__isnull=False)
            if not is_admin(request.user):
                previous = previous.filter(owner=request.user)
            previous = previous.first()
            if previous:
                messages.info(request, "This exact file was imported previously. Its saved result is shown below.")
                return redirect("pharmacy_import_review", pk=previous.pk)
            draft = MedicineImport.objects.create(owner=request.user, source=source, filename=Path(source.name).name[:255],
                fingerprint=fingerprint, kind=form.cleaned_data["kind"], columns=columns, raw_rows=raw,
                mapping=mapping, rows=rows, document=document)
            return redirect("pharmacy_import_review", pk=draft.pk)
        except ValidationError as exc:
            form.add_error("source", exc)
    drafts = MedicineImport.objects.all() if is_admin(request.user) else MedicineImport.objects.filter(owner=request.user)
    return render(request, "core/pharmacy_import_upload.html", {"page_title": "Import stock from invoice" if stock_mode else "Import medicines / stock", "form": form, "drafts": drafts[:20], "stock_mode": stock_mode})


def mapping_form(draft, data=None):
    fields = ImportRowForm(kind=draft.kind).fields
    choices = [("", "Not in file"), *[(str(i), f"{i+1}. {title or 'Untitled'}") for i, title in enumerate(draft.columns)]]
    form = forms.Form(data)
    for name, field in fields.items():
        if name in ("include", "medicine", "duplicate_ok"):
            continue
        initial = next((str(i) for i, title in enumerate(draft.columns)
                        if normalized(title).replace(" ", "_") == name), "")
        form.fields[name] = forms.ChoiceField(label=field.label or name.replace("_", " ").title(),
                                             choices=choices, required=False, initial=draft.mapping.get(name, initial))
    for name in ("unit", "form", "pack_size", "supplier", "invoice_reference", "quantity_basis", "price_basis"):
        if name in fields:
            field = fields[name]
            field = copy.deepcopy(field)
            field.required = False
            field.label = f"Default {name.replace('_', ' ')} (optional)"
            form.fields["default_" + name] = field
    return form


@login_required(login_url="login")
def import_review(request, pk):
    draft = draft_for(request, pk)
    if draft.committed_at:
        return render(request, "core/pharmacy_import_result.html", {"page_title": "Import saved", "draft": draft,
            "medicines": Medicine.objects.filter(pk__in=draft.result.get("medicine_ids", []))})
    mapping = mapping_form(draft, request.POST if request.POST.get("action") == "map" else None)
    page = Paginator(list(enumerate(draft.rows)), 20).get_page(request.GET.get("page"))
    if request.method == "POST":
        if request.POST.get("revision") != str(draft.revision):
            messages.error(request, "This draft changed in another tab. Review the latest version before saving.")
            return redirect(reverse("pharmacy_import_review", args=[pk], query=request.GET))
        action = request.POST.get("action")
        rows, selected_mapping = draft.rows, draft.mapping
        if action == "map" and mapping.is_valid():
            selected_mapping = {name: value for name, value in mapping.cleaned_data.items() if not name.startswith("default_")}
            defaults = {name.removeprefix("default_"): value for name, value in mapping.cleaned_data.items() if name.startswith("default_") and value not in ("", None)}
            rows = mapped_rows(draft, selected_mapping, defaults)
        elif action in ("save", "commit"):
            rows = list(draft.rows)
            fields = ImportRowForm(kind=draft.kind).fields
            for index, _ in page:
                rows[index] = {name: request.POST.get(f"row-{index}-{name}", "")
                               for name in fields}
                rows[index]["include"] = request.POST.get(f"row-{index}-include") == "on"
        else:
            action = None
        if action:
            changed = MedicineImport.objects.filter(pk=pk, revision=draft.revision, committed_at__isnull=True).update(
                rows=rows, mapping=selected_mapping, revision=F("revision") + 1, result={})
            if not changed:
                messages.error(request, "This draft changed. Review its latest version.")
            elif action == "commit":
                try:
                    commit_import(pk, request.user)
                except ImportReviewError as exc:
                    MedicineImport.objects.filter(pk=pk, committed_at__isnull=True).update(result={"errors": exc.errors})
                    messages.error(request, "Review the highlighted rows before retrying the import.")
                except (IntegrityError, OperationalError):
                    messages.error(request, "Another update is in progress. Your draft is saved. Retry the import.")
            else:
                messages.success(request, "Draft saved.")
            return redirect(reverse("pharmacy_import_review", args=[pk], query=request.GET))
    errors = draft.result.get("errors", {})
    row_forms = []
    for index, row in page:
        form = ImportRowForm(initial=row, prefix=f"row-{index}", kind=draft.kind)
        row_forms.append((index + 1, form, errors.get(str(index), {})))
    error_links = [{"row": int(index) + 1, "page": int(index) // 20 + 1} for index in errors]
    return render(request, "core/pharmacy_import_review.html", {
        "page_title": "Review import", "draft": draft, "mapping": mapping, "page": page,
        "row_forms": row_forms, "error_links": error_links, "document": draft.document,
        "preview": draft.raw_rows[:5], "columns": draft.columns,
        "preview_type": Path(draft.filename).suffix.lower(),
    })


@login_required(login_url="login")
def import_source(request, pk):
    draft = draft_for(request, pk)
    inline = request.GET.get("inline") == "1" and Path(draft.filename).suffix.lower() in (".pdf", ".jpg", ".jpeg", ".png")
    if inline and Path(draft.filename).suffix.lower() == ".pdf":
        import pdfplumber
        try:
            with draft.source.open("rb") as source, pdfplumber.open(source) as pdf:
                preview = pdf.pages[0].to_image(resolution=110).original.convert("RGB")
                output = io.BytesIO()
                preview.save(output, format="PNG")
            response = HttpResponse(output.getvalue(), content_type="image/png")
        except Exception:
            return HttpResponse("PDF preview is unavailable. Download the original document instead.", status=415, content_type="text/plain")
    else:
        response = FileResponse(draft.source.open("rb"), as_attachment=not inline, filename=draft.filename,
                                content_type=mimetypes.guess_type(draft.filename)[0] or "application/octet-stream")
    if inline:
        response["Content-Security-Policy"] = "sandbox"
        response["X-Frame-Options"] = "SAMEORIGIN"
    response["X-Content-Type-Options"] = "nosniff"
    response["Cache-Control"] = "private, no-store"
    return response


@login_required(login_url="login")
def import_template(request):
    require_pharmacy(request.user)
    fields = [name for name in ImportRowForm(kind="stock").fields if name not in ("include", "medicine", "duplicate_ok")]
    if request.GET.get("format") == "xlsx":
        from openpyxl import Workbook
        workbook = Workbook()
        workbook.active.append(fields)
        output = io.BytesIO()
        workbook.save(output)
        response = HttpResponse(output.getvalue(), content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        extension = "xlsx"
    else:
        response = HttpResponse(content_type="text/csv")
        csv.writer(response).writerow(fields)
        extension = "csv"
    response["Content-Disposition"] = f'attachment; filename="pharmacy-import-template.{extension}"'
    return response


@login_required(login_url="login")
def medicine_photo(request, medicine_id):
    require_pharmacy(request.user)
    medicine = get_object_or_404(Medicine, pk=medicine_id)
    imported = None
    import_id = request.GET.get("import", "")
    if import_id.isdigit():
        imports = MedicineImport.objects.filter(pk=import_id, committed_at__isnull=False)
        if not is_admin(request.user):
            imports = imports.filter(owner=request.user)
        imported = imports.first()
    candidates, error, preview_token = [], "", ""
    approved_preview = None
    if request.method == "POST":
        try:
            action = request.POST.get("action")
            source = ""
            if action == "remove":
                data = None
            elif action == "preview_link":
                url_field = forms.URLField(max_length=2000)
                url = url_field.clean(request.POST.get("photo_url", ""))
                data, source = linked_photo(url)
                preview_token = stage_photo(data, medicine, request.user, source)
            elif action == "preview_upload":
                photo = request.FILES.get("photo")
                data = validated_photo(photo.read(MAX_PHOTO_BYTES + 1) if photo else b"")
                preview_token = stage_photo(data, medicine, request.user)
            elif action == "approve" and request.POST.get("preview"):
                data, approved_preview = read_photo_preview(request.POST["preview"], medicine, request.user)
                source = approved_preview["source"]
            elif action == "upload":
                photo = request.FILES.get("photo")
                data = validated_photo(photo.read(MAX_PHOTO_BYTES + 1) if photo else b"")
            elif action == "approve":
                try:
                    choice = signing.loads(request.POST.get("candidate", ""), salt="medicine-photo", max_age=86400)
                except signing.BadSignature:
                    raise ValidationError("This photo suggestion expired. Search again.")
                if choice["medicine"] != medicine.pk:
                    raise ValidationError("This photo suggestion belongs to another medicine.")
                source = choice["candidate"]["source"]
                data = validated_photo(public_get(choice["candidate"]["image"]))
            else:
                raise ValidationError("Choose a valid photo action.")
            if not preview_token:
                if data:
                    medicine.photo.save(uuid4().hex + ".webp", ContentFile(data), save=False)
                else:
                    medicine.photo = ""
                medicine.photo_source = source
                medicine.photo_approved_by = request.user if data else None
                medicine.photo_approved_at = timezone.now() if data else None
                medicine.save(update_fields=["photo", "photo_source", "photo_approved_by", "photo_approved_at"])
                if approved_preview:
                    try:
                        private_report_storage().delete(approved_preview["path"])
                    except OSError:
                        pass  # Expired previews are also cleaned up when another preview is staged.
                # Keep prior files available to any concurrent readers.
                messages.success(request, "Medicine photo saved." if data else "Medicine photo removed.")
                return redirect(reverse("medicine_photo", args=[medicine.pk], query=request.GET))
        except ValidationError as exc:
            error = " ".join(exc.messages)
        except (OSError, ValueError, KeyError, TypeError, HTTPError):
            error = "The photo could not be loaded. Retry or upload a photo."
    if request.method == "GET" and request.GET.get("search") == "1" and settings.BRAVE_SEARCH_API_KEY:
        try:
            candidates = photo_candidates(medicine)
        except ValidationError as exc:
            error = " ".join(exc.messages)
        except (OSError, ValueError, TypeError, HTTPError):
            error = "Photo search is unavailable. Retry or upload a photo."
    return render(request, "core/pharmacy_photo.html", {
        "page_title": "Product photo", "medicine": medicine, "candidates": candidates, "error": error,
        "searched": request.GET.get("search") == "1" and bool(settings.BRAVE_SEARCH_API_KEY), "import_draft": imported,
        "search_url": "https://www.google.com/search?" + urlencode({"tbm": "isch", "q": medicine_image_query(medicine)}),
        "preview_token": preview_token,
        "preview_url": reverse("medicine_photo_preview", args=[medicine.pk], query={"token": preview_token}) if preview_token else "",
        "photo_url": request.POST.get("photo_url", ""),
        "online_suggestions": bool(settings.BRAVE_SEARCH_API_KEY),
    })


@login_required(login_url="login")
def medicine_photo_preview(request, medicine_id):
    require_pharmacy(request.user)
    medicine = get_object_or_404(Medicine, pk=medicine_id)
    try:
        data, _ = read_photo_preview(request.GET.get("token", ""), medicine, request.user)
    except ValidationError:
        return HttpResponse("Photo preview expired. Paste or upload the photo again.", status=404, content_type="text/plain")
    response = HttpResponse(data, content_type="image/webp")
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response
