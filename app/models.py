import uuid
from datetime import datetime

from sqlalchemy import Boolean, Column, String, DateTime, Text

from .database import Base


def gen_id() -> str:
    return uuid.uuid4().hex


class Setting(Base):
    __tablename__ = "settings"

    key = Column(String, primary_key=True)
    value = Column(Text, nullable=True)


class User(Base):
    __tablename__ = "users"

    id = Column(String, primary_key=True, default=gen_id)
    username = Column(String, unique=True, nullable=False)
    password_hash = Column(String, nullable=False)
    is_admin = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    last_login = Column(DateTime, nullable=True)
    last_active = Column(DateTime, nullable=True)
    # Free-text, admin-only (e.g. the person's real name) - purely a memory
    # aid shown in their activity log, never surfaced to the user themselves.
    note = Column(Text, nullable=True)


class Notification(Base):
    """An admin-composed message for one specific user, shown as a one-time
    popup the next time that user's browser polls for it - the row is
    deleted once they dismiss it, there's no persistent inbox."""
    __tablename__ = "notifications"

    id = Column(String, primary_key=True, default=gen_id)
    username = Column(String, nullable=False, index=True)
    message = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
