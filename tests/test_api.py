import pytest
from unittest.mock import patch
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.db import Base
from app.models import Setting, Contact
import app.db as app_db
import app.routes.api as api_route
from app.main import app


@pytest.fixture(autouse=True)
def setup_api_db(monkeypatch, tmp_path):
    db_file = tmp_path / "api_test.db"
    test_engine = create_engine(f"sqlite:///{db_file}", connect_args={"check_same_thread": False})
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)

    Base.metadata.drop_all(bind=test_engine)
    Base.metadata.create_all(bind=test_engine)

    with TestingSessionLocal() as session:
        session.add(Setting(key="global_kill_switch", value="off"))
        session.commit()

    monkeypatch.setattr(app_db, "engine", test_engine)
    monkeypatch.setattr(app_db, "SessionLocal", TestingSessionLocal)
    monkeypatch.setattr(settings, "DASHBOARD_TOKEN", "secret-test-token")

    def override_get_db():
        with TestingSessionLocal() as session:
            yield session

    app.dependency_overrides[app_db.get_db] = override_get_db
    yield TestingSessionLocal
    app.dependency_overrides.clear()


@pytest.fixture
def client():
    return TestClient(app)


def test_health_check_unauthenticated(client):
    res = client.get("/health")
    assert res.status_code == 200
    assert res.json()["status"] == "ok"


def test_auth_rejection_missing_or_bad_token(client):
    # No header
    res1 = client.get("/api/contacts")
    assert res1.status_code == 401

    # Wrong token
    res2 = client.get("/api/contacts", headers={"Authorization": "Bearer wrong-token"})
    assert res2.status_code == 401


def test_contacts_crud_lifecycle(client):
    headers = {"Authorization": "Bearer secret-test-token"}

    # 1. Create contact
    res_post = client.post(
        "/api/contacts",
        headers=headers,
        json={"wa_number": "+91 98765-43210", "name": "Best Friend", "persona_notes": "Speaks Hinglish"},
    )
    assert res_post.status_code == 200
    contact_data = res_post.json()
    assert contact_data["wa_number"] == "919876543210"
    assert contact_data["name"] == "Best Friend"
    assert contact_data["bot_enabled"] is False
    contact_id = contact_data["id"]

    # 2. List contacts
    res_list = client.get("/api/contacts", headers=headers)
    assert res_list.status_code == 200
    assert len(res_list.json()) == 1

    # 3. Patch contact
    res_patch = client.patch(
        f"/api/contacts/{contact_id}",
        headers=headers,
        json={"bot_enabled": True, "name": "Best Friend Forever"},
    )
    assert res_patch.status_code == 200
    assert res_patch.json()["bot_enabled"] is True
    assert res_patch.json()["name"] == "Best Friend Forever"

    # 4. Delete contact
    res_del = client.delete(f"/api/contacts/{contact_id}", headers=headers)
    assert res_del.status_code == 200
    assert res_del.json()["status"] == "deleted"


def test_schedules_cron_validation(client, setup_api_db):
    headers = {"Authorization": "Bearer secret-test-token"}

    with setup_api_db() as db:
        c = Contact(wa_number="919111111111", name="Friend")
        db.add(c)
        db.commit()
        db.refresh(c)
        c_id = c.id

    # Invalid cron expression -> 422
    bad_res = client.post(
        "/api/schedules",
        headers=headers,
        json={"contact_id": c_id, "cron": "invalid-cron-string", "body": "Hello!"},
    )
    assert bad_res.status_code == 422
    assert "Invalid cron expression" in bad_res.json()["detail"]

    # Valid cron expression -> 200
    with patch("app.routes.api.add_or_update_schedule_job") as mock_add_job:
        good_res = client.post(
            "/api/schedules",
            headers=headers,
            json={"contact_id": c_id, "cron": "0 9 * * *", "body": "Good morning!"},
        )
        assert good_res.status_code == 200
        mock_add_job.assert_called_once()


def test_kill_switch_api(client):
    headers = {"Authorization": "Bearer secret-test-token"}

    # Initial state
    res_get = client.get("/api/kill-switch", headers=headers)
    assert res_get.status_code == 200
    assert res_get.json()["kill_switch"] == "off"

    # Toggle to on
    res_post_on = client.post("/api/kill-switch", headers=headers, json={"on": True})
    assert res_post_on.status_code == 200
    assert res_post_on.json()["kill_switch"] == "on"

    # Toggle back to off
    res_post_off = client.post("/api/kill-switch", headers=headers, json={"on": False})
    assert res_post_off.status_code == 200
    assert res_post_off.json()["kill_switch"] == "off"
