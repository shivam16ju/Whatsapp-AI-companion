import logging
import time
from datetime import datetime, timezone, timedelta
from typing import Optional, List
import httpx
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db, SessionLocal
from app.models import Contact, Message, Setting
from app.whatsapp import send_text, normalize_number
from app.reply import generate_reply

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/bridge", tags=["bridge"])


class BridgeWebhookPayload(BaseModel):
    sender: str
    name: Optional[str] = ""
    text: str
    message_id: str
    jid: Optional[str] = None
    from_me: Optional[bool] = False


def process_bridge_message(
    sender_raw: str,
    sender_name: str,
    body: str,
    wa_msg_id: str,
    jid: Optional[str] = None,
    from_me: bool = False,
) -> None:
    """Process incoming or outgoing WhatsApp Web bridge message asynchronously in background."""
    with SessionLocal() as db:
        try:
            sender_number = normalize_number(sender_raw)
            if not sender_number:
                logger.warning("Empty sender number in bridge payload; skipping.")
                return

            # Deduplication
            if wa_msg_id:
                existing = db.query(Message).filter(Message.wa_msg_id == wa_msg_id).first()
                if existing:
                    logger.info("Bridge message %s already stored; skipping duplicate.", wa_msg_id)
                    return

            # Find or create Contact
            contact = db.query(Contact).filter(Contact.wa_number == sender_number).first()
            if not contact:
                contact = Contact(
                    wa_number=sender_number,
                    name=sender_name or sender_number,
                    bot_enabled=settings.AUTO_ENABLE_NEW_CONTACTS,
                )
                db.add(contact)
                db.commit()
                db.refresh(contact)
            elif sender_name and (not contact.name or contact.name == contact.wa_number):
                contact.name = sender_name
                db.commit()

            # If user sent this message manually from WhatsApp on phone:
            if from_me:
                outbound_msg = Message(
                    contact_id=contact.id,
                    direction="out",
                    body=body,
                    wa_msg_id=wa_msg_id,
                    wa_status="delivered",
                )
                db.add(outbound_msg)
                db.commit()
                logger.info("Recorded user's manual outbound message to %s: %s", sender_number, body)
                return

            # Store inbound message
            inbound_msg = Message(
                contact_id=contact.id,
                direction="in",
                body=body,
                wa_msg_id=wa_msg_id,
            )
            db.add(inbound_msg)
            db.commit()

            # Safety: Check STOP command
            if body.strip().upper() == "STOP":
                contact.bot_enabled = False
                db.commit()
                logger.info("Contact %s opted out via STOP. Bot disabled.", sender_number)
                try:
                    reply_id = send_text(
                        contact.wa_number,
                        "Auto-replies disabled. You can text anytime to reach me directly.",
                    )
                    db.add(Message(
                        contact_id=contact.id,
                        direction="out",
                        body="Auto-replies disabled. You can text anytime to reach me directly.",
                        wa_msg_id=reply_id,
                    ))
                    db.commit()
                except Exception as err:
                    logger.exception("Failed to send STOP confirmation: %s", err)
                return

            # Check global kill switch
            kill_setting = db.query(Setting).filter(Setting.key == "global_kill_switch").first()
            kill_switch_on = bool(kill_setting and kill_setting.value.lower() == "on")

            if not contact.bot_enabled or kill_switch_on:
                logger.info(
                    "Skipping auto-reply for %s (bot_enabled=%s, kill_switch=%s)",
                    sender_number,
                    contact.bot_enabled,
                    kill_switch_on,
                )
                return

            # Skip auto-reply for non-text placeholder
            if body == "[non-text message]":
                return

            # Echo protection: Do not reply if inbound message matches our last outbound reply
            last_outbound = (
                db.query(Message)
                .filter(Message.contact_id == contact.id, Message.direction == "out")
                .order_by(Message.id.desc())
                .first()
            )
            if last_outbound and last_outbound.body.strip() == body.strip():
                logger.info("Inbound body matches last outbound message; skipping echo.")
                return

            # Reply delay
            if settings.REPLY_DELAY_SECONDS > 0:
                time.sleep(settings.REPLY_DELAY_SECONDS)

            # Re-check state after delay
            db.refresh(contact)
            kill_setting = db.query(Setting).filter(Setting.key == "global_kill_switch").first()
            if (kill_setting and kill_setting.value.lower() == "on") or not contact.bot_enabled:
                logger.info("State changed during reply delay; aborting reply.")
                return

            # Generate reply
            reply_text, gif_url, sticker_url, image_url = generate_reply(contact=contact, incoming_text=body, db=db)
            if reply_text or gif_url or sticker_url or image_url:
                out_msg_id = send_text(
                    contact.wa_number,
                    reply_text or "",
                    provider="bridge",
                    jid=jid,
                    gif_url=gif_url,
                    sticker_url=sticker_url,
                    image_url=image_url,
                )
                if image_url:
                    body_label = f"[Photo] {reply_text}" if reply_text else "[Photo]"
                elif sticker_url:
                    body_label = f"[Sticker] {reply_text}" if reply_text else "[Sticker]"
                elif gif_url:
                    body_label = f"[GIF] {reply_text}" if reply_text else "[GIF]"
                else:
                    body_label = reply_text or ""

                outbound_msg = Message(
                    contact_id=contact.id,
                    direction="out",
                    body=body_label,
                    wa_msg_id=out_msg_id,
                    wa_status="delivered",
                )
                db.add(outbound_msg)
                db.commit()
                logger.info(
                    "Sent bridge auto-reply to %s: %s (Photo: %s, GIF: %s, Sticker: %s)",
                    sender_number,
                    reply_text,
                    image_url,
                    gif_url,
                    sticker_url,
                )

        except Exception as e:
            logger.exception("Error processing bridge message from %s: %s", sender_raw, e)
            db.rollback()


@router.post("/webhook")
def receive_bridge_webhook(
    payload: BridgeWebhookPayload,
    background_tasks: BackgroundTasks,
):
    """Receive incoming WhatsApp Web message from local Node.js bridge."""
    background_tasks.add_task(
        process_bridge_message,
        payload.sender,
        payload.name or "",
        payload.text,
        payload.message_id,
        payload.jid,
        payload.from_me or False,
    )
    return {"status": "accepted"}


class SyncContactItem(BaseModel):
    number: str
    name: Optional[str] = ""
    jid: Optional[str] = None


class SyncContactsPayload(BaseModel):
    contacts: List[SyncContactItem]


@router.post("/sync-contacts")
def sync_contacts_from_bridge(payload: SyncContactsPayload, db: Session = Depends(get_db)):
    """Bulk sync contacts or chats detected by WhatsApp Web bridge into SQLite."""
    added = 0
    updated = 0
    for item in payload.contacts:
        clean_num = normalize_number(item.number)
        if not clean_num or len(clean_num) < 7:
            continue

        contact = db.query(Contact).filter(Contact.wa_number == clean_num).first()
        if not contact:
            contact = Contact(
                wa_number=clean_num,
                name=item.name or clean_num,
                bot_enabled=settings.AUTO_ENABLE_NEW_CONTACTS,
            )
            db.add(contact)
            added += 1
        else:
            if item.name and (not contact.name or contact.name == contact.wa_number):
                contact.name = item.name
                updated += 1
    db.commit()
    logger.info("Synced contacts from bridge: %d added, %d updated", added, updated)
    return {"status": "ok", "added": added, "updated": updated}


def recover_unreplied_messages() -> int:
    """
    Scan for incoming messages from active contacts that never received an outbound response
    (e.g., received while internet was down, bridge disconnected, or offline sync).
    Generates and delivers replies to bring all conversations up to date.
    Returns the number of recovered conversations replied to.
    """
    recovered_count = 0
    with SessionLocal() as db:
        try:
            # 1. Global kill switch check
            kill_setting = db.query(Setting).filter(Setting.key == "global_kill_switch").first()
            if kill_setting and kill_setting.value.lower() == "on":
                logger.info("[Auto-Recover] Global kill switch is ON; skipping recovery.")
                return 0

            # 2. Check if bridge is connected
            try:
                resp = httpx.get(f"{settings.BRIDGE_URL}/status", timeout=4.0)
                if resp.status_code != 200 or resp.json().get("status") != "connected":
                    logger.debug("[Auto-Recover] Bridge not connected; skipping recovery.")
                    return 0
            except Exception:
                logger.debug("[Auto-Recover] Bridge unreachable; skipping recovery.")
                return 0

            # 3. Find contacts with bot enabled
            contacts = db.query(Contact).filter(Contact.bot_enabled == True).all()
            now = datetime.now(timezone.utc)

            for contact in contacts:
                # Find the most recent message for this contact
                last_msg = (
                    db.query(Message)
                    .filter(Message.contact_id == contact.id)
                    .order_by(Message.id.desc())
                    .first()
                )

                # If the latest message is an inbound message from the contact, it needs a reply!
                if not last_msg or last_msg.direction != "in":
                    continue

                # Ignore non-text or STOP
                body = (last_msg.body or "").strip()
                if not body or body == "[non-text message]" or body.upper() == "STOP":
                    continue

                # Check age: only recover messages within the last 24 hours to avoid ancient history
                if last_msg.created_at:
                    msg_time = last_msg.created_at
                    if msg_time.tzinfo is None:
                        msg_time = msg_time.replace(tzinfo=timezone.utc)
                    if (now - msg_time) > timedelta(hours=24):
                        continue

                # Echo protection
                last_outbound = (
                    db.query(Message)
                    .filter(Message.contact_id == contact.id, Message.direction == "out")
                    .order_by(Message.id.desc())
                    .first()
                )
                if last_outbound and last_outbound.body.strip() == body:
                    continue

                logger.info(
                    "[Auto-Recover] Found unreplied message from %s (%s): '%s'. Generating catch-up reply...",
                    contact.name, contact.wa_number, body[:40]
                )

                # Generate reply
                reply_text, gif_url, sticker_url, image_url = generate_reply(contact=contact, incoming_text=body, db=db)
                if reply_text or gif_url or sticker_url or image_url:
                    out_msg_id = send_text(
                        contact.wa_number,
                        reply_text or "",
                        provider="bridge",
                        gif_url=gif_url,
                        sticker_url=sticker_url,
                        image_url=image_url,
                    )
                    if image_url:
                        body_label = f"[Photo] {reply_text}" if reply_text else "[Photo]"
                    elif sticker_url:
                        body_label = f"[Sticker] {reply_text}" if reply_text else "[Sticker]"
                    elif gif_url:
                        body_label = f"[GIF] {reply_text}" if reply_text else "[GIF]"
                    else:
                        body_label = reply_text or ""

                    outbound_msg = Message(
                        contact_id=contact.id,
                        direction="out",
                        body=body_label,
                        wa_msg_id=out_msg_id,
                        wa_status="delivered",
                    )
                    db.add(outbound_msg)
                    db.commit()
                    recovered_count += 1
                    logger.info("[Auto-Recover] Sent catch-up reply to %s: '%s'", contact.wa_number, body_label[:50])
                    time.sleep(1.0)

        except Exception as e:
            logger.exception("[Auto-Recover] Error during offline message recovery: %s", e)
            db.rollback()

    return recovered_count


@router.post("/recover-pending")
def trigger_recover_pending(background_tasks: BackgroundTasks):
    """Trigger background recovery of any unreplied left-out messages."""
    background_tasks.add_task(recover_unreplied_messages)
    return {"status": "triggered"}

