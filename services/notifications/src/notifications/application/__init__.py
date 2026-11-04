# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""Application layer of notifications: use cases.

Use cases depend on the domain and on ports (``Protocol`` classes in ``ports``). Adapters
implement the ports and are wired in ``notifications.main``.
"""
