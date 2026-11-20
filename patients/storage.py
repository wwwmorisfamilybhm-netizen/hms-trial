"""Report storage deliberately outside the publicly served media directory."""
from pathlib import Path
from uuid import uuid4
from django.conf import settings
from django.core.files.storage import FileSystemStorage


class PrivateReportStorage(FileSystemStorage):
    @property
    def base_location(self):
        return str(getattr(settings, "PRIVATE_REPORT_ROOT", settings.BASE_DIR / "private_reports"))

    @property
    def location(self):
        import os
        return os.path.abspath(self.base_location)


def private_report_storage():
    return PrivateReportStorage()


def report_upload_path(instance, filename):
    return f"reports/{uuid4().hex}{Path(filename).suffix.lower()}"


def pharmacy_import_path(instance, filename):
    return f"pharmacy_imports/{uuid4().hex}{Path(filename).suffix.lower()}"
