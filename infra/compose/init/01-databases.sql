-- One database and one login role per service. No service may read another
-- service's database; only contracts are shared (see docs/architecture).
-- Role passwords are set by 02-role-passwords.sh from SERVICE_DB_PASSWORD.

CREATE ROLE identity LOGIN;
CREATE ROLE fleet LOGIN;
CREATE ROLE telemetry LOGIN;
CREATE ROLE scheduler LOGIN;
CREATE ROLE notifications LOGIN;
CREATE ROLE entitlements LOGIN;
CREATE ROLE billing LOGIN;
CREATE ROLE planner LOGIN;
CREATE ROLE vision LOGIN;
CREATE ROLE openfga LOGIN;

CREATE DATABASE identity OWNER identity;
CREATE DATABASE fleet OWNER fleet;
CREATE DATABASE telemetry OWNER telemetry;
CREATE DATABASE scheduler OWNER scheduler;
CREATE DATABASE notifications OWNER notifications;
CREATE DATABASE entitlements OWNER entitlements;
CREATE DATABASE billing OWNER billing;
CREATE DATABASE planner OWNER planner;
CREATE DATABASE vision OWNER vision;
CREATE DATABASE openfga OWNER openfga;

-- Prevent cross-database access through the default PUBLIC privilege.
REVOKE CONNECT ON DATABASE identity, fleet, telemetry, scheduler, notifications,
  entitlements, billing, planner, vision, openfga FROM PUBLIC;
GRANT CONNECT ON DATABASE identity TO identity;
GRANT CONNECT ON DATABASE fleet TO fleet;
GRANT CONNECT ON DATABASE telemetry TO telemetry;
GRANT CONNECT ON DATABASE scheduler TO scheduler;
GRANT CONNECT ON DATABASE notifications TO notifications;
GRANT CONNECT ON DATABASE entitlements TO entitlements;
GRANT CONNECT ON DATABASE billing TO billing;
GRANT CONNECT ON DATABASE planner TO planner;
GRANT CONNECT ON DATABASE vision TO vision;
GRANT CONNECT ON DATABASE openfga TO openfga;
