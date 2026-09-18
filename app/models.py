import uuid
from datetime import datetime

from sqlalchemy import Boolean, Column, String, Float, DateTime, Integer, Text

from .database import Base


def gen_id() -> str:
    return uuid.uuid4().hex


class Download(Base):
    __tablename__ = "downloads"

    id = Column(String, primary_key=True, default=gen_id)
    url = Column(Text, nullable=False)
    source = Column(String, nullable=True)

    mode = Column(String, nullable=False)  # video | video_only | audio
    quality = Column(String, nullable=True)
    container = Column(String, nullable=True)
    subtitle_lang = Column(String, nullable=True)
    premiere_compat = Column(Integer, default=0)
    clip_start = Column(Float, nullable=True)
    clip_end = Column(Float, nullable=True)

    status = Column(String, default="queued", index=True)  # queued, downloading, finished, error, expired, deleted
    progress = Column(Float, default=0.0)
    eta_seconds = Column(Integer, nullable=True)
    # Set when premiere_compat found the downloaded file's codec unsuitable
    # and auto-started a conversion job for it - id of that Conversion row.
    auto_convert_id = Column(String, nullable=True)
    # Whether the anonymous attempt failed and this download only succeeded
    # after retrying with the account cookies configured in admin settings.
    used_cookies = Column(Boolean, default=False)

    title = Column(Text, nullable=True)
    filepath = Column(Text, nullable=True)
    filesize = Column(Integer, nullable=True)
    error_message = Column(Text, nullable=True)

    client_ip = Column(String, nullable=True)
    client_id = Column(String, nullable=True, index=True)
    username = Column(String, nullable=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    finished_at = Column(DateTime, nullable=True)


class Conversion(Base):
    __tablename__ = "conversions"

    id = Column(String, primary_key=True, default=gen_id)
    original_filename = Column(Text, nullable=True)
    input_summary = Column(Text, nullable=True)
    duration_seconds = Column(Float, nullable=True)

    quality = Column(String, default="high")  # high | medium | low
    audio_option = Column(String, default="original")  # aac | original | none
    # True when auto-started by the "compatibility" flow right after a
    # download (see converter.submit_conversion_from_download), as opposed
    # to a user manually hitting "Конвертувати".
    is_auto = Column(Boolean, default=False)

    status = Column(String, default="queued", index=True)  # queued, converting, finished, error, expired
    progress = Column(Float, default=0.0)
    eta_seconds = Column(Integer, nullable=True)

    filepath = Column(Text, nullable=True)
    filesize = Column(Integer, nullable=True)
    error_message = Column(Text, nullable=True)

    client_ip = Column(String, nullable=True)
    client_id = Column(String, nullable=True, index=True)
    username = Column(String, nullable=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    finished_at = Column(DateTime, nullable=True)


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
