#!/bin/sh
set -e

python -c "from db import init_db; init_db()"

exec gunicorn app:app --bind 0.0.0.0:8080 --workers 2
