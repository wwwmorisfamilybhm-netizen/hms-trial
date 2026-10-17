release: python manage.py migrate --noinput
web: python manage.py collectstatic --noinput && gunicorn hms.wsgi:application --bind 0.0.0.0:${PORT:-8000}
