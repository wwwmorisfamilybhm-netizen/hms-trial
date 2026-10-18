"""Portable, per-patient backup archives for CAMS HMS."""

import base64
import binascii
import hashlib
import io
import json
import mimetypes
import os
import zipfile
from datetime import date, datetime, time
from decimal import Decimal

from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.management.color import no_style
from django.core.serializers.json import DjangoJSONEncoder
from django.db import connection, transaction
from django.utils import timezone

from appointments.models import Appointment, Doctor
from patients.models import Patient


ARCHIVE_FORMAT = "cams-hms-patient-backup"
ARCHIVE_VERSION = 1
MAX_UPLOAD_BYTES = 100 * 1024 * 1024
MAX_ARCHIVE_BYTES = 500 * 1024 * 1024
MAX_PATIENT_FILES = 10000
MAX_PHOTO_BYTES = 20 * 1024 * 1024
ALLOWED_PHOTO_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}


class BackupArchiveError(ValueError):
    """Raised when a patient backup archive is invalid or unsafe to import."""


def _json_value(value):
    if isinstance(value, (date, datetime, time)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return value


def _model_values(instance, *, exclude=()):
    values = {}
    for field in instance._meta.concrete_fields:
        if field.name in exclude or field.primary_key:
            continue
        values[field.name] = _json_value(getattr(instance, field.attname))
    return values


def _patient_record(patient):
    photo = None
    if patient.photo:
        with patient.photo.open("rb") as photo_file:
            photo_bytes = photo_file.read(MAX_PHOTO_BYTES + 1)
        if len(photo_bytes) > MAX_PHOTO_BYTES:
            raise BackupArchiveError(
                f"Photo for MR #{patient.mr_number or patient.pk} exceeds the 20 MB backup limit."
            )
        photo_name = os.path.basename(patient.photo.name)
        photo_type = mimetypes.guess_type(photo_name)[0]
        if photo_type not in ALLOWED_PHOTO_TYPES:
            raise BackupArchiveError(
                f"Photo for MR #{patient.mr_number or patient.pk} is not a supported image type."
            )
        photo = {
            "filename": photo_name,
            "mime_type": photo_type,
            "data_base64": base64.b64encode(photo_bytes).decode("ascii"),
        }

    appointments = []
    for appointment in patient.appointments.select_related("doctor").all():
        doctor = appointment.doctor
        appointments.append({
            "source_appointment_id": appointment.pk,
            "doctor": {
                "source_doctor_id": doctor.pk,
                "first_name": doctor.first_name,
                "last_name": doctor.last_name,
                "specialty": doctor.specialty,
                "phone": doctor.phone,
                "email": doctor.email,
            },
            "appointment": _model_values(
                appointment,
                exclude=("patient", "doctor"),
            ),
        })

    return {
        "schema": "cams-hms.patient",
        "version": ARCHIVE_VERSION,
        "source_patient_id": patient.pk,
        "patient": _model_values(patient, exclude=("photo",)),
        "photo": photo,
        "appointments": appointments,
    }


def build_patient_backup():
    """Return a ZIP containing one JSON file per patient plus a manifest."""
    output = io.BytesIO()
    file_hashes = {}
    patients = Patient.objects.order_by("mr_number", "pk").iterator(chunk_size=200)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        patient_count = 0
        for patient in patients:
            patient_count += 1
            record = _patient_record(patient)
            filename_key = patient.mr_number or f"patient-{patient.pk}"
            safe_key = "".join(char for char in filename_key if char.isalnum() or char in "-_")
            safe_key = safe_key[:80] or f"patient-{patient.pk}"
            archive_path = f"patients/{safe_key}-{patient.pk}.json"
            payload = json.dumps(record, ensure_ascii=False, indent=2, cls=DjangoJSONEncoder).encode("utf-8")
            archive.writestr(archive_path, payload)
            file_hashes[archive_path] = hashlib.sha256(payload).hexdigest()

        manifest = {
            "format": ARCHIVE_FORMAT,
            "version": ARCHIVE_VERSION,
            "exported_at": timezone.now().isoformat(),
            "patient_count": patient_count,
            "files": file_hashes,
        }
        archive.writestr(
            "manifest.json",
            json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"),
        )
    output.seek(0)
    return output


def _read_archive(upload):
    if upload.size > MAX_UPLOAD_BYTES:
        raise BackupArchiveError("The ZIP file exceeds the 100 MB upload limit.")
    try:
        archive = zipfile.ZipFile(upload)
    except (zipfile.BadZipFile, OSError) as exc:
        raise BackupArchiveError("Choose a valid CAMS HMS backup ZIP file.") from exc

    with archive:
        infos = archive.infolist()
        if sum(info.file_size for info in infos) > MAX_ARCHIVE_BYTES:
            raise BackupArchiveError("The uncompressed backup exceeds the 500 MB safety limit.")
        if len(infos) > MAX_PATIENT_FILES + 1:
            raise BackupArchiveError("The backup contains too many files.")
        names = {info.filename for info in infos}
        if len(names) != len(infos):
            raise BackupArchiveError("The ZIP contains duplicate file names.")
        if "manifest.json" not in names:
            raise BackupArchiveError("This ZIP is missing its backup manifest.")

        try:
            manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BackupArchiveError("The backup manifest is not valid JSON.") from exc
        if not isinstance(manifest, dict):
            raise BackupArchiveError("The backup manifest is malformed.")
        if manifest.get("format") != ARCHIVE_FORMAT or manifest.get("version") != ARCHIVE_VERSION:
            raise BackupArchiveError("This backup format or version is not supported.")

        patient_paths = sorted(
            name for name in names
            if name.startswith("patients/") and name.endswith(".json")
        )
        if not isinstance(manifest.get("patient_count"), int) or len(patient_paths) != manifest.get("patient_count"):
            raise BackupArchiveError("The patient count does not match the backup manifest.")
        if len(patient_paths) > MAX_PATIENT_FILES:
            raise BackupArchiveError("The backup contains too many patient records.")

        expected_hashes = manifest.get("files")
        if not isinstance(expected_hashes, dict) or set(expected_hashes) != set(patient_paths):
            raise BackupArchiveError("The backup file list does not match its manifest.")
        records = []
        for path in patient_paths:
            if ".." in path.split("/") or path.count("/") != 1:
                raise BackupArchiveError("The backup contains an unsafe file path.")
            payload = archive.read(path)
            if hashlib.sha256(payload).hexdigest() != expected_hashes.get(path):
                raise BackupArchiveError(f"Backup integrity check failed for {path}.")
            try:
                record = json.loads(payload.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise BackupArchiveError(f"{path} is not valid UTF-8 JSON.") from exc
            if record.get("schema") != "cams-hms.patient" or record.get("version") != ARCHIVE_VERSION:
                raise BackupArchiveError(f"{path} uses an unsupported patient record format.")
            records.append(record)
        return records


def _clean_model_values(model, values, *, excluded=()):
    if not isinstance(values, dict):
        raise BackupArchiveError("A record is missing its data object.")
    cleaned = {}
    allowed_fields = {
        field.name: field
        for field in model._meta.concrete_fields
        if not field.primary_key and field.name not in excluded
    }
    unknown = set(values) - set(allowed_fields)
    if unknown:
        raise BackupArchiveError(f"Unknown {model.__name__} fields: {', '.join(sorted(unknown))}.")
    for name, value in values.items():
        try:
            cleaned[name] = allowed_fields[name].to_python(value)
        except (TypeError, ValueError) as exc:
            raise BackupArchiveError(f"Invalid value for {model.__name__}.{name}.") from exc
    return cleaned


def _photo_bytes(photo):
    if photo is None:
        return None
    if not isinstance(photo, dict):
        raise BackupArchiveError("A patient photo entry is malformed.")
    mime_type = photo.get("mime_type")
    if mime_type not in ALLOWED_PHOTO_TYPES:
        raise BackupArchiveError("A patient photo has an unsupported image type.")
    encoded = photo.get("data_base64", "")
    if not isinstance(encoded, str) or len(encoded) > ((MAX_PHOTO_BYTES + 2) // 3) * 4:
        raise BackupArchiveError("A patient photo exceeds the 20 MB limit.")
    try:
        image = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise BackupArchiveError("A patient photo is not valid base64 data.") from exc
    if len(image) > MAX_PHOTO_BYTES:
        raise BackupArchiveError("A patient photo exceeds the 20 MB limit.")
    return image


def _restore_photo(patient, photo, photo_payload, saved_files):
    if not photo_payload:
        return
    extension = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/gif": ".gif",
    }[photo["mime_type"]]
    patient.photo.save(
        f"patient_{patient.mr_number or patient.pk}{extension}",
        ContentFile(photo_payload),
        save=False,
    )
    saved_files.append(patient.photo.name)


def _get_or_create_doctor(data):
    if not isinstance(data, dict):
        raise BackupArchiveError("An appointment is missing its doctor details.")
    values = _clean_model_values(
        Doctor,
        {key: data[key] for key in ("first_name", "last_name", "specialty", "phone", "email") if key in data},
    )
    if not values.get("first_name") or not values.get("last_name"):
        raise BackupArchiveError("An appointment contains incomplete doctor details.")
    doctor = Doctor.objects.filter(**values).first()
    if doctor:
        return doctor
    source_id = data.get("source_doctor_id")
    if isinstance(source_id, int) and source_id > 0 and not Doctor.objects.filter(pk=source_id).exists():
        return Doctor.objects.create(pk=source_id, **values)
    return Doctor.objects.create(**values)


def import_patient_backup(upload):
    """Validate and import new patients, skipping MR numbers already present."""
    records = _read_archive(upload)
    created_count = 0
    skipped_count = 0
    saved_files = []
    try:
        with transaction.atomic():
            for record in records:
                source_id = record.get("source_patient_id")
                if not isinstance(source_id, int) or source_id < 1:
                    raise BackupArchiveError("A patient record has an invalid source ID.")
                values = _clean_model_values(Patient, record.get("patient"), excluded=("photo",))
                mr_number = values.get("mr_number")
                if mr_number and Patient.objects.filter(mr_number=mr_number).exists():
                    skipped_count += 1
                    continue
                if Patient.objects.filter(pk=source_id).exists():
                    # This also makes repeat imports of legacy patients without an MR number safe.
                    skipped_count += 1
                    continue

                image = _photo_bytes(record.get("photo"))
                patient = Patient(pk=source_id, **values)
                try:
                    patient.full_clean(exclude=("photo",), validate_unique=False)
                except Exception as exc:
                    raise BackupArchiveError(f"Patient record {source_id} failed validation: {exc}") from exc
                original_created = values.get("created_at")
                original_updated = values.get("updated_at")
                patient.save()
                _restore_photo(patient, record.get("photo"), image, saved_files)
                if patient.photo:
                    patient.save(update_fields=("photo",))
                timestamp_updates = {}
                if original_created:
                    timestamp_updates["created_at"] = original_created
                if original_updated:
                    timestamp_updates["updated_at"] = original_updated
                if timestamp_updates:
                    Patient.objects.filter(pk=patient.pk).update(**timestamp_updates)

                appointments = record.get("appointments", [])
                if not isinstance(appointments, list):
                    raise BackupArchiveError(f"Patient record {source_id} has an invalid appointments list.")
                for appointment_record in appointments:
                    doctor = _get_or_create_doctor(appointment_record.get("doctor"))
                    appointment_values = _clean_model_values(
                        Appointment,
                        appointment_record.get("appointment"),
                        excluded=("patient", "doctor"),
                    )
                    original_appointment_created = appointment_values.get("created_at")
                    appointment = Appointment.objects.create(
                        patient=patient,
                        doctor=doctor,
                        **appointment_values,
                    )
                    if original_appointment_created:
                        Appointment.objects.filter(pk=appointment.pk).update(
                            created_at=original_appointment_created
                        )
                created_count += 1

            sequence_sql = connection.ops.sequence_reset_sql(no_style(), [Patient, Doctor, Appointment])
            with connection.cursor() as cursor:
                for statement in sequence_sql:
                    cursor.execute(statement)
    except Exception:
        for name in saved_files:
            default_storage.delete(name)
        raise
    return {"created": created_count, "skipped": skipped_count}
