import json
import time
import logging
from typing import Optional
from fastapi import APIRouter, Request, Header, Query, HTTPException, BackgroundTasks
from fastapi.responses import PlainTextResponse

from app.config import settings
from app.db import SessionLocal
from app.models import Contact, Message, Setting
from app.whatsapp import verify_signature, parse_webhook, parse_webhook_statuses, send_text

logger = logging.getLogger(__name__)
router = APIRouter()


def process_webhook_payload(payload: dict) -> None:
    """Background task to process WhatsApp incoming webhook messages."""
    from app.reply import generate_reply

    try:
        messages = parse_webhook(payload)
        statuses = parse_webhook_statuses(payload)
    except Exception as e:
        logger.exception("Failed to parse webhook payload: %s", e)
        return

    if not messages and not statuses:
        return

    with SessionLocal() as db:
        for status in statuses:
            outbound = db.query(Message).filter(
                Message.wa_msg_id == status["wa_msg_id"],
                Message.direction == "out",
            ).first()
            if not outbound:
                logger.warning("Received WhatsApp status for unknown message id %s", status["wa_msg_id"])
                continue
            outbound.wa_status = status["status"]
            outbound.wa_error = status["error"] or None
            logger.info("WhatsApp message %s status updated to %s", status["wa_msg_id"], status["status"])
        if statuses:
            db.commit()

        for msg in messages:
            sender_number = msg.get("sender_number")
            wa_msg_id = msg.get("wa_msg_id")
            sender_name = msg.get("sender_name") or ""
            msg_type = msg.get("msg_type")
            body = msg.get("text_body", "")

            if not sender_number:
                continue

            try:
                # Deduplicate by wa_msg_id (Meta retries)
                if wa_msg_id:
                    existing = db.query(Message).filter(Message.wa_msg_id == wa_msg_id).first()
                    if existing:
                        logger.info("Message %s already stored; skipping duplicate.", wa_msg_id)
                        continue

                # Auto-create Contact if unknown
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
                elif sender_name and not contact.name:
                    contact.name = sender_name
                    db.commit()

                # Store inbound message
                inbound_body = body if msg_type == "text" else f"[{msg_type} message]"
                inbound_msg = Message(
                    contact_id=contact.id,
                    direction="in",
                    body=inbound_body,
                    wa_msg_id=wa_msg_id,
                )
                db.add(inbound_msg)
                db.commit()

                # Safety: Check STOP command (case-insensitive)
                if msg_type == "text" and body.strip().upper() == "STOP":
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
                            wa_status="accepted",
                        ))
                        db.commit()
                    except Exception as err:
                        logger.exception("Failed to send STOP confirmation: %s", err)
                    continue

                # Check global kill switch
                kill_setting = db.query(Setting).filter(Setting.key == "global_kill_switch").first()
                kill_switch_on = bool(kill_setting and kill_setting.value.lower() == "on")

                # STOP unless bot_enabled is True AND global_kill_switch is off
                if not contact.bot_enabled or kill_switch_on:
                    logger.info(
                        "Skipping auto-reply for %s (bot_enabled=%s, kill_switch=%s)",
                        sender_number,
                        contact.bot_enabled,
                        kill_switch_on,
                    )
                    continue

                # Non-text messages: short polite notification
                if msg_type != "text":
                    try:
                        reply_id = send_text(
                            contact.wa_number,
                            "Sorry, I can only read text messages right now!",
                        )
                        db.add(Message(
                            contact_id=contact.id,
                            direction="out",
                            body="Sorry, I can only read text messages right now!",
                            wa_msg_id=reply_id,
                            wa_status="accepted",
                        ))
                        db.commit()
                    except Exception as err:
                        logger.exception("Failed to send non-text notification: %s", err)
                    continue

                # Wait REPLY_DELAY_SECONDS
                if settings.REPLY_DELAY_SECONDS > 0:
                    time.sleep(settings.REPLY_DELAY_SECONDS)

                # Re-check state after delay in case kill-switch was toggled
                db.refresh(contact)
                kill_setting = db.query(Setting).filter(Setting.key == "global_kill_switch").first()
                if (kill_setting and kill_setting.value.lower() == "on") or not contact.bot_enabled:
                    logger.info("State changed during reply delay; aborting reply.")
                    continue

                # Generate reply via reply.py
                reply_text, gif_url, sticker_url, image_url = generate_reply(contact=contact, incoming_text=body, db=db)
                if reply_text or gif_url or sticker_url or image_url:
                    out_msg_id = send_text(
                        contact.wa_number,
                        reply_text or "",
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
                        wa_status="delivered" if settings.WHATSAPP_PROVIDER == "bridge" else "accepted",
                    )
                    db.add(outbound_msg)
                    db.commit()

            except Exception as e:
                logger.exception("Error processing message from %s: %s", sender_number, e)
                db.rollback()


@router.get("/webhook")
def verify_webhook(
    hub_mode: Optional[str] = Query(None, alias="hub.mode"),
    hub_verify_token: Optional[str] = Query(None, alias="hub.verify_token"),
    hub_challenge: Optional[str] = Query(None, alias="hub.challenge"),
):
    """
    Handle Meta Webhook verification handshake.
    If hub.mode == 'subscribe' and hub.verify_token matches, return hub.challenge as plain text.
    """
    if hub_mode == "subscribe" and hub_verify_token == settings.WA_VERIFY_TOKEN:
        if hub_challenge is not None:
            return PlainTextResponse(content=hub_challenge, status_code=200)
    raise HTTPException(status_code=403, detail="Verification failed")


@router.post("/webhook")
async def handle_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_hub_signature_256: Optional[str] = Header(None, alias="X-Hub-Signature-256"),
):
    """
    Receive WhatsApp webhook notifications.
    Validates HMAC signature, returns 200 immediately, processes in background.
    """
    raw_body = await request.body()
    if not verify_signature(raw_body, x_hub_signature_256):
        raise HTTPException(status_code=403, detail="Invalid signature")

    try:
        payload = json.loads(raw_body.decode("utf-8")) if raw_body else {}
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    background_tasks.add_task(process_webhook_payload, payload)
    return {"status": "ok"}
