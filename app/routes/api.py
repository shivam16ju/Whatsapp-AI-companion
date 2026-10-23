import logging
from datetime import datetime, timezone
from typing import Optional, List
from fastapi import APIRouter, Depends, HTTPException, Header, UploadFile, File, Form, status
from pydantic import BaseModel, Field
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.models import Contact, Message, Memory, Schedule, Setting
from app.whatsapp import normalize_number, can_send_text, send_text, send_template
from app.rag import parse_chat_export, chunk_messages, upsert_chunks
from app.scheduler import add_or_update_schedule_job, remove_schedule_job

logger = logging.getLogger(__name__)
router = APIRouter()


# ---------------------------------------------------------
# Security Dependency
# ---------------------------------------------------------
def require_dashboard_auth(authorization: Optional[str] = Header(None)) -> bool:
    """Validate Bearer token against DASHBOARD_TOKEN."""
    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )
    parts = authorization.split(" ")
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Authorization header format. Expected 'Bearer <token>'",
        )
    token = parts[1]
    if token != settings.DASHBOARD_TOKEN:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid dashboard token",
        )
    return True


# ---------------------------------------------------------
# Pydantic Schemas
# ---------------------------------------------------------
class ContactCreate(BaseModel):
    wa_number: str = Field(..., description="WhatsApp phone number with country code")
    name: Optional[str] = None
    bot_enabled: bool = True
    persona_notes: Optional[str] = None


class ContactUpdate(BaseModel):
    name: Optional[str] = None
    bot_enabled: Optional[bool] = None
    persona_notes: Optional[str] = None


class MemoryCreate(BaseModel):
    fact: str = Field(..., description="Fact or memory to remember about this contact")


class ScheduleCreate(BaseModel):
    contact_id: int
    cron: str = Field(..., description="Standard 5-field cron expression")
    body: str
    template_name: str = "hello_world"
    template_lang: str = "en_US"
    enabled: bool = True


class ScheduleUpdate(BaseModel):
    cron: Optional[str] = None
    body: Optional[str] = None
    template_name: Optional[str] = None
    template_lang: Optional[str] = None
    enabled: Optional[bool] = None


class SendMessageRequest(BaseModel):
    contact_id: int
    body: str


class KillSwitchRequest(BaseModel):
    on: bool


# ---------------------------------------------------------
# Health Check (Unauthenticated for monitoring)
# ---------------------------------------------------------
@router.get("/health")
def health_check():
    return {
        "status": "ok",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "owner": settings.OWNER_NAME,
    }


# ---------------------------------------------------------
# Contacts Endpoints
# ---------------------------------------------------------
@router.get("/api/contacts", dependencies=[Depends(require_dashboard_auth)])
def list_contacts(db: Session = Depends(get_db)):
    contacts = db.query(Contact).all()
    results = []
    for c in contacts:
        last_msg = (
            db.query(Message)
            .filter(Message.contact_id == c.id)
            .order_by(Message.created_at.desc(), Message.id.desc())
            .first()
        )
        msg_count = db.query(Message).filter(Message.contact_id == c.id).count()
        last_active = last_msg.created_at if (last_msg and last_msg.created_at) else c.created_at
        is_unsaved = bool(not c.name or c.name == c.wa_number or c.name.strip() == "")
        results.append({
            "id": c.id,
            "wa_number": c.wa_number,
            "name": c.name or c.wa_number,
            "is_unsaved": is_unsaved,
            "bot_enabled": c.bot_enabled,
            "persona_notes": c.persona_notes,
            "created_at": c.created_at.isoformat() if c.created_at else None,
            "message_count": msg_count,
            "last_message": last_msg.body if last_msg else None,
            "last_message_direction": last_msg.direction if last_msg else None,
            "last_active": last_active.isoformat() if last_active else None,
        })
    # Sort: most recently active contact FIRST
    results.sort(key=lambda x: x["last_active"] or "", reverse=True)
    return results


@router.post("/api/contacts", dependencies=[Depends(require_dashboard_auth)])
def create_contact(req: ContactCreate, db: Session = Depends(get_db)):
    clean_num = normalize_number(req.wa_number)
    if not clean_num:
        raise HTTPException(status_code=400, detail="Invalid phone number")

    existing = db.query(Contact).filter(Contact.wa_number == clean_num).first()
    if existing:
        raise HTTPException(status_code=400, detail="Contact with this number already exists")

    contact = Contact(
        wa_number=clean_num,
        name=req.name or clean_num,
        bot_enabled=req.bot_enabled,
        persona_notes=req.persona_notes,
    )
    db.add(contact)
    db.commit()
    db.refresh(contact)
    return {
        "id": contact.id,
        "wa_number": contact.wa_number,
        "name": contact.name,
        "bot_enabled": contact.bot_enabled,
        "persona_notes": contact.persona_notes,
        "created_at": contact.created_at.isoformat() if contact.created_at else None,
    }


@router.patch("/api/contacts/{contact_id}", dependencies=[Depends(require_dashboard_auth)])
def update_contact(contact_id: int, req: ContactUpdate, db: Session = Depends(get_db)):
    contact = db.query(Contact).filter(Contact.id == contact_id).first()
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")

    if req.name is not None:
        contact.name = req.name
    if req.bot_enabled is not None:
        contact.bot_enabled = req.bot_enabled
    if req.persona_notes is not None:
        contact.persona_notes = req.persona_notes

    db.commit()
    db.refresh(contact)
    return {
        "id": contact.id,
        "wa_number": contact.wa_number,
        "name": contact.name,
        "bot_enabled": contact.bot_enabled,
        "persona_notes": contact.persona_notes,
    }


@router.delete("/api/contacts/{contact_id}", dependencies=[Depends(require_dashboard_auth)])
def delete_contact(contact_id: int, db: Session = Depends(get_db)):
    contact = db.query(Contact).filter(Contact.id == contact_id).first()
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")

    # Remove any scheduled jobs for this contact
    schedules = db.query(Schedule).filter(Schedule.contact_id == contact_id).all()
    for s in schedules:
        remove_schedule_job(s.id)

    db.delete(contact)
    db.commit()
    return {"status": "deleted", "id": contact_id}


# ---------------------------------------------------------
# Messages Endpoints
# ---------------------------------------------------------
@router.get("/api/contacts/{contact_id}/messages", dependencies=[Depends(require_dashboard_auth)])
def get_contact_messages(contact_id: int, db: Session = Depends(get_db)):
    contact = db.query(Contact).filter(Contact.id == contact_id).first()
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")

    # Fetch last 50 messages ordered chronologically
    messages = (
        db.query(Message)
        .filter(Message.contact_id == contact_id)
        .order_by(Message.created_at.desc(), Message.id.desc())
        .limit(50)
        .all()
    )
    messages.reverse()

    return [
        {
            "id": m.id,
            "direction": m.direction,
            "body": m.body,
            "wa_msg_id": m.wa_msg_id,
            "wa_status": m.wa_status,
            "wa_error": m.wa_error,
            "created_at": m.created_at.isoformat() if m.created_at else None,
        }
        for m in messages
    ]


@router.get("/api/messages", dependencies=[Depends(require_dashboard_auth)])
def get_all_recent_messages(limit: int = 50, db: Session = Depends(get_db)):
    """Fetch recent messages across all contacts for live dashboard monitoring."""
    messages = (
        db.query(Message, Contact)
        .join(Contact, Message.contact_id == Contact.id)
        .order_by(Message.created_at.desc(), Message.id.desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "id": m.id,
            "contact_id": m.contact_id,
            "contact_name": c.name or c.wa_number,
            "contact_number": c.wa_number,
            "direction": m.direction,
            "body": m.body,
            "wa_status": m.wa_status,
            "wa_error": m.wa_error,
            "created_at": m.created_at.isoformat() if m.created_at else None,
        }
        for m, c in messages
    ]


# ---------------------------------------------------------
# Memories Endpoints
# ---------------------------------------------------------
@router.get("/api/contacts/{contact_id}/memories", dependencies=[Depends(require_dashboard_auth)])
def list_memories(contact_id: int, db: Session = Depends(get_db)):
    contact = db.query(Contact).filter(Contact.id == contact_id).first()
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")

    memories = (
        db.query(Memory)
        .filter(Memory.contact_id == contact_id)
        .order_by(Memory.created_at.desc())
        .all()
    )
    return [
        {
            "id": m.id,
            "contact_id": m.contact_id,
            "fact": m.fact,
            "created_at": m.created_at.isoformat() if m.created_at else None,
        }
        for m in memories
    ]


@router.post("/api/contacts/{contact_id}/memories", dependencies=[Depends(require_dashboard_auth)])
def create_memory(contact_id: int, req: MemoryCreate, db: Session = Depends(get_db)):
    contact = db.query(Contact).filter(Contact.id == contact_id).first()
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")

    memory = Memory(contact_id=contact_id, fact=req.fact.strip())
    db.add(memory)
    db.commit()
    db.refresh(memory)
    return {
        "id": memory.id,
        "contact_id": memory.contact_id,
        "fact": memory.fact,
        "created_at": memory.created_at.isoformat() if memory.created_at else None,
    }


@router.delete("/api/contacts/{contact_id}/memories/{memory_id}", dependencies=[Depends(require_dashboard_auth)])
@router.delete("/api/memories/{memory_id}", dependencies=[Depends(require_dashboard_auth)])
def delete_memory(memory_id: int, contact_id: Optional[int] = None, db: Session = Depends(get_db)):
    query = db.query(Memory).filter(Memory.id == memory_id)
    if contact_id:
        query = query.filter(Memory.contact_id == contact_id)
    memory = query.first()
    if not memory:
        raise HTTPException(status_code=404, detail="Memory not found")

    db.delete(memory)
    db.commit()
    return {"status": "deleted", "id": memory_id}


# ---------------------------------------------------------
# Schedules Endpoints
# ---------------------------------------------------------
@router.get("/api/schedules", dependencies=[Depends(require_dashboard_auth)])
def list_schedules(db: Session = Depends(get_db)):
    schedules = db.query(Schedule).order_by(Schedule.id.desc()).all()
    results = []
    for s in schedules:
        contact = db.query(Contact).filter(Contact.id == s.contact_id).first()
        results.append({
            "id": s.id,
            "contact_id": s.contact_id,
            "contact_name": contact.name if contact else "Unknown",
            "contact_number": contact.wa_number if contact else "",
            "cron": s.cron,
            "body": s.body,
            "template_name": s.template_name,
            "template_lang": s.template_lang,
            "enabled": s.enabled,
            "created_at": s.created_at.isoformat() if s.created_at else None,
        })
    return results


@router.post("/api/schedules", dependencies=[Depends(require_dashboard_auth)])
def create_schedule(req: ScheduleCreate, db: Session = Depends(get_db)):
    contact = db.query(Contact).filter(Contact.id == req.contact_id).first()
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")

    # Validate cron with CronTrigger.from_crontab; return 422 on bad cron
    try:
        CronTrigger.from_crontab(req.cron)
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"Invalid cron expression: {e}")

    schedule = Schedule(
        contact_id=req.contact_id,
        cron=req.cron.strip(),
        body=req.body.strip(),
        template_name=req.template_name.strip(),
        template_lang=req.template_lang.strip(),
        enabled=req.enabled,
    )
    db.add(schedule)
    db.commit()
    db.refresh(schedule)

    if schedule.enabled:
        try:
            add_or_update_schedule_job(schedule)
        except Exception as err:
            logger.error("Failed to register job for new schedule: %s", err)

    return {
        "id": schedule.id,
        "contact_id": schedule.contact_id,
        "cron": schedule.cron,
        "body": schedule.body,
        "template_name": schedule.template_name,
        "template_lang": schedule.template_lang,
        "enabled": schedule.enabled,
    }


@router.patch("/api/schedules/{schedule_id}", dependencies=[Depends(require_dashboard_auth)])
def update_schedule(schedule_id: int, req: ScheduleUpdate, db: Session = Depends(get_db)):
    schedule = db.query(Schedule).filter(Schedule.id == schedule_id).first()
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found")

    if req.cron is not None:
        try:
            CronTrigger.from_crontab(req.cron)
        except Exception as e:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"Invalid cron expression: {e}")
        schedule.cron = req.cron.strip()

    if req.body is not None:
        schedule.body = req.body.strip()
    if req.template_name is not None:
        schedule.template_name = req.template_name.strip()
    if req.template_lang is not None:
        schedule.template_lang = req.template_lang.strip()
    if req.enabled is not None:
        schedule.enabled = req.enabled

    db.commit()
    db.refresh(schedule)

    if schedule.enabled:
        add_or_update_schedule_job(schedule)
    else:
        remove_schedule_job(schedule.id)

    return {
        "id": schedule.id,
        "contact_id": schedule.contact_id,
        "cron": schedule.cron,
        "body": schedule.body,
        "template_name": schedule.template_name,
        "template_lang": schedule.template_lang,
        "enabled": schedule.enabled,
    }


@router.delete("/api/schedules/{schedule_id}", dependencies=[Depends(require_dashboard_auth)])
def delete_schedule(schedule_id: int, db: Session = Depends(get_db)):
    schedule = db.query(Schedule).filter(Schedule.id == schedule_id).first()
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found")

    remove_schedule_job(schedule.id)
    db.delete(schedule)
    db.commit()
    return {"status": "deleted", "id": schedule_id}


# ---------------------------------------------------------
# Chat History Upload & Ingestion
# ---------------------------------------------------------
@router.post("/api/chat-history", dependencies=[Depends(require_dashboard_auth)])
async def upload_chat_history(
    file: UploadFile = File(...),
    sender_name: Optional[str] = Form(None),
):
    try:
        content_bytes = await file.read()
        text_content = content_bytes.decode("utf-8", errors="replace")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not read uploaded file: {e}")

    messages = parse_chat_export(text_content)
    if not messages:
        raise HTTPException(status_code=400, detail="No valid messages parsed from file.")

    effective_sender = (sender_name or settings.OWNER_NAME or "Owner").strip()
    chunks = chunk_messages(messages, owner_name=effective_sender)

    chunks_upserted = 0
    if chunks:
        try:
            chunks_upserted = upsert_chunks(chunks)
        except Exception as e:
            logger.exception("Pinecone upsert failed: %s", e)
            raise HTTPException(status_code=500, detail=f"Failed to upsert chunks into Pinecone: {e}")

    return {
        "messages_parsed": len(messages),
        "chunks_upserted": chunks_upserted,
        "sender_name": effective_sender,
    }


# ---------------------------------------------------------
# Manual Send Message (24-Hour Rule Compliant)
# ---------------------------------------------------------
@router.post("/api/send", dependencies=[Depends(require_dashboard_auth)])
def manual_send_message(req: SendMessageRequest, db: Session = Depends(get_db)):
    contact = db.query(Contact).filter(Contact.id == req.contact_id).first()
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")

    is_within_24h = can_send_text(contact.id, db)

    try:
        if is_within_24h:
            wa_msg_id = send_text(contact.wa_number, req.body)
            channel = "text"
            sent_body = req.body
        else:
            wa_msg_id = send_template(contact.wa_number, name="hello_world", lang="en_US")
            channel = "template"
            sent_body = f"[Template: hello_world (outside 24h window)] {req.body}"

        outbound = Message(
            contact_id=contact.id,
            direction="out",
            body=sent_body,
            wa_msg_id=wa_msg_id,
            wa_status="accepted",
        )
        db.add(outbound)
        db.commit()

        return {
            "status": "sent",
            "channel": channel,
            "wa_msg_id": wa_msg_id,
            "note": "Sent via template because contact is outside 24h window" if not is_within_24h else None,
        }
    except Exception as err:
        logger.exception("Manual send failed: %s", err)
        raise HTTPException(status_code=502, detail=f"Failed to deliver message via WhatsApp: {err}")


# ---------------------------------------------------------
# Global Kill Switch
# ---------------------------------------------------------
@router.get("/api/kill-switch", dependencies=[Depends(require_dashboard_auth)])
def get_kill_switch(db: Session = Depends(get_db)):
    setting = db.query(Setting).filter(Setting.key == "global_kill_switch").first()
    state = setting.value if setting else "off"
    return {"kill_switch": state}


@router.post("/api/kill-switch", dependencies=[Depends(require_dashboard_auth)])
def set_kill_switch(req: KillSwitchRequest, db: Session = Depends(get_db)):
    setting = db.query(Setting).filter(Setting.key == "global_kill_switch").first()
    new_val = "on" if req.on else "off"
    if not setting:
        setting = Setting(key="global_kill_switch", value=new_val)
        db.add(setting)
    else:
        setting.value = new_val
    db.commit()
    logger.warning("Global kill-switch toggled to: %s", new_val)
    return {"kill_switch": new_val}
