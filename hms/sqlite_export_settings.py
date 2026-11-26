"""Read the former SQLite database only for the one-time data export.

Use with ``manage.py dumpdata --settings=hms.sqlite_export_settings``.
The normal application settings require PostgreSQL.
"""

import os
from pathlib import Path

from .settings import *  # noqa: F403


source_path = Path(os.environ.get('SQLITE_SOURCE_PATH', BASE_DIR / 'db.sqlite3')).resolve()  # noqa: F405
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': f'{source_path.as_uri()}?mode=ro',
        'OPTIONS': {'uri': True},
    }
}
