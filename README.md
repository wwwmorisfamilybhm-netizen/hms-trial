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

## Project structure

```
hms/                  # project settings, urls
core/                 # dashboard
patients/             # Patient model, views, forms, admin
appointments/         # Doctor + Appointment models, views, forms, admin
templates/            # HTML pages
static/css/           # stylesheet
```
