"""Recognise supplier invoice tables and produce reviewable pharmacy stock rows.

The generic file importer deliberately treats every extracted row as data. Real
invoices, however, wrap the product table in customer, tax and total rows. This
module finds the product header, maps common column names and keeps uncertain
values visible for pharmacist review instead of guessing silently.
"""
import hashlib
import io
import re
from calendar import monthrange
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from django.core.exceptions import ValidationError

MAX_BYTES = 10 * 1024 * 1024
MAX_PAGES = 10
MAX_ROWS = 500

INVOICE_COLUMNS = [
    ("name", "Medicine / brand name"),
    ("strength", "Strength"),
    ("form", "Form"),
    ("unit", "Unit"),
    ("pack_size", "Pack size"),
    ("manufacturer", "Manufacturer"),
    ("hsn_code", "HSN code"),
    ("batch_number", "Batch number"),
    ("expiry_date", "Expiry date"),
    ("quantity", "Quantity"),
    ("purchase_cost", "Purchase cost"),
    ("selling_price", "Selling price"),
    ("mrp", "MRP per dispensing unit"),
    ("gst_rate", "GST %"),
]

HEADER_ALIASES = {
    "name": ("PRODUCT NAME", "ITEM NAME", "MEDICINE NAME", "PRODUCT", "ITEM", "DESCRIPTION", "MEDICINE"),
    "pack_size": ("PACK SIZE", "PACKING", "PACK"),
    "manufacturer": ("MFG BY MKTD BY", "MFG BY", "MANUFACTURER", "COMPANY", "MFR"),
    "hsn_code": ("HSN CODE", "HSN", "HS CODE"),
    "batch_number": ("BATCH NO", "BATCH NUMBER", "BATCH", "LOT NO", "LOT"),
    "quantity": ("QTY", "QUANTITY"),
    "mrp": ("M R P", "MRP"),
    "expiry_date": ("EXP DT", "EXP DATE", "EXPIRY DATE", "EXPIRY", "EXP"),
    "purchase_cost": ("P T R", "PTR", "PURCHASE RATE", "BUY RATE", "COST RATE"),
    "selling_price": ("P T S", "PTS", "SELLING RATE", "SALE RATE", "MRP RATE"),
    "gst_rate": ("GST%", "GST", "GST PERCENT", "TAX PERCENT", "TAX"),
    "amount": ("AMOUNT", "LINE TOTAL", "TOTAL"),
}

FORM_PATTERNS = [
    (r"\bNASAL\s+SPRAY\b", "Nasal spray", "bottle"),
    (r"\bTABLETS?\b", "Tablet", "tablet"),
    (r"\bTAB\b", "Tablet", "tablet"),
    (r"\bCAPSULES?\b", "Capsule", "capsule"),
    (r"\bCAP\b", "Capsule", "capsule"),
    (r"\bSYRUP\b", "Syrup", "bottle"),
    (r"\bSYP\b", "Syrup", "bottle"),
    (r"\bDROPS?\b", "Drops", "bottle"),
    (r"\bINJECTIONS?\b", "Injection", "vial"),
    (r"\bINJ\b", "Injection", "vial"),
    (r"\bCREAM\b", "Cream", "tube"),
    (r"\bOINTMENT\b", "Ointment", "tube"),
    (r"\bGEL\b", "Gel", "tube"),
    (r"\bSOLUTION\b", "Solution", "bottle"),
    (r"\bSPRAY\b", "Spray", "bottle"),
]

FORM_HINTS = {
    "TAB": ("Tablet", "tablet"),
    "TABLET": ("Tablet", "tablet"),
    "CAP": ("Capsule", "capsule"),
    "CAPSULE": ("Capsule", "capsule"),
    "SYP": ("Syrup", "bottle"),
    "SYRUP": ("Syrup", "bottle"),
    "NSL": ("Nasal spray", "bottle"),
    "DROP": ("Drops", "bottle"),
    "DROPS": ("Drops", "bottle"),
    "INJ": ("Injection", "vial"),
}


class InvoiceNotRecognised(Exception):
    """The PDF did not contain a recognisable medicine invoice table."""


def _text(value):
    return " ".join(str(value or "").replace("\xa0", " ").split())


def _header(value):
    return re.sub(r"[^A-Z0-9%]+", " ", _text(value).upper()).strip()


def _cell(row, index):
    return _text(row[index]) if index is not None and index < len(row) else ""


def _find_header(headers, aliases):
    for alias in aliases:
        for index, value in enumerate(headers):
            if value == alias:
                return index
    return None


def _header_map(row):
    headers = [_header(value) for value in row]
    mapping = {field: _find_header(headers, aliases) for field, aliases in HEADER_ALIASES.items()}
    if mapping["name"] is None or mapping["quantity"] is None:
        return None
    if not any(mapping[field] is not None for field in ("batch_number", "expiry_date", "purchase_cost", "selling_price")):
        return None
    return mapping


def _money(value):
    text = _text(value).upper().replace("₹", "").replace("RS.", "").replace(",", "").strip()
    if not text:
        return None
    try:
        number = Decimal(text)
    except InvalidOperation:
        return None
    if not number.is_finite():
        return None
    return number.quantize(Decimal(".01"), rounding=ROUND_HALF_UP)


def _quantity(value):
    number = _money(value)
    if number is None or number <= 0 or number != number.to_integral_value():
        return None
    return str(int(number))


def _expiry(value):
    text = _text(value).upper().replace(".", "/").replace("-", "/")
    if not text:
        return ""
    for pattern, has_day in ((r"^(\d{1,2})/(\d{1,2})/(\d{2,4})$", True),
                             (r"^(\d{1,2})/(\d{1,2})$", False),
                             (r"^(\d{4})/(\d{1,2})$", False)):
        match = re.match(pattern, text)
        if not match:
            continue
        try:
            if has_day:
                day, month, year = (int(part) for part in match.groups())
                if year < 100:
                    year += 2000
                return date(year, month, day).isoformat()
            if len(match.groups()) == 2 and len(match.group(1)) == 4:
                year, month = (int(part) for part in match.groups())
            else:
                month, year = (int(part) for part in match.groups())
                if year < 100:
                    year += 2000
            return date(year, month, monthrange(year, month)[1]).isoformat()
        except ValueError:
            return ""
    for pattern in ("%Y-%m-%d", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, pattern).date().isoformat()
        except ValueError:
            pass
    return ""


def _product(description, form_hint=""):
    original = _text(description)
    working = original.upper()
    form = unit = ""
    for pattern, label, dispensing_unit in FORM_PATTERNS:
        if re.search(pattern, working):
            form, unit = label, dispensing_unit
            working = re.sub(pattern, " ", working)
            break
    if not form:
        hint = FORM_HINTS.get(_text(form_hint).upper())
        if hint:
            form, unit = hint

    volume = re.search(r"(?<!\d)(\d+(?:\.\d+)?)\s*(ML|L)\b", working)
    strength = ""
    if volume:
        strength = f"{volume.group(1)}{volume.group(2)}"
        working = working[:volume.start()] + " " + working[volume.end():]

    # DT describes a tablet variant; it is not part of the brand identity.
    working = re.sub(r"\bDT\b", " ", working)
    working = " ".join(working.split()).strip(" ,;-")
    if not strength:
        match = re.match(r"^(.*?)\s+(\d+(?:\.\d+)?)$", working)
        if match and match.group(1).strip():
            working, strength = match.group(1).strip(" ,;-"), match.group(2)
    name = " ".join(working.split()).strip(" ,;-") or original
    return name, strength, form, unit


def _pack_size(value, unit):
    if unit == "bottle":
        return "1"
    compact = re.sub(r"\s+", "", _text(value).upper()).replace("×", "X").replace("*", "X")
    if re.fullmatch(r"\d+", compact):
        return str(int(compact))
    if re.fullmatch(r"\d+(?:X\d+)+", compact):
        size = 1
        for part in compact.split("X"):
            size *= int(part)
        return str(size)
    return ""


def _hsn(value, serial):
    code = _text(value)
    if re.fullmatch(r"\d{6,8}", code):
        return code
    match = re.match(r"^\s*\d+\s+(\d{6,8})(?:\s|$)", _text(serial))
    return match.group(1) if match else ""


def _parse_items(table):
    header_index = next((index for index, row in enumerate(table) if _header_map(row)), None)
    if header_index is None:
        return []
    mapping = _header_map(table[header_index])
    items = []
    for row in table[header_index + 1:]:
        name_cell = _cell(row, mapping["name"])
        if not name_cell or _header(name_cell) in {"PRODUCT NAME", "ITEM NAME", "DESCRIPTION", "MEDICINE"}:
            continue
        if re.match(r"^(TOTAL|NET|GST|GRAND|SUB\s+TOTAL)\b", name_cell.upper()):
            continue
        quantity = _quantity(_cell(row, mapping["quantity"]))
        if not quantity:
            continue
        serial = _cell(row, 0)
        batch = _cell(row, mapping["batch_number"])
        expiry = _cell(row, mapping["expiry_date"])
        if not re.fullmatch(r"\d+(?:\s+\d{6,8})?", serial) and not (batch or expiry):
            continue

        hint_index = mapping["name"] + 1
        hint = _cell(row, hint_index)
        if _header(hint) not in FORM_HINTS:
            hint = ""
        name, strength, form, unit = _product(name_cell, hint)
        pack_size = _pack_size(_cell(row, mapping["pack_size"]), unit)
        purchase_cost = _money(_cell(row, mapping["purchase_cost"]))
        selling_price = _money(_cell(row, mapping["selling_price"]))
        mrp = _money(_cell(row, mapping["mrp"]))
        if mrp is not None and pack_size:
            mrp = (mrp / Decimal(pack_size)).quantize(Decimal(".01"), rounding=ROUND_HALF_UP)
        elif mrp is not None:
            mrp = None
        gst = _money(_cell(row, mapping["gst_rate"]))
        amount = _money(_cell(row, mapping["amount"]))
        item = {
            "name": name,
            "generic_name": "",
            "strength": strength,
            "form": form,
            "unit": unit,
            "pack_size": pack_size,
            "reorder_level": "10",
            "manufacturer": _cell(row, mapping["manufacturer"]),
            "barcode": "",
            "hsn_code": _hsn(_cell(row, mapping["hsn_code"]), serial),
            "batch_number": batch,
            "expiry_date": _expiry(expiry),
            "quantity": quantity,
            "quantity_basis": "packs",
            "purchase_cost": f"{purchase_cost:.2f}" if purchase_cost is not None else "",
            "selling_price": f"{selling_price:.2f}" if selling_price is not None else "",
            "price_basis": "packs",
            "mrp": f"{mrp:.2f}" if mrp is not None else "",
            "gst_rate": f"{gst:.2f}" if gst is not None else "",
            "source_description": name_cell,
            "source_pack_size": _cell(row, mapping["pack_size"]),
            "source_unit": hint,
            "source_amount": f"{amount:.2f}" if amount is not None else "",
        }
        items.append(item)
        if len(items) > MAX_ROWS:
            raise ValidationError("Split this invoice into imports of up to 500 medicine rows.")
    return items


def _parse_date(value):
    parsed = _expiry(value)
    if not parsed:
        return ""
    # Invoice dates are day-specific, unlike month/year batch expiry dates.
    text = _text(value)
    for pattern in ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d", "%d/%m/%y"):
        try:
            return datetime.strptime(text, pattern).date().isoformat()
        except ValueError:
            pass
    return parsed


def _metadata(text, items):
    lines = [_text(line) for line in text.splitlines() if _text(line)]
    supplier = ""
    for line in lines[:10]:
        upper = line.upper()
        if upper.startswith(("TO,", "M/S", "CUSTOMER", "PATIENT")):
            continue
        if re.search(r"\b(PRIVATE\s+LIMITED|PVT\.?\s+LTD\.?|LTD\.?|LIMITED|PHARMA|BIOCARE)\b", upper):
            supplier = line
            break
    if not supplier:
        for line in lines[:5]:
            if not re.match(r"^(INVOICE|TAX\s+INVOICE|GSTIN|DATE|TO,)", line.upper()):
                supplier = line
                break

    invoice = ""
    match = re.search(
        r"\b(?:INV(?:OICE)?\.?\s*(?:NO\.?|NUMBER)?|INVOICE\s*(?:NO\.?|NUMBER)?|BILL\s*(?:NO\.?|NUMBER)?)"
        r"\s*[:#\-]?\s*([A-Z0-9][A-Z0-9/_\-.]{2,})",
        text, re.IGNORECASE,
    )
    if match:
        invoice = match.group(1).strip().rstrip(".,;")

    invoice_date = ""
    for line in lines:
        upper = line.upper()
        if "DATE" not in upper or re.match(r"^\s*(DUE|L\.?\s*R\.?|DELIVERY)\b", upper):
            continue
        match = re.search(r"\bDATE\s*[:#\-]?\s*(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})", line, re.IGNORECASE)
        if match:
            invoice_date = _parse_date(match.group(1))
            break

    def total(patterns):
        for pattern in patterns:
            match = re.search(pattern + r"\s*[:#\-]?\s*(?:RS\.?|₹)?\s*([\d,]+\.\d{2})", text, re.IGNORECASE)
            if match:
                return _money(match.group(1))
        return None

    net_amount = total((r"NET\s+AMOUNT", r"GRAND\s+TOTAL", r"TOTAL\s+DUE", r"NET\s+TOTAL"))
    subtotal = total((r"SUB\s+TOTAL", r"TOTAL\s+AMOUNT", r"TAXABLE\s+AMOUNT"))
    tax = total((r"TOTAL\s+GST", r"ADD\s+GST", r"GST\s+AMOUNT"))
    line_total = sum((_money(item["source_amount"]) or Decimal("0")) for item in items)
    warnings = []
    if not supplier:
        warnings.append("Supplier could not be detected. Enter the supplier default before importing.")
    if not invoice:
        warnings.append("Invoice number could not be detected. Enter the invoice reference before importing.")
    if net_amount is not None and subtotal is not None and tax is not None:
        if abs((subtotal + tax) - net_amount) > Decimal("0.01"):
            warnings.append("Invoice subtotal plus GST does not equal the net amount. Verify the source document.")
    if line_total and subtotal is not None and abs(line_total - subtotal) > Decimal("0.01"):
        warnings.append("Extracted line amounts do not equal the invoice subtotal. Review quantities and rates.")

    for number, item in enumerate(items, 1):
        if not item["form"] or not item["unit"]:
            warnings.append(f"Row {number}: dosage form/unit needs review.")
        if not item["pack_size"]:
            warnings.append(f"Row {number}: pack size could not be read. Enter packs or dispensing units explicitly.")
        if not item["batch_number"]:
            warnings.append(f"Row {number}: batch number is missing.")
        if not item["expiry_date"]:
            warnings.append(f"Row {number}: expiry date is missing or invalid.")
        elif date.fromisoformat(item["expiry_date"]) < date.today():
            warnings.append(f"Row {number}: expiry date is in the past.")
        if item["purchase_cost"] == "":
            warnings.append(f"Row {number}: purchase rate is missing.")
        if item["selling_price"] == "":
            warnings.append(f"Row {number}: selling rate is missing.")
        hint = FORM_HINTS.get(_text(item.get("source_unit")).upper())
        if hint and item["form"] and hint[0] != item["form"]:
            warnings.append(f"Row {number}: product name and invoice unit differ; the product name was used.")

    return {
        "supplier": supplier,
        "invoice_reference": invoice,
        "invoice_date": invoice_date,
        "subtotal": f"{subtotal:.2f}" if subtotal is not None else "",
        "tax_amount": f"{tax:.2f}" if tax is not None else "",
        "total_amount": f"{net_amount:.2f}" if net_amount is not None else "",
        "line_total": f"{line_total:.2f}",
        "item_count": len(items),
        "parser": "invoice-table",
        "warnings": warnings,
    }


def parse_invoice_pdf(upload):
    """Return canonical rows and invoice metadata, or ask for generic mapping."""
    data = upload.read(MAX_BYTES + 1)
    if not data or len(data) > MAX_BYTES:
        raise ValidationError("Upload a non-empty PDF up to 10 MB.")
    if not data.startswith(b"%PDF"):
        raise ValidationError("The uploaded file is not a PDF.")
    try:
        import pdfplumber
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            if len(pdf.pages) > MAX_PAGES:
                raise ValidationError("Split this PDF into files of up to 10 pages.")
            items = []
            text_parts = []
            for page in pdf.pages:
                text_parts.append(page.extract_text() or "")
                for table in page.extract_tables():
                    items.extend(_parse_items(table))
                    if len(items) > MAX_ROWS:
                        raise ValidationError("Split this invoice into imports of up to 500 medicine rows.")
            text = "\n".join(text_parts)
    except ValidationError:
        raise
    except Exception as exc:
        raise ValidationError("This PDF could not be read. Check its format and try an unencrypted file.") from exc
    finally:
        upload.seek(0)
    if not items:
        raise InvoiceNotRecognised
    document = _metadata(text, items)
    return {
        "items": items,
        "document": document,
        "fingerprint": hashlib.sha256(data).hexdigest(),
    }
