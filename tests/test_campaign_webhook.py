"""Campaign webhook handler — signature verification and the GET handshake.

The signature check is what stops anyone who knows our Function URL from
injecting fake leads. If this test goes green on an unsigned body, we have a
security bug.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from unittest.mock import patch

import pytest

from agents import campaign


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("META_APP_SECRET", "shh-secret")
    monkeypatch.setenv("META_WEBHOOK_VERIFY_TOKEN", "verify-me")


def _signed_event(payload: dict) -> dict:
    body = json.dumps(payload)
    sig = hmac.new(b"shh-secret", body.encode(), hashlib.sha256).hexdigest()
    return {
        "requestContext": {"http": {"method": "POST"}},
        "headers": {"x-hub-signature-256": f"sha256={sig}"},
        "body": body,
    }


def test_get_handshake_returns_challenge_when_token_matches():
    event = {
        "requestContext": {"http": {"method": "GET"}},
        "queryStringParameters": {
            "hub.mode": "subscribe",
            "hub.verify_token": "verify-me",
            "hub.challenge": "hello-123",
        },
    }
    resp = campaign.webhook_handler(event, None)
    assert resp["statusCode"] == 200
    assert resp["body"] == "hello-123"


def test_get_handshake_rejects_wrong_token():
    event = {
        "requestContext": {"http": {"method": "GET"}},
        "queryStringParameters": {
            "hub.mode": "subscribe",
            "hub.verify_token": "nope",
            "hub.challenge": "x",
        },
    }
    resp = campaign.webhook_handler(event, None)
    assert resp["statusCode"] == 403


def test_post_rejects_missing_signature():
    event = {
        "requestContext": {"http": {"method": "POST"}},
        "headers": {},
        "body": json.dumps({"entry": []}),
    }
    resp = campaign.webhook_handler(event, None)
    assert resp["statusCode"] == 403


def test_post_rejects_tampered_body():
    event = _signed_event({"entry": []})
    event["body"] = json.dumps({"entry": [{"extra": "mutated"}]})  # sig no longer matches
    resp = campaign.webhook_handler(event, None)
    assert resp["statusCode"] == 403


def test_post_accepts_valid_signature_and_processes_leads():
    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "leadgen_id": "LID123",
                            "form_id": "FORM1",
                            "created_time": "2026-04-12T12:00:00+0000",
                        }
                    }
                ]
            }
        ]
    }
    event = _signed_event(payload)

    # _persist_lead talks to DB + Meta; stub it out.
    with patch.object(campaign, "_persist_lead", return_value="uuid-here"):
        resp = campaign.webhook_handler(event, None)

    assert resp["statusCode"] == 200
    assert json.loads(resp["body"])["persisted"] == 1


def test_post_method_other_than_get_or_post_rejected():
    event = {"requestContext": {"http": {"method": "DELETE"}}, "headers": {}, "body": ""}
    resp = campaign.webhook_handler(event, None)
    assert resp["statusCode"] == 405


def test_extract_fields_flattens_meta_field_data():
    lead_json = {
        "field_data": [
            {"name": "full_name", "values": ["Jane Doe"]},
            {"name": "email", "values": ["jane@example.com"]},
            {"name": "qualifying_0", "values": ["Yes"]},
        ]
    }
    out = campaign._extract_fields(lead_json)
    assert out == {"full_name": "Jane Doe", "email": "jane@example.com", "qualifying_0": "Yes"}
