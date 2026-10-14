"""Database adapter: SQLAlchemy metadata, repositories and Alembic migrations.

The engine comes from ``mowdeck_service_kit.db.create_engine``. Every account-scoped query
filters by the account id of the verified principal.
"""
