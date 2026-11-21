# 🏥 Cams HMS — Hospital Management System

A hospital management system built from scratch with **Django** and **SQLite** (swap to PostgreSQL later if needed).

## Features (v1)

- **Dashboard** — today's schedule, key numbers, recent patients
- **Patients** — registration, search, full record page with visit history
- **Appointments** — book visits, day-by-day calendar view, double-booking protection
- **Admin panel** — manage everything at `/admin/`

## Getting started

```bash
cd hms
python3.14 -m venv .venv          # only needed once
.venv/bin/pip install -r requirements.txt
.venv/bin/python manage.py migrate
.venv/bin/python manage.py runserver
```

Open:

- App: http://127.0.0.1:8000/
- Admin: http://127.0.0.1:8000/admin/ — username `admin`

## Common commands

| Task | Command |
|---|---|
| Start server | `.venv/bin/python manage.py runserver` |
| Make migrations after editing models | `.venv/bin/python manage.py makemigrations` |
| Apply migrations | `.venv/bin/python manage.py migrate` |
| Create admin user | `.venv/bin/python manage.py createsuperuser` |
| Seed synthetic OPD demo visits | `.venv/bin/python manage.py seed_opd_demo` |
| Django shell (query data) | `.venv/bin/python manage.py shell` |
| **Refresh CSS/JS after editing assets** | `.venv/bin/python manage.py collectstatic --noinput` |

### ⚠️ Static files: run `collectstatic` after CSS/JS changes

The app serves CSS and JavaScript through WhiteNoise from `staticfiles/` (the collected
copy), not straight from `static/`. If you edit `static/css/*.css` or `static/js/*.js`
and the browser still shows the old styling or an unstyled page, the collected copy is
stale — run:

```bash
.venv/bin/python manage.py collectstatic --noinput --clear
```

`collectstatic` is run automatically on every deploy (the `Procfile` does it), and with
`DJANGO_DEBUG=True` the dev server serves the source files directly — but with
`DJANGO_DEBUG=False` only the collected copy is served.

## Deploy a test app on Domain India

The project is configured to use SQLite locally and PostgreSQL when `DATABASE_URL` is set.
For a test deployment on the Domain India App Platform:

1. Push this project to a private GitHub repository and connect the repository in the App Platform dashboard.
2. Create or attach the managed PostgreSQL database.
3. Set these app environment variables in the dashboard:
	- `DJANGO_DEBUG=False`
	- `DJANGO_SECRET_KEY` to a newly generated secret
	- `DJANGO_ALLOWED_HOSTS` to the assigned app hostname, without `https://`
	- `CSRF_TRUSTED_ORIGINS` to the assigned HTTPS origin, such as `https://your-app.example`
	- `DATABASE_URL` to the managed database URL (the platform may set this automatically)
4. Deploy the selected branch. The `Procfile` applies migrations, collects static files, and starts Gunicorn.
5. Test using fake patient data only. Do not put real patient records into a prototype deployment.

Generate a secret locally with:

```bash
python3 -c 'from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())'
```

Never commit the generated secret or database credentials. The app's initial admin account can be created with
`python manage.py createsuperuser` using the platform's Django command console, if available.

### OPD Assistant access and demo records

OPD access uses the existing Django authentication backend and each user's assigned designation. Create or
select a staff account in Django Admin, then set its profile designation to **OPD Assistant**. There is no
hard-coded demo password. The seed command creates synthetic patients and workflow records marked `DEMO`, plus
doctor directory entries with unusable passwords; it does not create sign-in credentials. Run it repeatedly
without creating duplicate demo encounters. Never enter real patient information into a demo environment.

### Service Staff and Pharmacy access

Assign **Service Staff** or **Pharmacy** to a staff account from the Admin Console. Service Staff can process the laboratory, imaging, ECG, physiotherapy, counselling, and procedure queues. Radiology uses X-Ray/USG modality tabs; physiotherapy and counselling use multi-session plans with attendance and progress notes. Pharmacy staff use the separate prescription dispensing queue; OPD Assistants and Service Staff do not have pharmacy queue or dispensing access. Apply the database migrations after deploying the designation changes.

## Project structure

```
hms/                  # project settings, urls
core/                 # dashboard
patients/             # Patient model, views, forms, admin
appointments/         # Doctor + Appointment models, views, forms, admin
templates/            # HTML pages
static/css/           # stylesheet
```

### Pharmacy and service operations

Pharmacy now supports a medicine catalogue, batch stock receipts, reservations, cashier-linked itemized bills, partial handovers, expiry checks and a stock ledger. Service Staff use assigned departments, order-linked reports and doctor review. See [operations setup and workflow](docs/operations.md) for first-use steps, migration compatibility and deployment storage requirements.


## Pharmacy imports and product photos

Open **Pharmacy → Import medicines / stock** to download a CSV or Excel template, upload a file, map its columns, and review editable rows. Save each review page before moving to another page. Committing validates every selected row and records stock receipts together. The result links to product-photo review for each medicine.

Supported files are UTF-8 CSV, Excel .xlsx (the first worksheet), PDF, JPEG and PNG. Limits are 10 MB, 10 PDF pages and 500 rows. Scanned invoices use local English Tesseract OCR. Install the Python dependencies from requirements.txt and install Tesseract with `brew install tesseract` on macOS or `apt-get install tesseract-ocr` on Debian/Ubuntu. Tesseract must be on the application process's PATH. OCR values need pharmacist review. PDF extraction runs during the upload request, so production worker/proxy timeouts should allow up to 300 seconds for the maximum-size scan.

For product photos, **Search images** opens a pre-filled Google Images search. Copy an image and paste or drop it into the medicine screen, upload a file, or paste a public HTTPS image/product-page link. Product-page previews use Open Graph or Twitter image metadata. Review the preview and choose **Use this photo**. This workflow works without an API key. Private previews expire after one hour, with expired files cleaned up on later preview requests.

For optional in-app online product-photo suggestions, set the server environment variable `BRAVE_SEARCH_API_KEY` to a Brave Search API subscription token with image-search access, then restart the application. Keys stay on the server. Search uses medicine details with India and English preferences. Staff approve a candidate or upload their own JPEG, PNG or WebP photo up to 5 MB. Approved photos appear in the catalogue, preparation screen and issue detail.

Import source files use the private report storage directory and authenticated owner/admin download routes. Include this directory, medicine media files and the database in deployment backups.
