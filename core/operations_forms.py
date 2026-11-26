from decimal import Decimal
from pathlib import Path
from django import forms
from django.core.exceptions import ValidationError
from django.utils import timezone
from patients.models import (
    Medicine, MedicineBatch, ServiceDefinition, PharmacySettings, PharmacySupplier,
)


class MedicineForm(forms.ModelForm):
    class Meta:
        model = Medicine
        fields = [
            "name", "generic_name", "strength", "form", "unit", "pack_size", "reorder_level",
            "is_active", "manufacturer", "barcode", "composition", "category", "hsn_code",
            "drug_schedule", "prescription_required", "is_controlled", "storage_instructions",
            "rack_location", "gst_rate",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # A documented GST rate defaults to zero; a saved medicine can omit it.
        self.fields["gst_rate"].required = False

    def clean_pack_size(self):
        value = self.cleaned_data["pack_size"]
        if value < 1:
            raise ValidationError("A pack must contain at least one dispensing unit.")
        return value

    def clean_gst_rate(self):
        value = self.cleaned_data.get("gst_rate") or Decimal("0")
        if value < 0 or value > 100:
            raise ValidationError("GST rate must be between 0 and 100 percent.")
        return value


class ReceiptForm(forms.Form):
    medicine = forms.ModelChoiceField(queryset=Medicine.objects.filter(is_active=True))
    batch_number = forms.CharField(max_length=100)
    expiry_date = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))
    supplier = forms.CharField(max_length=200)
    invoice_reference = forms.CharField(max_length=100, help_text="Use an opening stock reference for your first stock-taking.")
    packs = forms.IntegerField(min_value=1, max_value=1000000, label="Packs received", help_text="Packs × catalogue pack size = dispensing units received.")
    purchase_cost = forms.DecimalField(min_value=0, max_digits=10, decimal_places=2, label="Purchase cost per dispensing unit (₹)")
    selling_price = forms.DecimalField(min_value=0, max_digits=10, decimal_places=2, label="Selling price per dispensing unit (₹)")
    request_key = forms.UUIDField(widget=forms.HiddenInput)

    def clean_expiry_date(self):
        value = self.cleaned_data["expiry_date"]
        if value < timezone.localdate():
            raise ValidationError("Cannot receive an expired batch.")
        return value


class BatchPriceForm(forms.Form):
    batch = forms.ModelChoiceField(queryset=MedicineBatch.objects.select_related("medicine"), widget=forms.HiddenInput)
    purchase_cost = forms.DecimalField(min_value=0, max_digits=10, decimal_places=2, label="Purchase cost (₹)")
    selling_price = forms.DecimalField(min_value=0, max_digits=10, decimal_places=2, label="Selling price (₹)")
    mrp = forms.DecimalField(min_value=0, max_digits=10, decimal_places=2, required=False, label="MRP (₹)")
    reason = forms.CharField(max_length=255, label="Reason for change")
    request_key = forms.UUIDField(widget=forms.HiddenInput)


class AdjustmentForm(forms.Form):
    batch = forms.ModelChoiceField(queryset=MedicineBatch.objects.select_related("medicine"))
    quantity = forms.IntegerField(min_value=-1000000, max_value=1000000, help_text="Correction in dispensing units; use a negative number to remove stock.")
    reason = forms.CharField(max_length=255)
    request_key = forms.UUIDField(widget=forms.HiddenInput)


class ServiceDefinitionForm(forms.ModelForm):
    class Meta:
        model = ServiceDefinition
        fields = ["name", "department", "modality", "price", "is_active"]

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("modality") and cleaned.get("department") and cleaned["department"].slug != "radiology":
            self.add_error("modality", "Modality only applies to radiology services.")
        return cleaned

    def clean_price(self):
        value = self.cleaned_data.get("price") or 0
        if value < 0:
            raise ValidationError("Price cannot be negative.")
        return value


class ReportForm(forms.Form):
    title = forms.CharField(max_length=255)
    summary = forms.CharField(widget=forms.Textarea(attrs={"rows": 4}), max_length=10000)
    attachment = forms.FileField(help_text="PDF, JPEG or PNG, up to 10 MB.")

    def clean_attachment(self):
        value = self.cleaned_data["attachment"]
        if value.size > 10 * 1024 * 1024:
            raise ValidationError("Reports must be 10 MB or smaller.")
        extension = Path(value.name).suffix.lower()
        header = value.read(8)
        value.seek(0)
        if extension == ".pdf":
            if not header.startswith(b"%PDF-"):
                raise ValidationError("Choose a valid PDF file.")
        elif extension in (".png", ".jpg", ".jpeg"):
            from PIL import Image, UnidentifiedImageError
            try:
                with Image.open(value) as img:
                    if img.format not in ("JPEG", "PNG"):
                        raise ValidationError("Choose a JPEG or PNG image.")
                    img.verify()
            except (UnidentifiedImageError, OSError, ValueError):
                raise ValidationError("Choose a valid image.")
            finally:
                value.seek(0)
        else:
            raise ValidationError("Choose a PDF, JPEG or PNG report.")
        return value


class PharmacySupplierForm(forms.ModelForm):
    class Meta:
        model = PharmacySupplier
        fields = ["name", "contact_person", "phone", "email", "address", "gstin", "drug_licence",
                  "payment_terms", "credit_period_days", "is_active"]
        widgets = {"address": forms.Textarea(attrs={"rows": 3})}


class PharmacySettingsForm(forms.ModelForm):
    class Meta:
        model = PharmacySettings
        fields = ["expiry_warning_days", "near_expiry_warn_days", "default_gst_rate",
                  "max_discount_percent", "allow_direct_sales", "require_controlled_authorisation"]
        labels = {
            "expiry_warning_days": "Watch window (days)",
            "near_expiry_warn_days": "Critical window (days)",
            "default_gst_rate": "Default GST rate (%)",
            "max_discount_percent": "Maximum bill discount (%)",
            "allow_direct_sales": "Enable direct POS sales",
            "require_controlled_authorisation": "Require controlled-medicine authorisation",
        }
        help_texts = {
            "expiry_warning_days": "Batches inside this window appear on the watch list.",
            "near_expiry_warn_days": "The tighter window used by the dashboard and hero capsule.",
        }

    def clean_default_gst_rate(self):
        value = self.cleaned_data.get("default_gst_rate") or Decimal("0")
        if value < 0 or value > 100:
            raise ValidationError("Default GST rate must be between 0 and 100 percent.")
        return value

    def clean_max_discount_percent(self):
        value = self.cleaned_data.get("max_discount_percent") or Decimal("0")
        if value < 0 or value > 100:
            raise ValidationError("Maximum discount must be between 0 and 100 percent.")
        return value

    def clean(self):
        cleaned = super().clean()
        near = cleaned.get("near_expiry_warn_days")
        watch = cleaned.get("expiry_warning_days")
        if near is not None and watch is not None and near > watch:
            self.add_error("near_expiry_warn_days",
                           "The critical window must be shorter than or equal to the watch window.")
        return cleaned


class StockHoldForm(forms.Form):
    batch = forms.ModelChoiceField(queryset=MedicineBatch.objects.select_related("medicine"))
    quantity = forms.IntegerField(min_value=1)
    reason = forms.CharField(max_length=255)
    request_key = forms.UUIDField(widget=forms.HiddenInput)


class QuarantineReleaseForm(forms.Form):
    batch = forms.ModelChoiceField(queryset=MedicineBatch.objects.select_related("medicine"))
    quantity = forms.IntegerField(min_value=1)
    outcome = forms.ChoiceField(choices=[("active", "Release back to saleable stock"),
                                         ("damaged", "Move to damaged"),
                                         ("writeoff", "Write off and remove from stock")])
    reason = forms.CharField(max_length=255)
    request_key = forms.UUIDField(widget=forms.HiddenInput)


class StockTransferForm(forms.Form):
    batch = forms.ModelChoiceField(queryset=MedicineBatch.objects.select_related("medicine"))
    quantity = forms.IntegerField(min_value=1, help_text="Units moved in this transfer; recorded in the ledger.")
    to_location = forms.CharField(max_length=100, help_text="The batch keeps one storage location; this becomes its new location.")
    reason = forms.CharField(max_length=255)
    request_key = forms.UUIDField(widget=forms.HiddenInput)


class PatientReturnForm(forms.Form):
    sale_line = forms.ModelChoiceField(queryset=None)
    quantity = forms.IntegerField(min_value=1)
    reason = forms.CharField(max_length=255)
    request_key = forms.UUIDField(widget=forms.HiddenInput)

    def __init__(self, *args, sale=None, **kwargs):
        super().__init__(*args, **kwargs)
        from patients.models import PharmacySaleLine
        lines = PharmacySaleLine.objects.select_related("sale", "medicine", "batch").filter(
            sale__status="completed")
        if sale is not None:
            lines = lines.filter(sale=sale)
        self.fields["sale_line"].queryset = lines.order_by("-sale__completed_at", "-pk")

        def sale_line_label(line):
            when = line.sale.completed_at
            stamp = f" · {when:%d %b %Y}" if when else ""
            return (f"{line.sale.reference or 'Sale'} · {line.medicine_name} · "
                    f"{line.quantity} sold at ₹{line.unit_price}{stamp}")

        self.fields["sale_line"].label_from_instance = sale_line_label
        self.fields["sale_line"].help_text = (
            "Only completed sales can be returned." if sale is None
            else "Showing the lines from the selected sale only.")


class SupplierReturnForm(forms.Form):
    batch = forms.ModelChoiceField(queryset=MedicineBatch.objects.select_related("medicine").filter(on_hand__gt=0))
    quantity = forms.IntegerField(min_value=1)
    reason = forms.CharField(max_length=255)
    request_key = forms.UUIDField(widget=forms.HiddenInput)


class PatientRefundForm(forms.Form):
    return_id = forms.IntegerField(min_value=1, widget=forms.HiddenInput)
    request_key = forms.UUIDField(widget=forms.HiddenInput)


class PurchaseLineForm(forms.Form):
    medicine = forms.ModelChoiceField(queryset=Medicine.objects.filter(is_active=True))
    batch_number = forms.CharField(max_length=100, required=False)
    expiry_date = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))
    quantity = forms.IntegerField(min_value=1)
    purchase_cost = forms.DecimalField(min_value=0, max_digits=10, decimal_places=2)
    selling_price = forms.DecimalField(min_value=0, max_digits=10, decimal_places=2)
    mrp = forms.DecimalField(min_value=0, max_digits=10, decimal_places=2, required=False)


PurchaseLineFormSet = forms.formset_factory(PurchaseLineForm, extra=3, can_delete=True)


class PurchaseReceiptForm(forms.Form):
    MODE_ORDER = "order"
    MODE_RECEIVE = "receive"
    MODE_CHOICES = [
        (MODE_RECEIVE, "Post goods receipt — stock enters inventory now"),
        (MODE_ORDER, "Record purchase order — no stock until goods are received"),
    ]

    def clean(self):
        cleaned = super().clean()
        cleaned["mode"] = cleaned.get("mode") or self.MODE_RECEIVE
        return cleaned

    mode = forms.ChoiceField(
        choices=MODE_CHOICES, initial=MODE_RECEIVE, required=False, widget=forms.RadioSelect,
        label="What are you recording?",
        help_text="Orders can be received line by line later from the purchase detail page.")
    supplier = forms.ModelChoiceField(queryset=PharmacySupplier.objects.filter(is_active=True))
    reference = forms.CharField(max_length=120, label="Purchase reference")
    invoice_reference = forms.CharField(max_length=100, required=False)
    request_key = forms.UUIDField(widget=forms.HiddenInput)


class PurchaseLineDetailsForm(forms.Form):
    """Batch details captured against an ordered line before it is received."""

    batch_number = forms.CharField(max_length=100)
    expiry_date = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))

    def clean_expiry_date(self):
        value = self.cleaned_data["expiry_date"]
        if value < timezone.localdate():
            raise ValidationError("Cannot receive an expired batch.")
        return value
