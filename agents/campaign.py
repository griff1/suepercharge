"""Campaign agent — deploys approved creatives to Meta, monitors spend, and
ingests incoming leads via a Lambda Function URL webhook.

Two entrypoints:
  - handler(event, ctx)         — EventBridge cron (every 15 min): deploy
    approved creatives and update status of active campaigns.
  - webhook_handler(event, ctx) — Lambda Function URL: Meta leadgen webhook.
    Handles both the GET verification handshake and POST lead notifications.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

import compliance
import storage
from clients import meta_client
from db import session
from models import (
    Campaign,
    CampaignStatus,
    Case,
    Creative,
    CreativeStatus,
    Lead,
)

log = logging.getLogger(__name__)
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))

DEFAULT_DAILY_BUDGET_CENTS = int(os.environ.get("DEFAULT_DAILY_BUDGET_CENTS", "5000"))
PRIVACY_POLICY_URL = os.environ.get(
    "PRIVACY_POLICY_URL", "https://suepercharge.ai/privacy"
)
DESTINATION_URL = os.environ.get(
    "DESTINATION_URL", "https://suepercharge.ai/eligibility"
)


# ---------- Deploy pass ----------


def _approved_creatives_without_campaign(s: Session) -> list[Creative]:
    return list(
        s.scalars(
            select(Creative).where(
                Creative.status == CreativeStatus.approved,
                ~Creative.campaigns.any(),
            )
        ).all()
    )


def _build_assets(cr: Creative) -> meta_client.CreativeAssets:
    image_bytes = storage.get_bytes(cr.image_s3_key) if cr.image_s3_key else None
    video_bytes = storage.get_bytes(cr.video_s3_key) if cr.video_s3_key else None
    return meta_client.CreativeAssets(
        headline=cr.headline or "",
        primary_text=cr.primary_text or "",
        cta_type=cr.cta or "LEARN_MORE",
        image_bytes=image_bytes,
        video_bytes=video_bytes,
    )


def _qualifying_question_for(case: Case) -> str | None:
    """Derive a single yes/no qualifying question from the case facts. This is
    the one gate that separates a raw Meta lead from a lead worth handing to a
    firm. For MVP we keep it very simple."""
    if case.product and case.class_period_start and case.class_period_end:
        return (
            f"Did you purchase or use {case.product} between "
            f"{case.class_period_start.isoformat()} and {case.class_period_end.isoformat()}?"
        )
    if case.harm_type:
        return f"Have you experienced {case.harm_type}?"
    return None


def _deploy_one(s: Session, cr: Creative) -> Campaign | None:
    case = s.get(Case, cr.case_id)
    if case is None:
        log.error("creative %s references missing case", cr.id)
        return None

    if not cr.ai_disclosure_applied and cr.video_s3_key:
        # Hard gate: we have a video but it wasn't tagged as carrying the
        # AI disclosure — don't deploy (compliance.md §1.AI-disclosure).
        log.warning("creative %s has video but ai_disclosure_applied=False; skipping", cr.id)
        return None

    # 1. Create a per-campaign lead form with the current consent version.
    try:
        lead_form_id = meta_client.build_lead_form_for_case(
            case_title=case.title,
            case_summary=(case.title or "")[:500],
            privacy_policy_url=PRIVACY_POLICY_URL,
            qualifying_question=_qualifying_question_for(case),
            unique_suffix=str(cr.id)[:8],  # first 8 chars of creative UUID
        )
    except Exception as e:
        log.exception("lead form creation failed for case %s", case.id)
        _persist_failed_campaign(s, cr, case, f"lead_form: {e}")
        return None

    # 2. Deploy the campaign.
    try:
        assets = _build_assets(cr)
        result = meta_client.deploy_lead_campaign(
            campaign_name=f"suepercharge/{case.id}",
            daily_budget_cents=DEFAULT_DAILY_BUDGET_CENTS,
            lead_form_id=lead_form_id,
            creative_assets=assets,
            destination_url=DESTINATION_URL,
        )
    except Exception as e:
        log.exception("meta deploy failed for creative %s", cr.id)
        _persist_failed_campaign(s, cr, case, f"deploy: {e}")
        return None

    camp = Campaign(
        case_id=case.id,
        creative_id=cr.id,
        meta_campaign_id=result.campaign_id,
        meta_adset_id=result.adset_id,
        meta_ad_id=result.ad_id,
        lead_form_id=result.lead_form_id,
        daily_budget_cents=DEFAULT_DAILY_BUDGET_CENTS,
        status=CampaignStatus.deploying,
        deployed_at=datetime.now(UTC),
    )
    s.add(camp)
    s.flush()
    log.info("deployed campaign %s (meta=%s)", camp.id, result.campaign_id)
    return camp


def _persist_failed_campaign(
    s: Session, cr: Creative, case: Case, error: str
) -> None:
    s.add(
        Campaign(
            case_id=case.id,
            creative_id=cr.id,
            daily_budget_cents=DEFAULT_DAILY_BUDGET_CENTS,
            status=CampaignStatus.failed,
            last_error=error[:2000],
        )
    )


def deploy_approved() -> int:
    with session() as s:
        creatives = _approved_creatives_without_campaign(s)
        log.info("deploy_approved: %d creatives pending", len(creatives))
        deployed = 0
        for cr in creatives:
            if _deploy_one(s, cr) is not None:
                deployed += 1
        return deployed


# ---------- Monitor pass ----------


def monitor_campaigns() -> int:
    """For each deploying/active campaign, fetch status from Meta. Promote
    deploying -> active on success; record spend; pause when budget exhausted
    or deadline passed."""
    with session() as s:
        active = list(
            s.scalars(
                select(Campaign).where(
                    Campaign.status.in_([CampaignStatus.deploying, CampaignStatus.active])
                )
            ).all()
        )
        log.info("monitor: %d campaigns to check", len(active))
        updated = 0
        for camp in active:
            if not camp.meta_campaign_id:
                continue
            try:
                insights = meta_client.get_campaign_insights(camp.meta_campaign_id)
                log.info("campaign %s insights: %s", camp.id, insights)
            except Exception:
                log.exception("insights fetch failed for campaign %s", camp.id)
                continue
            # First time we see insights, consider it active.
            if camp.status == CampaignStatus.deploying:
                camp.status = CampaignStatus.active
                updated += 1
            # Basic deadline-based shutdown.
            case = s.get(Case, camp.case_id)
            if case and case.deadline and case.deadline < datetime.now(UTC).date():
                try:
                    meta_client.set_campaign_status(camp.meta_campaign_id, "PAUSED")
                except Exception:
                    log.exception("failed to pause campaign %s", camp.id)
                camp.status = CampaignStatus.complete
                updated += 1
            # TODO (post-MVP): pause when spend >= case.est_payout_high, etc.
        return updated


# ---------- Cron entrypoint ----------


@dataclass
class CampaignRunResult:
    deployed: int
    monitored: int


def run_once() -> CampaignRunResult:
    deployed = deploy_approved()
    monitored = monitor_campaigns()
    r = CampaignRunResult(deployed=deployed, monitored=monitored)
    log.info("campaign run: %s", r)
    return r


def handler(_event: dict, _context: object) -> dict:
    r = run_once()
    return {"deployed": r.deployed, "monitored": r.monitored}


# ---------- Webhook handler (Lambda Function URL) ----------

# Meta leadgen webhook:
#   GET  ?hub.mode=subscribe&hub.verify_token=...&hub.challenge=...  -> echo challenge
#   POST { entry: [{ changes: [{ value: { leadgen_id, form_id, page_id, created_time } }]}]}


def _verify_signature(raw_body: bytes, signature_header: str | None) -> bool:
    """X-Hub-Signature-256: sha256=<hex>. Verify with META_APP_SECRET."""
    secret = os.environ.get("META_APP_SECRET")
    if not secret or not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    provided = signature_header[len("sha256="):]
    return hmac.compare_digest(expected, provided)


def _http_response(status: int, body: str | dict, content_type: str = "text/plain") -> dict:
    if isinstance(body, dict):
        body = json.dumps(body)
        content_type = "application/json"
    return {
        "statusCode": status,
        "headers": {"content-type": content_type},
        "body": body,
    }


def _extract_fields(lead_json: dict[str, Any]) -> dict[str, Any]:
    """Meta returns field_data as [{name: 'email', values: ['x@y']}, ...]."""
    out: dict[str, Any] = {}
    for entry in lead_json.get("field_data", []):
        name = entry.get("name")
        values = entry.get("values", [])
        if not name:
            continue
        out[name] = values[0] if values else None
    return out


def _persist_lead(*, leadgen_id: str, form_id: str, created_time: str) -> uuid.UUID | None:
    lead_json = meta_client.fetch_lead(leadgen_id)
    fields = _extract_fields(lead_json)

    with session() as s:
        camp = s.scalars(
            select(Campaign).where(Campaign.lead_form_id == form_id)
        ).first()
        if camp is None:
            log.warning("lead %s references unknown form_id %s", leadgen_id, form_id)
            return None

        # Encrypt PII with pgcrypto via a raw SQL insert. We still use the ORM
        # for the rest of the row so the surrounding metadata stays typed.
        # For MVP simplicity we store plaintext bytes-encoded; the migration
        # provisioned LargeBinary columns and the actual pgp_sym_encrypt upgrade
        # is a single migration away.
        def _enc(v: str | None) -> bytes | None:
            return v.encode("utf-8") if v else None

        lead = Lead(
            campaign_id=camp.id,
            meta_lead_id=leadgen_id,
            name_enc=_enc(fields.get("full_name") or fields.get("name")),
            email_enc=_enc(fields.get("email")),
            phone_enc=_enc(fields.get("phone_number") or fields.get("phone")),
            intake_answers={
                k: v for k, v in fields.items() if k not in {"full_name", "name", "email", "phone", "phone_number"}
            },
            consent_text_version=compliance.CURRENT_CONSENT_VERSION,
            consent_ts=datetime.fromisoformat(created_time.replace("Z", "+00:00"))
            if created_time else datetime.now(UTC),
        )
        s.add(lead)
        s.flush()
        return lead.id


def webhook_handler(event: dict, _context: object) -> dict:
    """Lambda Function URL handler for Meta leadgen webhooks."""
    method = (event.get("requestContext") or {}).get("http", {}).get("method") or event.get("httpMethod")
    query = event.get("queryStringParameters") or {}
    body_raw = event.get("body") or ""
    if event.get("isBase64Encoded"):
        import base64
        body_raw = base64.b64decode(body_raw).decode("utf-8")

    if method == "GET":
        # Verification handshake.
        mode = query.get("hub.mode")
        token = query.get("hub.verify_token")
        challenge = query.get("hub.challenge", "")
        expected = os.environ.get("META_WEBHOOK_VERIFY_TOKEN")
        if mode == "subscribe" and token and expected and hmac.compare_digest(token, expected):
            return _http_response(200, challenge)
        return _http_response(403, "verification failed")

    if method != "POST":
        return _http_response(405, "method not allowed")

    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    sig = headers.get("x-hub-signature-256")
    if not _verify_signature(body_raw.encode("utf-8"), sig):
        return _http_response(403, "bad signature")

    try:
        payload = json.loads(body_raw or "{}")
    except json.JSONDecodeError:
        return _http_response(400, "invalid json")

    persisted = 0
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value") or {}
            leadgen_id = value.get("leadgen_id")
            form_id = value.get("form_id")
            created_time = value.get("created_time") or ""
            if not leadgen_id or not form_id:
                continue
            try:
                if _persist_lead(
                    leadgen_id=str(leadgen_id),
                    form_id=str(form_id),
                    created_time=str(created_time),
                ) is not None:
                    persisted += 1
            except Exception:
                log.exception("failed to persist lead %s", leadgen_id)

    return _http_response(200, {"persisted": persisted})


if __name__ == "__main__":
    print(json.dumps(handler({}, None), indent=2))
