import json
import hmac
import hashlib
from unittest.mock import patch, MagicMock
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.db import Base
from app.models import Contact, Message, Setting
import app.db as app_db
import app.routes.webhook as webhook_route
from app.main import app


@pytest.fixture(autouse=True)
def setup_test_db(monkeypatch, tmp_path):
    # Use isolated SQLite file for tests
    db_file = tmp_path / "test.db"
    test_db_url = f"sqlite:///{db_file}"
    test_engine = create_engine(test_db_url, connect_args={"check_same_thread": False})
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)

    Base.metadata.drop_all(bind=test_engine)
    Base.metadata.create_all(bind=test_engine)

    # Initialize setting
    with TestingSessionLocal() as session:
        session.add(Setting(key="global_kill_switch", value="off"))
        session.commit()

    monkeypatch.setattr(app_db, "engine", test_engine)
    monkeypatch.setattr(app_db, "SessionLocal", TestingSessionLocal)
    monkeypatch.setattr(webhook_route, "SessionLocal", TestingSessionLocal)

    # Configure dummy secrets for tests
    monkeypatch.setattr(settings, "WA_VERIFY_TOKEN", "test-verify-token")
    monkeypatch.setattr(settings, "WA_APP_SECRET", "test-secret-key")
    monkeypatch.setattr(settings, "REPLY_DELAY_SECONDS", 0)

    yield TestingSessionLocal


@pytest.fixture
def client():
    return TestClient(app)


def make_hub_signature(body: bytes, secret: str = "test-secret-key") -> str:
    sig = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={sig}"


def test_webhook_get_handshake_success(client):
    response = client.get("/webhook", params={
        "hub.mode": "subscribe",
        "hub.verify_token": "test-verify-token",
        "hub.challenge": "1158201444",
    })
    assert response.status_code == 200
    assert response.text == "1158201444"


def test_webhook_get_handshake_bad_token(client):
    response = client.get("/webhook", params={
        "hub.mode": "subscribe",
        "hub.verify_token": "wrong-token",
        "hub.challenge": "1158201444",
    })
    assert response.status_code == 403


def test_webhook_post_bad_signature(client):
    payload = {"entry": []}
    body = json.dumps(payload).encode("utf-8")
    response = client.post(
        "/webhook",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": "sha256=invalidhexsignature00000000000000000",
        },
    )
    assert response.status_code == 403


def test_webhook_post_status_update_ignored(client, setup_test_db):
    payload = {
        "entry": [{
            "changes": [{
                "value": {
                    "messaging_product": "whatsapp",
                    "metadata": {"display_phone_number": "123", "phone_number_id": "456"},
                    "statuses": [{
                        "id": "wamid.HBgL...",
                        "status": "delivered",
                        "timestamp": "1710260000",
                        "recipient_id": "919876543210"
                    }]
                }
            }]
        }]
    }
    body = json.dumps(payload).encode("utf-8")
    sig = make_hub_signature(body)

    with patch("app.whatsapp.send_text") as mock_send:
        response = client.post(
            "/webhook",
            content=body,
            headers={"Content-Type": "application/json", "X-Hub-Signature-256": sig},
        )
        assert response.status_code == 200
        mock_send.assert_not_called()

    # Check no message was created
    with setup_test_db() as db:
        assert db.query(Message).count() == 0


def test_webhook_post_text_message_and_deduplication(client, setup_test_db):
    sender_number = "919876543210"
    msg_id = "wamid.test.msg.123"

    payload = {
        "entry": [{
            "changes": [{
                "value": {
                    "messaging_product": "whatsapp",
                    "contacts": [{"wa_id": sender_number, "profile": {"name": "Shivam"}}],
                    "messages": [{
                        "from": sender_number,
                        "id": msg_id,
                        "timestamp": "1710260000",
                        "type": "text",
                        "text": {"body": "Hey bro, kaisa hai?"}
                    }]
                }
            }]
        }]
    }
    body = json.dumps(payload).encode("utf-8")
    sig = make_hub_signature(body)

    # Mock Graph API, LLM, and RAG
    with patch("app.routes.webhook.send_text", return_value="wamid.out.1") as mock_send, \
         patch("app.reply.get_llm") as mock_llm_factory, \
         patch("app.rag.query_chunks", return_value=["Badiya hu bhai"]), \
         patch.object(settings, "AUTO_ENABLE_NEW_CONTACTS", False):

        mock_llm_instance = MagicMock()
        mock_llm_instance.generate.return_value = "Sab badiya bro, tu bata!"
        mock_llm_factory.return_value = mock_llm_instance

        # 1. First delivery - contact does not exist yet (bot_enabled defaults to False)
        resp1 = client.post(
            "/webhook",
            content=body,
            headers={"Content-Type": "application/json", "X-Hub-Signature-256": sig},
        )
        assert resp1.status_code == 200

        with setup_test_db() as db:
            contact = db.query(Contact).filter(Contact.wa_number == sender_number).first()
            assert contact is not None
            assert contact.bot_enabled is False
            # Inbound message stored
            inbound = db.query(Message).filter(Message.wa_msg_id == msg_id).first()
            assert inbound is not None
            assert inbound.direction == "in"

            # Enable bot for this contact to test auto-reply
            contact.bot_enabled = True
            db.commit()

        # 2. Duplicate retry with same wa_msg_id - must be skipped!
        resp2 = client.post(
            "/webhook",
            content=body,
            headers={"Content-Type": "application/json", "X-Hub-Signature-256": sig},
        )
        assert resp2.status_code == 200
        mock_send.assert_not_called()

        # 3. New message with different wa_msg_id when bot_enabled is True
        new_msg_id = "wamid.test.msg.456"
        payload["entry"][0]["changes"][0]["value"]["messages"][0]["id"] = new_msg_id
        payload["entry"][0]["changes"][0]["value"]["messages"][0]["text"]["body"] = "Dinner kiya?"
        body2 = json.dumps(payload).encode("utf-8")
        sig2 = make_hub_signature(body2)

        resp3 = client.post(
            "/webhook",
            content=body2,
            headers={"Content-Type": "application/json", "X-Hub-Signature-256": sig2},
        )
        assert resp3.status_code == 200

        # Now auto-reply should have been generated and sent
        assert mock_send.call_count == 1
        mock_send.assert_called_with(sender_number, "Sab badiya bro, tu bata!", gif_url=None, sticker_url=None, image_url=None)

        with setup_test_db() as db:
            outbound = db.query(Message).filter(Message.direction == "out").first()
            assert outbound is not None
            assert outbound.body == "Sab badiya bro, tu bata!"
            assert outbound.wa_msg_id == "wamid.out.1"


def test_webhook_post_stop_command_disables_bot(client, setup_test_db):
    sender_number = "919999999999"

    # Pre-populate contact with bot_enabled=True
    with setup_test_db() as db:
        c = Contact(wa_number=sender_number, name="Tester", bot_enabled=True)
        db.add(c)
        db.commit()

    payload = {
        "entry": [{
            "changes": [{
                "value": {
                    "messaging_product": "whatsapp",
                    "messages": [{
                        "from": sender_number,
                        "id": "wamid.stop.123",
                        "timestamp": "1710260000",
                        "type": "text",
                        "text": {"body": "stop"}
                    }]
                }
            }]
        }]
    }
    body = json.dumps(payload).encode("utf-8")
    sig = make_hub_signature(body)

    with patch("app.routes.webhook.send_text", return_value="wamid.stop.confirm") as mock_send:
        resp = client.post(
            "/webhook",
            content=body,
            headers={"Content-Type": "application/json", "X-Hub-Signature-256": sig},
        )
        assert resp.status_code == 200
        mock_send.assert_called_once()
        assert "Auto-replies disabled" in mock_send.call_args[0][1]

    with setup_test_db() as db:
        c = db.query(Contact).filter(Contact.wa_number == sender_number).first()
        assert c.bot_enabled is False
