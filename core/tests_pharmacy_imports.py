import io
import json
import tempfile
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch, Mock
from uuid import uuid4

from django.core import signing
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from PIL import Image

from patients.models import Medicine, MedicineBatch, MedicineImport, StockMovement
from .tests_operations import staff, close_file_without_request_finished
from .pharmacy_imports import extract_rows, commit_import, ImportReviewError, prepare_invoice_rows
from .pharmacy_invoice_parser import parse_invoice_pdf
from .pharmacy_photos import validated_photo, public_get, photo_candidates, linked_photo


class PharmacyImportTests(TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.settings_override = override_settings(
            MEDIA_ROOT=self.folder.name, PRIVATE_REPORT_ROOT=self.folder.name,
            BRAVE_SEARCH_API_KEY="",
            STORAGES={"default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
                      "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}},
        )
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)
        self.pharmacist = staff("import-pharmacist", "pharmacy")
        self.client.force_login(self.pharmacist)
        self.row = {
            "include": True, "name": "Sample 10mg tablets", "generic_name": "", "strength": "10mg",
            "form": "Tablet", "unit": "tablet", "pack_size": "10", "reorder_level": "10",
            "manufacturer": "Sample maker", "barcode": "",
            "batch_number": "B1", "expiry_date": str(timezone.localdate() + timedelta(days=90)),
            "supplier": "Sample supplier", "invoice_reference": "INVOICE-1", "quantity": "2",
            "quantity_basis": "packs", "purchase_cost": "10.00", "selling_price": "20.00", "price_basis": "packs",
        }

    def draft(self, rows=None):
        return MedicineImport.objects.create(
            owner=self.pharmacist, source=SimpleUploadedFile("stock.csv", b"name\nSample\n"),
            filename="stock.csv", fingerprint=str(uuid4()), kind="stock", rows=rows or [self.row],
        )

    def test_uploaded_csv_mapping_commit_retry_and_duplicate_file(self):
        content = b"name,form,unit,pack_size\nDemo,Tablet,tablet,10\n"
        response = self.client.post(reverse("pharmacy_import"), {
            "kind": "catalogue", "header": "on", "source": SimpleUploadedFile("list.csv", content),
        })
        draft = MedicineImport.objects.get()
        review = reverse("pharmacy_import_review", args=[draft.pk])
        self.assertRedirects(response, review)
        self.assertContains(self.client.get(review), "Map file columns")
        self.client.post(review, {"action": "map", "revision": 0, "name": "0", "form": "1", "unit": "2", "pack_size": "3"})
        draft.refresh_from_db()
        self.assertEqual(draft.rows[0]["name"], "Demo")
        row = draft.rows[0]
        payload = {f"row-0-{name}": value for name, value in row.items()}
        payload.update({"row-0-include": "on", "action": "commit", "revision": draft.revision})
        self.client.post(review, payload)
        draft.refresh_from_db()
        self.assertIsNotNone(draft.committed_at)
        self.assertEqual(Medicine.objects.get().pack_size, 10)
        self.client.post(review, payload)
        self.assertEqual(Medicine.objects.count(), 1)
        result = self.client.post(reverse("pharmacy_import"), {
            "kind": "catalogue", "header": "on", "source": SimpleUploadedFile("again.csv", content),
        })
        self.assertRedirects(result, review)
        self.assertEqual(MedicineImport.objects.count(), 1)

    def test_stock_conversion_and_commit_are_idempotent(self):
        draft = self.draft()
        commit_import(draft.pk, self.pharmacist)
        commit_import(draft.pk, self.pharmacist)
        batch = MedicineBatch.objects.get()
        self.assertEqual(batch.on_hand, 20)
        self.assertEqual(str(batch.purchase_cost), "1.00")
        self.assertEqual(str(batch.selling_price), "2.00")
        self.assertEqual(StockMovement.objects.count(), 1)
        with self.assertRaises(ImportReviewError):
            commit_import(self.draft().pk, self.pharmacist)
        self.row["duplicate_ok"] = True
        commit_import(self.draft().pk, self.pharmacist)
        batch.refresh_from_db()
        self.assertEqual(batch.on_hand, 40)

    def test_all_selected_rows_rollback_and_expiry_errors_survive_review(self):
        invalid = {**self.row, "name": "Expired item", "expiry_date": "2000-01-01"}
        draft = self.draft([self.row, invalid])
        with self.assertRaises(ImportReviewError) as failure:
            commit_import(draft.pk, self.pharmacist)
        self.assertIn("expiry_date", failure.exception.errors["1"])
        self.assertEqual(Medicine.objects.count(), 0)
        self.assertEqual(StockMovement.objects.count(), 0)
        self.assertFalse(MedicineImport.objects.get(pk=draft.pk).committed_at)

    def test_invoice_duplicate_history_and_failed_review_are_preserved(self):
        commit_import(self.draft().pk, self.pharmacist)
        self.row["invoice_reference"] = "INVOICE-2"
        commit_import(self.draft().pk, self.pharmacist)
        with self.assertRaises(ImportReviewError):
            commit_import(self.draft().pk, self.pharmacist)
        bad = {**self.row, "expiry_date": "2000-01-01"}
        draft = self.draft([bad])
        url = reverse("pharmacy_import_review", args=[draft.pk])
        payload = {f"row-0-{key}": value for key, value in bad.items()}
        payload.update({"row-0-include": "on", "action": "commit", "revision": "0"})
        self.client.post(url, payload)
        draft.refresh_from_db()
        self.assertIn("expiry_date", draft.result["errors"]["0"])
        self.assertEqual(draft.rows[0]["expiry_date"], "2000-01-01")
        self.assertContains(self.client.get(url), "Cannot receive an expired batch.")
        self.assertEqual(StockMovement.objects.count(), 2)

    def test_existing_identity_mismatch_and_stale_revision_are_rejected(self):
        Medicine.objects.create(name=self.row["name"], strength="20mg", pack_size=10)
        draft = self.draft()
        with self.assertRaises(ImportReviewError):
            commit_import(draft.pk, self.pharmacist)
        review = reverse("pharmacy_import_review", args=[draft.pk])
        self.client.post(review, {"action": "save", "revision": 99})
        self.assertEqual(MedicineImport.objects.get(pk=draft.pk).rows, [self.row])

    def test_source_and_review_are_owner_scoped_and_private(self):
        draft = self.draft()
        url = reverse("pharmacy_import_source", args=[draft.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertIn("no-store", response["Cache-Control"])
        close_file_without_request_finished(response)
        self.client.force_login(staff("other-pharmacist", "pharmacy"))
        self.assertEqual(self.client.get(url).status_code, 404)
        self.client.force_login(staff("doctor-reader", "doctor"))
        self.assertEqual(self.client.get(url).status_code, 403)

    def test_inline_pdf_source_uses_a_safe_image_preview(self):
        output = io.BytesIO()
        Image.new("RGB", (120, 160), "white").save(output, "PDF")
        draft = MedicineImport.objects.create(
            owner=self.pharmacist, source=SimpleUploadedFile("invoice.pdf", output.getvalue()),
            filename="invoice.pdf", fingerprint=str(uuid4()), kind="stock",
        )
        url = reverse("pharmacy_import_source", args=[draft.pk])
        response = self.client.get(url, {"inline": "1"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "image/png")
        self.assertIn("sandbox", response["Content-Security-Policy"])
        download = self.client.get(url)
        self.assertEqual(download["Content-Type"], "application/pdf")
        close_file_without_request_finished(download)

    def test_xlsx_dates_csv_limits_and_downloadable_templates(self):
        from openpyxl import Workbook
        workbook = Workbook()
        workbook.active.append(["name", "expiry_date"])
        workbook.active.append(["Demo", timezone.localdate()])
        output = io.BytesIO()
        workbook.save(output)
        columns, rows, _ = extract_rows(SimpleUploadedFile("items.xlsx", output.getvalue()))
        self.assertEqual(columns, ["name", "expiry_date"])
        self.assertEqual(rows[0][1], str(timezone.localdate()))
        with self.assertRaises(ValidationError):
            extract_rows(SimpleUploadedFile("huge.csv", b"name\n" + b"item\n" * 501))
        for kind in ("csv", "xlsx"):
            self.assertEqual(self.client.get(reverse("pharmacy_import_template"), {"format": kind}).status_code, 200)

    def test_scanned_image_and_pdf_use_ocr_review_rows(self):
        image = Image.new("RGB", (100, 100), "white")
        for extension, fmt in (("png", "PNG"), ("pdf", "PDF")):
            output = io.BytesIO()
            image.save(output, fmt)
            with patch("core.pharmacy_imports.ocr", return_value="Medicine   Quantity\nSample   20") as ocr:
                columns, rows, _ = extract_rows(SimpleUploadedFile("invoice." + extension, output.getvalue()))
                self.assertEqual(rows, [["Sample", "20"]])
                self.assertEqual(columns, ["Medicine", "Quantity"])
                ocr.assert_called_once()

    def invoice_table(self):
        header = ["SN", "HSN\nCODE", "PRODUCT NAME", "", "PACK\nSIZE", "CASE\nLOT", "", "",
                  "MFG.BY/\nMKTD BY", "BATCH NO", "QTY", "QTY\nDISC", "M.R.P.", "EXP\nDT",
                  "P.T.R.", "", "P.T.S.", "DISC\n%", "GST%", "AMOUNT"]
        return [header,
            ["1", "30049099", "NOZCALM NASAL SPRAY 20ML", "NSL", "1X20ML", "600", "", "", "WELLNES",
             "DLSZ6002", "30", "0", "50.00", "07/28", "18.00", "", "18.00", "0.00", "5.00", "540.00"],
            ["2 30049081", "", "GABIPENTA 100 TAB", "TAB", "10X1X10", "30", "", "", "WELLNES",
             "9226003", "4", "0", "750.00", "06/28", "240.00", "", "240.00", "0.00", "5.00", "960.00"]]

    def test_ivaans_style_pdf_parser_extracts_invoice_and_stock_fields(self):
        table = self.invoice_table()
        text = ("IVAANS BIOCARE PRIVATE LIMITED\n"
                "Inv. No. : TI01910/26\n"
                "Date : 06/11/2026\n"
                "TOTAL AMOUNT 1500.00\n"
                "ADD GST 75.00\n"
                "NET AMOUNT 1575.10\n")

        class Page:
            def extract_tables(self):
                return [table]

            def extract_text(self):
                return text

        class PDF:
            pages = [Page()]

            def __len__(self):
                return len(self.pages)

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        with patch("pdfplumber.open", return_value=PDF()):
            parsed = parse_invoice_pdf(SimpleUploadedFile("invoice.pdf", b"%PDF-1.4 sample", content_type="application/pdf"))
        self.assertEqual(parsed["document"]["supplier"], "IVAANS BIOCARE PRIVATE LIMITED")
        self.assertEqual(parsed["document"]["invoice_reference"], "TI01910/26")
        self.assertEqual(parsed["document"]["invoice_date"], "2026-11-06")
        self.assertEqual(parsed["document"]["total_amount"], "1575.10")
        self.assertTrue(any("does not equal" in warning for warning in parsed["document"]["warnings"]))
        self.assertEqual(len(parsed["items"]), 2)
        spray, tablets = parsed["items"]
        self.assertEqual((spray["name"], spray["unit"], spray["pack_size"]), ("NOZCALM", "bottle", "1"))
        self.assertEqual(spray["expiry_date"], "2028-07-31")
        self.assertEqual(spray["gst_rate"], "5.00")
        self.assertEqual(tablets["hsn_code"], "30049081")
        self.assertEqual((tablets["name"], tablets["strength"], tablets["pack_size"]), ("GABIPENTA", "100", "100"))
        self.assertEqual(tablets["mrp"], "7.50")
        self.assertEqual(tablets["expiry_date"], "2028-06-30")

    def test_pdf_upload_auto_maps_reviews_and_commits_inventory(self):
        tablets = {
            "name": "GABIPENTA", "generic_name": "", "strength": "100", "form": "Tablet", "unit": "tablet",
            "pack_size": "100", "reorder_level": "10", "manufacturer": "WELLNES", "barcode": "",
            "hsn_code": "30049081", "batch_number": "9226003", "expiry_date": "2028-06-30",
            "quantity": "4", "quantity_basis": "packs", "purchase_cost": "240.00", "selling_price": "250.00",
            "price_basis": "packs", "mrp": "7.50", "gst_rate": "5.00", "source_description": "GABIPENTA 100 TAB",
            "source_pack_size": "10X1X10", "source_amount": "960.00",
        }
        parsed = {"items": [tablets], "fingerprint": "invoice-fingerprint", "document": {
            "supplier": "IVAANS BIOCARE PRIVATE LIMITED", "invoice_reference": "TI01910/26",
            "invoice_date": "2026-11-06", "item_count": 1, "warnings": [],
        }}
        with patch("core.pharmacy_import_views.parse_invoice_pdf", return_value=parsed):
            response = self.client.post(reverse("pharmacy_import"), {
                "kind": "stock", "header": "on", "source": SimpleUploadedFile("invoice.pdf", b"%PDF-1.4 sample"),
            })
        draft = MedicineImport.objects.get()
        review = reverse("pharmacy_import_review", args=[draft.pk])
        self.assertRedirects(response, review)
        self.assertEqual(draft.rows[0]["supplier"], "IVAANS BIOCARE PRIVATE LIMITED")
        self.assertTrue(draft.document["auto_mapped"])
        self.assertContains(self.client.get(review), "Detected invoice")
        self.assertContains(self.client.get(review), "TI01910/26")
        payload = {f"row-0-{name}": value for name, value in draft.rows[0].items()}
        payload.update({"row-0-include": "on", "action": "commit", "revision": draft.revision})
        self.client.post(review, payload)
        draft.refresh_from_db()
        self.assertIsNotNone(draft.committed_at)
        medicine = Medicine.objects.get()
        batch = MedicineBatch.objects.get()
        self.assertEqual(medicine.hsn_code, "30049081")
        self.assertEqual(str(medicine.gst_rate), "5.00")
        self.assertEqual(batch.on_hand, 400)
        self.assertEqual(str(batch.purchase_cost), "2.40")
        self.assertEqual(str(batch.selling_price), "2.50")
        self.assertEqual(str(batch.mrp), "7.50")

    def test_duplicate_invoice_brands_are_disambiguated_and_existing_strength_matches(self):
        Medicine.objects.create(name="ZEFWAY 200", strength="200", form="Tablet", unit="tablet", pack_size=100)
        items = []
        for strength in ("200", "100"):
            items.append({"name": "ZEFWAY", "strength": strength, "form": "Tablet", "unit": "tablet",
                          "pack_size": "100", "source_description": f"ZEFWAY {strength} DT TAB"})
        rows, warnings = prepare_invoice_rows(items)
        self.assertEqual(rows[0]["name"], "ZEFWAY 200")
        self.assertEqual(rows[0]["medicine"], Medicine.objects.get(name="ZEFWAY 200").pk)
        self.assertEqual(rows[1]["name"], "ZEFWAY 100")
        self.assertNotIn("medicine", rows[1])
        self.assertEqual(warnings, [])

    def test_receive_stock_page_links_to_pdf_import(self):
        response = self.client.get(reverse("pharmacy_receive"))
        self.assertContains(response, reverse("pharmacy_import") + "?kind=stock")
        self.assertContains(response, "Import invoice / PDF")

    def photo_bytes(self):
        output = io.BytesIO()
        Image.new("RGB", (100, 100), "white").save(output, "PNG")
        return output.getvalue()

    def test_photo_upload_approval_removal_and_missing_credentials(self):
        medicine = Medicine.objects.create(name="Photo item")
        url = reverse("medicine_photo", args=[medicine.pk])
        response = self.client.get(url, {"search": "1"})
        self.assertContains(response, "Search images")
        self.assertNotContains(response, "BRAVE_SEARCH_API_KEY")
        result = self.client.post(url, {"action": "upload", "photo": SimpleUploadedFile("photo.png", self.photo_bytes())})
        self.assertEqual(result.status_code, 302)
        medicine.refresh_from_db()
        self.assertEqual(medicine.photo_approved_by, self.pharmacist)
        self.assertEqual(Path(medicine.photo.name).suffix, ".webp")
        token = signing.dumps({"medicine": medicine.pk, "candidate": {
            "source": "https://example.com/product", "image": "https://example.com/photo.png",
        }}, salt="medicine-photo")
        with patch("core.pharmacy_import_views.public_get", return_value=self.photo_bytes()):
            self.client.post(url, {"action": "approve", "candidate": token})
        medicine.refresh_from_db()
        self.assertEqual(medicine.photo_source, "https://example.com/product")
        self.assertContains(self.client.post(url, {"action": "approve", "candidate": "tampered"}), "expired")
        self.client.post(url, {"action": "remove"})
        medicine.refresh_from_db()
        self.assertFalse(medicine.photo)
        self.assertFalse(medicine.photo_approved_at)
        with self.assertRaises(ValidationError):
            validated_photo(b"<svg>bad</svg>")

    def test_uploaded_preview_requires_approval_and_is_private(self):
        medicine = Medicine.objects.create(name="Preview item")
        url = reverse("medicine_photo", args=[medicine.pk])
        response = self.client.post(url, {"action": "preview_upload",
            "photo": SimpleUploadedFile("photo.png", self.photo_bytes())})
        token, preview_url = response.context["preview_token"], response.context["preview_url"]
        self.assertTrue(token)
        medicine.refresh_from_db()
        self.assertFalse(medicine.photo)
        self.assertEqual(self.client.get(preview_url)["Content-Type"], "image/webp")
        with patch("django.core.signing.time.time", return_value=1):
            expired_token = signing.dumps(signing.loads(token, salt="medicine-photo-preview"), salt="medicine-photo-preview")
        expired_response = self.client.post(url, {"action": "approve", "preview": expired_token})
        self.assertContains(expired_response, "preview expired")
        other = staff("preview-other", "pharmacy")
        self.client.force_login(other)
        self.assertEqual(self.client.get(preview_url).status_code, 404)
        self.client.force_login(self.pharmacist)
        self.client.post(url, {"action": "approve", "preview": token})
        medicine.refresh_from_db()
        self.assertTrue(medicine.photo)
        self.assertEqual(self.client.get(preview_url).status_code, 404)
        expired = self.client.post(url, {"action": "approve", "preview": token})
        self.assertContains(expired, "preview expired")

    def test_direct_url_and_relative_page_metadata_previews(self):
        medicine = Medicine.objects.create(name="Linked photo", strength="10mg", manufacturer="Maker")
        url = reverse("medicine_photo", args=[medicine.pk])
        image = self.photo_bytes()
        with patch("core.pharmacy_photos.public_get", return_value=(image, "https://shop.example/product.png")):
            response = self.client.post(url, {"action": "preview_link", "photo_url": "https://shop.example/product.png"})
        self.assertContains(response, "Approve product photo")
        self.assertFalse(Medicine.objects.get(pk=medicine.pk).photo)
        html = b'<meta property="og:image" content="../pack.png"><meta name="twitter:image" content="/other.png">'
        with patch("core.pharmacy_photos.public_get", side_effect=[(html, "https://shop.example/products/demo"), image]) as fetch:
            data, source = linked_photo("https://shop.example/start")
        self.assertTrue(data)
        self.assertEqual(source, "https://shop.example/start")
        self.assertEqual(fetch.call_args.args[0], "https://shop.example/pack.png")
        self.assertIn("Maker", response.context["search_url"])

    def test_metadata_fallback_failures_and_invalid_images(self):
        html = b'<meta property="og:image" content="https://shop.example/broken.png"><meta name="twitter:image" content="/good.png">'
        with patch("core.pharmacy_photos.public_get", side_effect=[
            (html, "https://shop.example/product"), ValidationError("Blocked"), self.photo_bytes(),
        ]) as fetch:
            data, _ = linked_photo("https://shop.example/product")
        self.assertTrue(data)
        self.assertEqual(fetch.call_args.args[0], "https://shop.example/good.png")
        for payload in (b"<html>Scripts required</html>", b"invalid image"):
            with patch("core.pharmacy_photos.public_get", return_value=(payload, "https://shop.example/product")):
                with self.assertRaises(ValidationError):
                    linked_photo("https://shop.example/product")
        with self.assertRaises(ValidationError):
            validated_photo(b"x" * (5 * 1024 * 1024 + 1))

    def test_redirect_to_private_host_is_blocked(self):
        public = [(2, 1, 6, "", ("8.8.8.8", 443))]
        private = [(2, 1, 6, "", ("127.0.0.1", 443))]
        response = Mock(status=302, headers={"Location": "https://internal.example/photo.png"})
        with patch("core.pharmacy_photos.socket.getaddrinfo", side_effect=[public, private]):
            with patch("core.pharmacy_photos.urllib3.HTTPSConnectionPool") as pool:
                pool.return_value.urlopen.return_value = response
                with self.assertRaises(ValidationError):
                    public_get("https://shop.example/photo.png")
                self.assertEqual(pool.call_count, 1)

    def test_private_image_hosts_blocked_before_http_connection(self):
        with patch("core.pharmacy_photos.socket.getaddrinfo", return_value=[(2, 1, 6, "", ("127.0.0.1", 443))]):
            with patch("core.pharmacy_photos.urllib3.HTTPSConnectionPool") as pool:
                with self.assertRaises(ValidationError):
                    public_get("https://example.com/photo.png")
                pool.assert_not_called()

    @override_settings(BRAVE_SEARCH_API_KEY="fake-test-key")
    def test_search_signs_candidates_without_saving_a_photo(self):
        medicine = Medicine.objects.create(name="Demo", manufacturer="Maker", strength="10mg")
        payload = {"results": [{"title": "Demo 10mg", "url": "https://example.com/product",
                    "thumbnail": {"src": "https://example.com/thumb.png"},
                    "properties": {"url": "https://example.com/full.png"}}]}
        with patch("core.pharmacy_photos.public_get", return_value=json.dumps(payload).encode()) as get:
            candidates = photo_candidates(medicine)
        self.assertEqual(len(candidates), 1)
        self.assertIn("country=IN", get.call_args.args[0])
        self.assertEqual(signing.loads(candidates[0]["token"], salt="medicine-photo")["medicine"], medicine.pk)
        medicine.refresh_from_db()
        self.assertFalse(medicine.photo)
