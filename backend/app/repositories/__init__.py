"""Persistence adapters.

Repositories contain database and filesystem access. They never commit or roll
back request-scoped database transactions; the service layer owns that policy.
"""
