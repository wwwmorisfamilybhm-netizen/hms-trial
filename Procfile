release: python manage.py migrate --noinput && python manage.py createsuperuser --noinput --username "$DJANGO_SUPERUSER_USERNAME" --email "$DJANGO_SUPERUSER_EMAIL"
web: python manage.py collectstatic --noinput && gunicorn hms.wsgi:application --bind 0.0.0.0:${PORT:-8000}
