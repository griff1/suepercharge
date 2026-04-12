"""Storage abstraction + client stubs behave correctly when no keys are set."""
from __future__ import annotations

import pytest

import storage
from clients import arcads_client, ideogram_client, meta_client, slack_client


@pytest.fixture
def local_storage(tmp_path, monkeypatch):
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    monkeypatch.setenv("S3_BUCKET", "test-bucket")
    monkeypatch.setenv("LOCAL_STORAGE_ROOT", str(tmp_path))
    return tmp_path


def test_storage_put_get_roundtrip(local_storage):
    storage.put_text("foo/bar.txt", "hello")
    assert storage.get_bytes("foo/bar.txt") == b"hello"


def test_storage_put_bytes_roundtrip(local_storage):
    storage.put_bytes("img.png", b"\x89PNG")
    assert storage.get_bytes("img.png") == b"\x89PNG"


def test_storage_presign_local_returns_file_url(local_storage):
    storage.put_text("raw/x.txt", "x")
    url = storage.presign("raw/x.txt")
    assert url.startswith("file://")


def test_storage_describe_local(local_storage):
    assert storage.describe().startswith("local:")


def test_ideogram_stub_returns_png_without_key(monkeypatch):
    monkeypatch.delenv("IDEOGRAM_API_KEY", raising=False)
    out = ideogram_client.generate("anything")
    # PNG signature
    assert out[:8] == b"\x89PNG\r\n\x1a\n"


def test_arcads_stub_roundtrip_without_key(monkeypatch):
    monkeypatch.delenv("ARCADS_API_KEY", raising=False)
    job_id = arcads_client.submit_video(script="test script")
    assert job_id.startswith("stub-job-")
    job = arcads_client.get_video(job_id)
    assert job.status == "completed"
    assert job.video_url
    assert arcads_client.download_video(job.video_url) == b"\x00"


def test_slack_stub_auto_approves(tmp_path, monkeypatch):
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.setenv("LOCAL_APPROVAL_DIR", str(tmp_path))
    ts = slack_client.post_creative_for_approval(
        headline="h", primary_text="p", cta="LEARN_MORE",
        image_url=None, video_url=None,
        case_title="t", case_url="u",
    )
    assert ts.startswith("local-")
    # File persisted
    assert (tmp_path / f"{ts}.json").exists()
    rx = slack_client.get_human_reactions(ts)
    assert rx.approve_users and not rx.reject_users


def test_slack_stub_manual_mode_waits_for_sidecar(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_STUB_SLACK", "manual")
    monkeypatch.setenv("LOCAL_APPROVAL_DIR", str(tmp_path))
    ts = slack_client.post_creative_for_approval(
        headline="h", primary_text="p", cta="LEARN_MORE",
        image_url=None, video_url=None, case_title="t", case_url="u",
    )
    # No sidecar → still pending.
    rx = slack_client.get_human_reactions(ts)
    assert not rx.approve_users and not rx.reject_users

    # Drop an approve sidecar.
    (tmp_path / f"{ts}.reaction").write_text("approve")
    rx = slack_client.get_human_reactions(ts)
    assert rx.approve_users and not rx.reject_users


def test_slack_stub_reject_mode(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCAL_STUB_SLACK", "reject")
    monkeypatch.setenv("LOCAL_APPROVAL_DIR", str(tmp_path))
    ts = slack_client.post_creative_for_approval(
        headline="h", primary_text="p", cta="LEARN_MORE",
        image_url=None, video_url=None, case_title="t", case_url="u",
    )
    rx = slack_client.get_human_reactions(ts)
    assert not rx.approve_users and rx.reject_users == ["local-stub"]


def test_meta_stub_deploy_returns_local_ids(monkeypatch):
    monkeypatch.delenv("META_ACCESS_TOKEN", raising=False)
    form_id = meta_client.create_lead_form(
        name="f", privacy_policy_url="https://x", intro_headline="h",
        intro_description="d",
        fields=meta_client.default_fields_for_case("Did you buy X?"),
        consent_text="ok",
    )
    assert form_id.startswith("local-form-")
    assets = meta_client.CreativeAssets(
        headline="h", primary_text="p", cta_type="LEARN_MORE",
        image_bytes=b"png", video_bytes=None,
    )
    result = meta_client.deploy_lead_campaign(
        campaign_name="test", daily_budget_cents=5000,
        lead_form_id=form_id, creative_assets=assets,
        destination_url="https://x",
    )
    for attr in ("campaign_id", "adset_id", "ad_id", "creative_id"):
        assert getattr(result, attr).startswith("local-")
    # set_campaign_status shouldn't raise.
    meta_client.set_campaign_status(result.campaign_id, "PAUSED")


def test_meta_stub_fetch_lead_returns_default_shape(monkeypatch):
    monkeypatch.delenv("META_ACCESS_TOKEN", raising=False)
    data = meta_client.fetch_lead("local-lead-x")
    assert data["id"] == "local-lead-x"
    assert any(f["name"] == "email" for f in data["field_data"])


def test_meta_stub_fetch_lead_reads_sidecar(tmp_path, monkeypatch):
    monkeypatch.delenv("META_ACCESS_TOKEN", raising=False)
    monkeypatch.setenv("LOCAL_LEADS_DIR", str(tmp_path))
    (tmp_path / "local-lead-seeded.json").write_text(
        '{"id":"local-lead-seeded","created_time":"2026-04-12T00:00:00+0000",'
        '"field_data":[{"name":"email","values":["from-sidecar@x.com"]}]}'
    )
    data = meta_client.fetch_lead("local-lead-seeded")
    assert data["field_data"][0]["values"] == ["from-sidecar@x.com"]
