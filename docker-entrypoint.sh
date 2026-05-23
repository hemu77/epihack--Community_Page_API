#!/bin/sh
set -e

DB_PATH="${COMMUNITY_DB_PATH:-community_messages.db}"

if [ ! -f "$DB_PATH" ]; then
  echo "Database not found at $DB_PATH; seeding initial data..."
  python seed_db.py
fi

exec uvicorn main:app --host 0.0.0.0 --port 8000
