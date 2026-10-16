"""Declarative base for future read-only mirror models of Laravel tables.

Never call ``Base.metadata.create_all``: Laravel migrations own the schema. Mirror
models should declare only the columns this service reads. Generated models for the
whole schema live in ``limoapi/docs/ai-agent/sqlalchemy_models.py`` for reference.
"""

from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Base class for mirror models (no table creation)."""
