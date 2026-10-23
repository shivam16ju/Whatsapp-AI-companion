from datetime import datetime, timezone
from sqlalchemy import Column, Integer, String, Boolean, Text, DateTime, ForeignKey, Index
from sqlalchemy.orm import relationship
from app.db import Base


def utc_now():
    return datetime.now(timezone.utc)


class Contact(Base):
    __tablename__ = "contacts"

    id = Column(Integer, primary_key=True, index=True)
    wa_number = Column(String(32), unique=True, nullable=False, index=True)
    name = Column(String(255), nullable=True)
    bot_enabled = Column(Boolean, default=True, nullable=False)
    persona_notes = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)

    messages = relationship("Message", back_populates="contact", cascade="all, delete-orphan")
    memories = relationship("Memory", back_populates="contact", cascade="all, delete-orphan")
    schedules = relationship("Schedule", back_populates="contact", cascade="all, delete-orphan")


class Message(Base):
    __tablename__ = "messages"

    id = Column(Integer, primary_key=True, index=True)
    contact_id = Column(Integer, ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False, index=True)
    direction = Column(String(8), nullable=False)  # 'in' or 'out'
    body = Column(Text, nullable=False)
    wa_msg_id = Column(String(255), unique=True, nullable=True, index=True)
    # "accepted" means the Graph API accepted the send; status webhooks advance it.
    wa_status = Column(String(16), nullable=True)
    wa_error = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)

    contact = relationship("Contact", back_populates="messages")


class Memory(Base):
    __tablename__ = "memories"

    id = Column(Integer, primary_key=True, index=True)
    contact_id = Column(Integer, ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False, index=True)
    fact = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)

    contact = relationship("Contact", back_populates="memories")


class Schedule(Base):
    __tablename__ = "schedules"

    id = Column(Integer, primary_key=True, index=True)
    contact_id = Column(Integer, ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False, index=True)
    cron = Column(String(64), nullable=False)  # standard 5-field cron
    body = Column(Text, nullable=False)
    template_name = Column(String(255), default="hello_world", nullable=False)
    template_lang = Column(String(16), default="en_US", nullable=False)
    enabled = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)

    contact = relationship("Contact", back_populates="schedules")


class Setting(Base):
    __tablename__ = "settings"

    key = Column(String(128), primary_key=True)
    value = Column(Text, nullable=False)
