"""Extract reviewable pharmacy drafts and commit approved rows through the stock ledger."""
import csv
import hashlib
import io
import re
import zipfile
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from django import forms
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from patients.models import Medicine, MedicineBatch, MedicineImport, MedicineImportReceipt
from .operations import receive_stock, require_pharmacy, is_admin
from .operations_forms import MedicineForm

MAX_BYTES = 10 * 1024 * 1024
MAX_ROWS = 500
MAX_PAGES = 10


class ImportUploadForm(forms.Form):
    kind = forms.ChoiceField(label="Import contents", choices=MedicineImport._meta.get_field("kind").choices)
    source = forms.FileField(label="CSV, Excel, PDF or invoice image")
    header = forms.BooleanField(required=False, initial=True, label="First extracted row contains column headings")


class ImportRowForm(forms.Form):
    include = forms.BooleanField(required=False, initial=True, label="Import this row")
    medicine = forms.ModelChoiceField(queryset=Medicine.objects.all(), required=False, label="Existing medicine (optional)")
    name = forms.CharField(max_length=255, label="Medicine / brand name")
    generic_name = forms.CharField(max_length=255, required=False)
    strength = forms.CharField(max_length=100, required=False)
    form = forms.CharField(max_length=50, label="Form (tablet, syrup, etc.)")
    unit = forms.ChoiceField(choices=[("", "Choose unit"), *Medicine._meta.get_field("unit").choices])
    pack_size = forms.IntegerField(min_value=1, max_value=1000000)
    manufacturer = forms.CharField(max_length=200, required=False)
    barcode = forms.CharField(max_length=100, required=False)
    hsn_code = forms.CharField(max_length=20, required=False, label="HSN code")
    gst_rate = forms.DecimalField(min_value=0, max_value=100, max_digits=5, decimal_places=2, required=False, label="GST %")
    reorder_level = forms.IntegerField(min_value=0, initial=10)
    batch_number = forms.CharField(max_length=100, required=False)
    expiry_date = forms.DateField(required=False, input_formats=["%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"], widget=forms.DateInput(attrs={"type": "date"}))
    supplier = forms.CharField(max_length=200, required=False)
    invoice_reference = forms.CharField(max_length=100, required=False)
    quantity = forms.IntegerField(min_value=1, max_value=1000000, required=False)
    quantity_basis = forms.ChoiceField(required=False, choices=[("", "Choose quantity basis"), ("packs", "Packs"), ("units", "Dispensing units")])
    purchase_cost = forms.DecimalField(min_value=0, max_digits=10, decimal_places=2, required=False)
    selling_price = forms.DecimalField(min_value=0, max_digits=10, decimal_places=2, required=False)
    mrp = forms.DecimalField(min_value=0, max_digits=10, decimal_places=2, required=False, label="MRP per dispensing unit (₹)")
    price_basis = forms.ChoiceField(required=False, choices=[("", "Choose price basis"), ("packs", "Per pack"), ("units", "Per dispensing unit")])
    duplicate_ok = forms.BooleanField(required=False, label="I reviewed the existing invoice / batch and this is an additional receipt")

    def __init__(self, *args, kind="catalogue", **kwargs):
        super().__init__(*args, **kwargs)
        self.kind = kind
        stock_fields = ["batch_number", "expiry_date", "supplier", "invoice_reference", "quantity",
                        "quantity_basis", "purchase_cost", "selling_price", "price_basis"]
        for name in stock_fields:
            self.fields[name].required = kind == "stock"
        if kind == "catalogue":
            for name in [*stock_fields, "duplicate_ok"]:
                self.fields.pop(name)

    def clean(self):
        data = super().clean()
        if self.kind == "stock" and data.get("expiry_date") and data["expiry_date"] < timezone.localdate():
            self.add_error("expiry_date", "Cannot receive an expired batch.")
        return data


def normalized(value):
    return " ".join(str(value).casefold().split())


def cell_text(value):
    if value is None:
        return ""
    if isinstance(value, (datetime, date)):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()[:2000]


def ocr(image):
    import pytesseract
    try:
        if image.width * image.height > 30_000_000:
            raise ValidationError("Invoice image is too large. Use a smaller image.")
        return pytesseract.image_to_string(image.convert("RGB"), lang="eng", config="--psm 6", timeout=25)
    except pytesseract.TesseractNotFoundError:
        raise ValidationError("Scanned invoices need Tesseract OCR installed on the server. CSV, Excel and text PDFs are available.")
    except (RuntimeError, pytesseract.TesseractError):
        raise ValidationError("OCR could not read this invoice within the time limit. Use a clearer or smaller scan.")


def text_rows(text):
    return [re.split(r"\t+|\s{2,}|\s*\|\s*", line.strip()) for line in text.splitlines() if line.strip()]


def extract_rows(upload, has_header=True):
    """Return a bounded raw grid. Every inferred field remains editable."""
    data = upload.read(MAX_BYTES + 1)
    if not data or len(data) > MAX_BYTES:
        raise ValidationError("Upload a non-empty file up to 10 MB.")
    extension = Path(upload.name).suffix.lower()
    rows = []
    try:
        if extension == ".csv":
            text = data.decode("utf-8-sig")
            try:
                dialect = csv.Sniffer().sniff(text[:8192], delimiters=",;\t")
            except csv.Error:
                dialect = csv.excel
            reader = csv.reader(io.StringIO(text), dialect)
            for row in reader:
                rows.append(row)
                if len(rows) > MAX_ROWS + 1:
                    raise ValidationError("Split this file into imports of up to 500 rows.")
        elif extension == ".xlsx":
            from openpyxl import load_workbook
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                if sum(item.file_size for item in archive.infolist()) > 100 * 1024 * 1024:
                    raise ValidationError("This workbook expands beyond the supported size.")
            workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=False)
            try:
                for row in workbook.active.iter_rows(max_col=32, values_only=True):
                    if any(value is not None for value in row):
                        rows.append(list(row))
                    if len(rows) > MAX_ROWS + 1:
                        raise ValidationError("Split this workbook into imports of up to 500 rows.")
            finally:
                workbook.close()
        elif extension == ".pdf":
            import pdfplumber
            if not data.startswith(b"%PDF"):
                raise ValidationError("The uploaded file is not a PDF.")
            with pdfplumber.open(io.BytesIO(data)) as pdf:
                if len(pdf.pages) > MAX_PAGES:
                    raise ValidationError("Split this PDF into files of up to 10 pages.")
                for page in pdf.pages:
                    tables = page.extract_tables()
                    if tables:
                        rows.extend(row for table in tables for row in table)
                    else:
                        text = page.extract_text() or ""
                        if not text.strip():
                            text = ocr(page.to_image(resolution=150).original)
                        rows.extend(text_rows(text))
        elif extension in (".jpg", ".jpeg", ".png"):
            from PIL import Image
            with Image.open(io.BytesIO(data)) as image:
                if image.format not in ("JPEG", "PNG"):
                    raise ValidationError("Choose a JPEG or PNG invoice.")
                rows = text_rows(ocr(image))
        else:
            raise ValidationError("Use CSV, .xlsx, PDF, JPG or PNG.")
    except ValidationError:
        raise
    except Exception:
        raise ValidationError("This file could not be read. Check its format and try an unencrypted file.")
    rows = [[cell_text(value) for value in row][:32] for row in rows if any(cell_text(v) for v in row)]
    if not rows:
        raise ValidationError("No readable rows found. Try a clearer scan or a spreadsheet.")
    if len(rows) > MAX_ROWS + int(has_header):
        raise ValidationError("Split this file into imports of up to 500 rows.")
    for row in rows:
        while row and not row[-1]:
            row.pop()
    width = max(len(row) for row in rows)
    columns = rows.pop(0) if has_header else [f"Column {index + 1}" for index in range(width)]
    if has_header:
        rows = [row for row in rows if [normalized(v) for v in row] != [normalized(v) for v in columns]]
    columns += [f"Column {index + 1}" for index in range(len(columns), width)]
    if not rows:
        raise ValidationError("The file contains headings but no medicine rows.")
    upload.seek(0)
    return columns, rows, hashlib.sha256(data).hexdigest()


def mapped_rows(draft, mapping, defaults):
    result = []
    for raw in draft.raw_rows:
        row = {"include": True, "reorder_level": "10", **defaults}
        for field, column in mapping.items():
            if column != "" and int(column) < len(raw):
                row[field] = raw[int(column)]
        result.append(row)
    return result


def prepare_invoice_rows(items):
    """Disambiguate invoice brands and preselect only safe catalogue matches."""
    rows = []
    for item in items:
        row = dict(item)
        row["include"] = True
        row["_base_name"] = row["name"]
        rows.append(row)

    groups = {}
    for row in rows:
        groups.setdefault(normalized(row["name"]), []).append(row)
    for group in groups.values():
        if len(group) < 2:
            continue
        for row in group:
            suffix = row.get("strength") or row.get("form") or ""
            row["name"] = " ".join(part for part in (row["name"], suffix) if part).strip()

    warnings = []
    for number, row in enumerate(rows, 1):
        aliases = []
        for value in (row["name"], row.get("source_description"), row.get("_base_name")):
            value = normalized(value)
            if value and value not in aliases:
                aliases.append(value)
        candidates = {}
        for alias in aliases:
            for medicine in Medicine.objects.filter(name__iexact=alias):
                candidates[medicine.pk] = medicine
        if not candidates:
            continue
        compatible = []
        for medicine in candidates.values():
            matches = True
            for field in ("strength", "form", "unit", "pack_size"):
                incoming, existing = row.get(field), getattr(medicine, field)
                if incoming not in (None, "") and existing not in (None, "") and normalized(incoming) != normalized(existing):
                    matches = False
                    break
            if matches:
                compatible.append(medicine)
        if len(compatible) == 1:
            medicine = compatible[0]
            row["medicine"] = medicine.pk
            row["name"] = medicine.name
            if not medicine.is_active:
                warnings.append(f"Row {number}: matched catalogue medicine is inactive. Review it before importing.")
        elif len(candidates) > 1:
            warnings.append(f"Row {number}: multiple catalogue medicines match this product. Choose the correct existing medicine.")
        else:
            warnings.append(f"Row {number}: a similarly named catalogue medicine has different product details. Review the name, strength, form and pack size.")
    return rows, warnings


def resolve_medicine(data):
    medicine = data.get("medicine")
    candidates = list(Medicine.objects.filter(name__iexact=data["name"]))
    if medicine is None and candidates:
        if len(candidates) != 1:
            raise ValidationError("Choose the existing catalogue entry explicitly.")
        medicine = candidates[0]
    metadata = {name: data[name] for name in MedicineForm._meta.fields if name in data and data[name] not in (None, "")}
    metadata["is_active"] = True
    if medicine:
        for field in ("name", "strength", "form", "unit", "pack_size", "manufacturer", "barcode", "hsn_code"):
            incoming, existing = data.get(field), getattr(medicine, field)
            if incoming not in (None, "") and normalized(incoming) != normalized(existing):
                if existing or field not in ("manufacturer", "barcode", "strength") or (field == "strength" and medicine.batches.exists()):
                    raise ValidationError(f"{field.replace('_', ' ').title()} differs from the existing medicine. Correct this row or use a distinct product name.")
        incoming_gst = data.get("gst_rate")
        if incoming_gst not in (None, "") and incoming_gst != medicine.gst_rate:
            if medicine.gst_rate not in (0, Decimal("0")):
                raise ValidationError("GST % differs from the existing medicine. Correct this row or update the catalogue explicitly.")
        if not medicine.is_active:
            raise ValidationError("This catalogue medicine is inactive. Review it before receiving stock.")
        additions = []
        for field in ("manufacturer", "barcode", "generic_name", "strength", "hsn_code"):
            if not getattr(medicine, field) and data.get(field):
                setattr(medicine, field, data[field])
                additions.append(field)
        if incoming_gst not in (None, "") and medicine.gst_rate in (0, Decimal("0")):
            medicine.gst_rate = incoming_gst
            additions.append("gst_rate")
        if additions:
            medicine.full_clean()
            medicine.save(update_fields=additions)
        return medicine, False
    medicine = Medicine(**metadata)
    medicine.full_clean()
    medicine.save()
    return medicine, True


@transaction.atomic
def commit_import(draft_id, user):
    require_pharmacy(user)
    owned = MedicineImport.objects.get(pk=draft_id)
    if owned.owner_id != user.pk and not is_admin(user):
        from django.core.exceptions import PermissionDenied
        raise PermissionDenied
    # Claim a write before reading balances, including on SQLite.
    if not MedicineImport.objects.filter(pk=draft_id, committed_at__isnull=True).update(revision=F("revision") + 1):
        return MedicineImport.objects.get(pk=draft_id)
    draft = MedicineImport.objects.get(pk=draft_id)
    result = {"created": 0, "received": 0, "medicine_ids": []}
    errors = {}
    for index, row in enumerate(draft.rows):
        if not row.get("include"):
            continue
        form = ImportRowForm(row, kind=draft.kind)
        if not form.is_valid():
            errors[str(index)] = form.errors.get_json_data()
            continue
        try:
            with transaction.atomic():
                data = form.cleaned_data
                medicine, created = resolve_medicine(data)
                result["created"] += int(created)
                if draft.kind == "stock":
                    receipt_identity = [str(medicine.pk), normalized(data["supplier"]),
                                        normalized(data["invoice_reference"]), normalized(data["batch_number"])]
                    signature = hashlib.sha256("\0".join(receipt_identity).encode()).hexdigest()
                    duplicate = MedicineImportReceipt.objects.filter(signature=signature).exists() or MedicineBatch.objects.filter(
                        medicine=medicine, batch_number=data["batch_number"],
                        supplier=data["supplier"], invoice_reference=data["invoice_reference"],
                    ).exists()
                    if duplicate and not data.get("duplicate_ok"):
                        raise ValidationError("This invoice / batch already exists. Review it and acknowledge an additional receipt.")
                    divisor = Decimal(medicine.pack_size) if data["price_basis"] == "packs" else Decimal(1)
                    unit_cost = (data["purchase_cost"] / divisor).quantize(Decimal(".01"), rounding=ROUND_HALF_UP)
                    unit_price = (data["selling_price"] / divisor).quantize(Decimal(".01"), rounding=ROUND_HALF_UP)
                    quantity = data["quantity"] * medicine.pack_size if data["quantity_basis"] == "packs" else data["quantity"]
                    receive_stock(user=user, medicine=medicine, batch_number=data["batch_number"],
                                  expiry_date=data["expiry_date"], supplier=data["supplier"],
                                  invoice_reference=data["invoice_reference"], quantity_units=quantity,
                                  purchase_cost=unit_cost, selling_price=unit_price, mrp=data.get("mrp"),
                                  request_key=uuid5(NAMESPACE_URL, f"medicine-import/{draft.pk}/{index}"))
                    MedicineImportReceipt.objects.create(draft=draft, medicine=medicine, row_number=index, signature=signature)
                    result["received"] += 1
                if medicine.pk not in result["medicine_ids"]:
                    result["medicine_ids"].append(medicine.pk)
        except ValidationError as exc:
            errors[str(index)] = {"__all__": [{"message": message} for message in exc.messages]}
    if errors:
        raise ImportReviewError(errors)
    if not result["medicine_ids"]:
        raise ImportReviewError({"0": {"__all__": [{"message": "Select at least one row to import."}]}})
    draft.result = result
    draft.committed_at = timezone.now()
    draft.save(update_fields=["result", "committed_at"])
    return draft


class ImportReviewError(Exception):
    def __init__(self, errors):
        self.errors = errors
