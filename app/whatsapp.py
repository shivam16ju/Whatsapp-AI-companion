import re
import hmac
import hashlib
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any, List
import httpx
from sqlalchemy.orm import Session
from app.config import settings
from app.models import Message

logger = logging.getLogger(__name__)


def normalize_number(number: str) -> str:
    """Normalize phone number to digits only (strip device suffix, '+', dashes, etc.)."""
    if not number:
        return ""
    if ":" in str(number):
        number = str(number).split(":")[0]
    return re.sub(r"\D", "", str(number))


def verify_signature(raw_body: bytes, signature_header: Optional[str]) -> bool:
    """Verify X-Hub-Signature-256 header using WA_APP_SECRET."""
    if not settings.WA_APP_SECRET:
        logger.warning("WA_APP_SECRET is not configured; skipping signature check.")
        return True
    if not signature_header:
        logger.error("Missing X-Hub-Signature-256 header.")
        return False

    prefix = "sha256="
    if not signature_header.startswith(prefix):
        logger.error("Malformed X-Hub-Signature-256 header: missing 'sha256=' prefix.")
        return False

    expected_sig = signature_header[len(prefix):]
    computed_sig = hmac.new(
        key=settings.WA_APP_SECRET.encode("utf-8"),
        msg=raw_body,
        digestmod=hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(computed_sig, expected_sig)


def can_send_text(contact_id: int, db: Session) -> bool:
    """
    Check Meta 24-hour customer service window rule:
    Free-form text is allowed only within 24 hours of the contact's last INBOUND message.
    Always allowed for bridge provider.
    """
    if settings.WHATSAPP_PROVIDER == "bridge":
        return True

    last_inbound = (
        db.query(Message)
        .filter(Message.contact_id == contact_id, Message.direction == "in")
        .order_by(Message.created_at.desc())
        .first()
    )
    if not last_inbound or not last_inbound.created_at:
        return False

    now = datetime.now(timezone.utc)
    created_at = last_inbound.created_at
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)

    diff = now - created_at
    return diff <= timedelta(hours=24)


def send_text(
    to: str,
    body: str,
    provider: Optional[str] = None,
    jid: Optional[str] = None,
    gif_url: Optional[str] = None,
    sticker_url: Optional[str] = None,
    image_url: Optional[str] = None,
) -> str:
    """
    Send free-form text, GIF reaction, sticker, or image via WhatsApp Web bridge or Meta Cloud API.
    Returns the message ID.
    """
    clean_to = normalize_number(to)
    target_provider = provider or settings.WHATSAPP_PROVIDER

    if target_provider == "bridge":
        url = f"{settings.BRIDGE_URL}/send"
        payload = {"to": clean_to, "text": body}
        if jid:
            payload["jid"] = jid
        if gif_url:
            payload["gifUrl"] = gif_url
        if sticker_url:
            payload["stickerUrl"] = sticker_url
        if image_url:
            payload["imageUrl"] = image_url
        with httpx.Client(timeout=25.0) as client:
            resp = client.post(url, json=payload)
            if resp.status_code >= 400:
                logger.error("Bridge send_text failed [%d]: %s", resp.status_code, resp.text)
                raise RuntimeError(f"Bridge send_text failed ({resp.status_code}): {resp.text}")
            data = resp.json()
            return data.get("message_id", "")

    url = f"https://graph.facebook.com/{settings.GRAPH_VERSION}/{settings.WA_PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {settings.WA_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": clean_to,
        "type": "text",
        "text": {"body": body},
    }

    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.post(url, json=payload, headers=headers)
            if resp.status_code >= 400:
                logger.warning(
                    "Graph API send_text failed [%d]: %s. Falling back to WhatsApp Web bridge...",
                    resp.status_code,
                    resp.text,
                )
                return send_text(clean_to, body, provider="bridge")

            data = resp.json()
            messages = data.get("messages", [])
            if messages and "id" in messages[0]:
                return messages[0]["id"]
            return ""
    except Exception as exc:
        if "Bridge send_text" in str(exc):
            raise
        logger.warning("Meta Graph API send_text exception (%s). Falling back to WhatsApp Web bridge...", exc)
        return send_text(clean_to, body, provider="bridge")


def send_template(to: str, name: str, lang: str = "en_US") -> str:
    """
    Send pre-approved template message via WhatsApp Cloud API.
    Used outside the 24-hour free-form text window.
    Falls back to WhatsApp Web bridge if Meta Cloud API fails or bridge is provider.
    """
    clean_to = normalize_number(to)
    if settings.WHATSAPP_PROVIDER == "bridge":
        return send_text(clean_to, f"[Template: {name}]", provider="bridge")

    url = f"https://graph.facebook.com/{settings.GRAPH_VERSION}/{settings.WA_PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {settings.WA_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": clean_to,
        "type": "template",
        "template": {
            "name": name,
            "language": {"code": lang},
        },
    }

    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.post(url, json=payload, headers=headers)
            if resp.status_code >= 400:
                logger.warning(
                    "Graph API send_template failed [%d]: %s. Falling back to WhatsApp Web bridge...",
                    resp.status_code,
                    resp.text,
                )
                return send_text(clean_to, f"[Template: {name}]", provider="bridge")

            data = resp.json()
            messages = data.get("messages", [])
            if messages and "id" in messages[0]:
                return messages[0]["id"]
            return ""
    except Exception as exc:
        if "Bridge send_text" in str(exc):
            raise
        logger.warning("Meta Graph API send_template exception (%s). Falling back to bridge...", exc)
        return send_text(clean_to, f"[Template: {name}]", provider="bridge")


def parse_webhook(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Parse WhatsApp Cloud API incoming webhook payload.
    Extracts incoming messages, ignoring delivery/read status updates (value.statuses).
    Returns list of dicts: {wa_msg_id, sender_number, sender_name, msg_type, text_body, timestamp}
    """
    parsed: List[Dict[str, Any]] = []
    entries = data.get("entry", [])
    for entry in entries:
        changes = entry.get("changes", [])
        for change in changes:
            value = change.get("value", {})
            # Ignore status receipts (sent, delivered, read)
            if "statuses" in value and "messages" not in value:
                continue

            # Extract sender names if available in metadata
            contact_names: Dict[str, str] = {}
            for c in value.get("contacts", []):
                wa_id = c.get("wa_id")
                profile_name = c.get("profile", {}).get("name")
                if wa_id and profile_name:
                    contact_names[wa_id] = profile_name

            messages = value.get("messages", [])
            for msg in messages:
                msg_id = msg.get("id")
                sender_wa = msg.get("from")
                msg_type = msg.get("type", "unknown")
                timestamp = msg.get("timestamp")

                text_body = ""
                if msg_type == "text":
                    text_body = msg.get("text", {}).get("body", "")

                sender_name = contact_names.get(sender_wa, "")

                parsed.append({
                    "wa_msg_id": msg_id,
                    "sender_number": normalize_number(sender_wa or ""),
                    "sender_name": sender_name,
                    "msg_type": msg_type,
                    "text_body": text_body,
                    "timestamp": timestamp,
                })
    return parsed


def parse_webhook_statuses(data: Dict[str, Any]) -> List[Dict[str, str]]:
    """Extract outgoing message delivery updates from Meta webhook payloads."""
    parsed: List[Dict[str, str]] = []
    for entry in data.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            for status in value.get("statuses", []):
                message_id = status.get("id")
                state = status.get("status")
                if not message_id or state not in {"sent", "delivered", "read", "failed"}:
                    continue

                error_text = ""
                errors = status.get("errors") or []
                if errors:
                    error = errors[0]
                    details = error.get("error_data", {}).get("details")
                    parts = [error.get("title"), error.get("message"), details]
                    error_text = ": ".join(dict.fromkeys(p.strip() for p in parts if p and p.strip()))
                    if not error_text and error.get("code") is not None:
                        error_text = f"WhatsApp error code {error['code']}"

                parsed.append({"wa_msg_id": message_id, "status": state, "error": error_text})
    return parsed
