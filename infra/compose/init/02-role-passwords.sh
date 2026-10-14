#!/bin/sh
# Sets the password of every per-service role from SERVICE_DB_PASSWORD.
set -eu
for role in identity fleet telemetry scheduler notifications entitlements billing planner vision openfga; do
  psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d postgres -v role="$role" -v pw="$SERVICE_DB_PASSWORD" <<'SQL'
ALTER ROLE :"role" PASSWORD :'pw';
SQL
done
