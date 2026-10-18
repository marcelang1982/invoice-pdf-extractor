#!/bin/sh
# Migrate, then serve. Compose already waits for Postgres to be healthy, so a
# failure here is a real migration problem rather than a startup race.
set -e

echo "Running database migrations..."
alembic upgrade head

echo "Starting the web app on :8000"
exec uvicorn invoice_web.main:create_app --factory --host 0.0.0.0 --port 8000
