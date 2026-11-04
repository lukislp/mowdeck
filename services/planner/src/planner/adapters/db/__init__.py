# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""Database adapter: SQLAlchemy tables, the store and Alembic migrations.

The engine comes from ``mowdeck_service_kit.db.create_engine``. Every account-scoped query
filters by the account id of the verified principal.
"""
