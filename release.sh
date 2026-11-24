#!/bin/sh
set -e

python manage.py migrate --noinput

python manage.py shell -c '
import os
from django.contrib.auth import get_user_model

User = get_user_model()
username = os.environ["DJANGO_SUPERUSER_USERNAME"]
email = os.environ["DJANGO_SUPERUSER_EMAIL"]
password = os.environ["DJANGO_SUPERUSER_PASSWORD"]

user, created = User.objects.get_or_create(
    username=username,
    defaults={"email": email},
)
user.email = email
user.is_staff = True
user.is_superuser = True
user.set_password(password)
user.save()
print("Superuser created" if created else "Superuser password reset")
'
