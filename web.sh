#!/bin/sh
set -e

python manage.py collectstatic --noinput
exec gunicorn hms.wsgi:application --bind 0.0.0.0:${PORT:-8000}
