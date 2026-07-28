"""Validated settings boundary for modules that initialize runtime resources."""

from __future__ import annotations

from .bootstrap import validate_startup
from .config import settings as configured_settings

settings = validate_startup(configured_settings)
