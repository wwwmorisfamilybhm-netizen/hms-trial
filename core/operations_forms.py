from pathlib import Path
from django import forms
from django.core.exceptions import ValidationError
from django.utils import timezone
from patients.models import Medicine, MedicineBatch, ServiceDefinition


class MedicineForm(forms.ModelForm):
    class Meta:
        model = Medicine
        fields = ["name", "generic_name", "strength", "form", "unit", "pack_size", "reorder_level", "is_active", "manufacturer", "barcode"]

    def clean_pack_size(self):
        value = self.cleaned_data["pack_size"]
        if value < 1:
            raise ValidationError("A pack must contain at least one dispensing unit.")
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


class AdjustmentForm(forms.Form):
    batch = forms.ModelChoiceField(queryset=MedicineBatch.objects.select_related("medicine"))
    quantity = forms.IntegerField(min_value=-1000000, max_value=1000000, help_text="Correction in dispensing units; use a negative number to remove stock.")
    reason = forms.CharField(max_length=255)
    request_key = forms.UUIDField(widget=forms.HiddenInput)


class ServiceDefinitionForm(forms.ModelForm):
    class Meta:
        model = ServiceDefinition
        fields = ["name", "department", "price", "is_active"]

    def clean_price(self):
        value = self.cleaned_data.get("price") or 0
        if value < 0:
            raise ValidationError("Price cannot be negative.")
        return value


class ReportForm(forms.Form):
    title = forms.CharField(max_length=255)
    summary = forms.CharField(widget=forms.Textarea, max_length=10000)
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
