"""MYRAA application-wide user identities (shared workspace, not separate tenants)."""
from datetime import datetime, timezone
from sqlalchemy import Column, Integer, String, Text, Boolean, DateTime
from database import Base


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class WorkspaceUser(Base):
    __tablename__ = "myraa_workspace_users"
    id = Column(Integer, primary_key=True)
    login_id = Column(String(80), nullable=False, unique=True, index=True)
    email = Column(String(255), nullable=False, unique=True, index=True)
    display_name = Column(String(160), nullable=False)
    business_name = Column(String(180), nullable=False, default="")
    phone = Column(String(32), nullable=False, default="")
    address = Column(Text, nullable=False, default="")
    photo_name = Column(String(90), nullable=True)
    password_hash = Column(String(350), nullable=False)
    verified = Column(Boolean, nullable=False, default=False)
    approved = Column(Boolean, nullable=False, default=False)
    active = Column(Boolean, nullable=False, default=True)
    role = Column(String(16), nullable=False, default="viewer")  # viewer / manager
    token_version = Column(String(48), nullable=False)
    otp_digest = Column(String(128), nullable=True)
    otp_nonce = Column(String(48), nullable=True)
    otp_type = Column(String(16), nullable=True)
    otp_expires = Column(DateTime, nullable=True)
    otp_last_sent = Column(DateTime, nullable=True)
    otp_attempts = Column(Integer, nullable=False, default=0)
    failed_logins = Column(Integer, nullable=False, default=0)
    locked_until = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=utcnow)
