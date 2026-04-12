"""Meta Marketing API wrapper.

This is the file that replaces the Manus AI idea from the original brief
(see plan §2). Every call into the `facebook-business` SDK goes through here
so the agent code stays readable and we have a single chokepoint for policy
pre-checks, logging, and the eventual swap to sandbox vs. live accounts.

Scope (MVP): Advantage+ lead campaigns only. Objective=OUTCOME_LEADS. Broad
targeting (required for legal vertical — Meta restricts narrow targeting on
special-category ads). Lead form with TCPA consent text hardcoded from
compliance.py.
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from dataclasses import dataclass
from typing import Any

import httpx
from facebook_business.adobjects.adaccount import AdAccount
from facebook_business.adobjects.advideo import AdVideo
from facebook_business.adobjects.campaign import Campaign as MetaCampaign
from facebook_business.adobjects.page import Page
from facebook_business.api import FacebookAdsApi

import compliance

log = logging.getLogger(__name__)

_initialized = False


def _stub_enabled() -> bool:
    """Stub mode: LOCAL_STUB_META=1 OR META_ACCESS_TOKEN unset."""
    if os.environ.get("LOCAL_STUB_META") in {"1", "true", "yes"}:
        return True
    return not os.environ.get("META_ACCESS_TOKEN")


def _init() -> None:
    """Initialize the FB SDK once per Lambda invocation."""
    global _initialized
    if _initialized:
        return
    FacebookAdsApi.init(
        access_token=os.environ["META_ACCESS_TOKEN"],
        app_id=os.environ.get("META_APP_ID") or None,
        app_secret=os.environ.get("META_APP_SECRET") or None,
        api_version="v21.0",
    )
    _initialized = True


def _ad_account() -> AdAccount:
    _init()
    return AdAccount(os.environ["META_AD_ACCOUNT_ID"])


def _page_id() -> str:
    return os.environ["META_PAGE_ID"]


# ---------- Inputs ----------


@dataclass
class LeadFormField:
    """One field on the lead form. For MVP we use mostly Meta's standard
    fields (auto-filled from the user's Facebook profile) plus one custom
    qualifying question derived from ICP disqualifiers."""

    key: str                   # e.g. "FULL_NAME", "EMAIL", "PHONE", or "custom_question_0"
    label: str                 # user-visible label
    type: str                  # "FULL_NAME" | "EMAIL" | "PHONE" | "CUSTOM"
    options: list[str] | None = None  # for multiple-choice custom


@dataclass
class CreativeAssets:
    headline: str
    primary_text: str
    cta_type: str              # Meta CTA enum, e.g. "LEARN_MORE"
    image_bytes: bytes | None
    video_bytes: bytes | None


@dataclass
class DeployResult:
    campaign_id: str
    adset_id: str
    ad_id: str
    creative_id: str
    lead_form_id: str


# ---------- Upload helpers ----------


def _upload_image(image_bytes: bytes) -> str:
    """Upload to the ad account; return the image hash Meta uses to reference it.

    The facebook-business SDK JSON-encodes the `params` dict before sending
    and raw `bytes` aren't JSON-serializable. The documented path is to pass
    a `filename` — the SDK reads the file and uploads via multipart internally."""
    import os as _os
    import tempfile

    account = _ad_account()
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        tmp.write(image_bytes)
        tmp_path = tmp.name
    try:
        result = account.create_ad_image(params={"filename": tmp_path})
    finally:
        _os.unlink(tmp_path)
    # The SDK v21+ returns an AdImage object with hash at the top level.
    # Older SDKs wrapped it as {"images": {"<filename>": {"hash": "..."}}}.
    # Handle both shapes.
    if result.get("hash"):
        return result["hash"]
    images = result.get("images", {})
    if images:
        return next(iter(images.values()))["hash"]
    raise RuntimeError(f"Unexpected image upload response: {result}")


def _upload_video(video_bytes: bytes) -> str:
    """Upload a video; poll status until processed, return the video_id.

    Same filename-based upload pattern as images."""
    import os as _os
    import tempfile

    account = _ad_account()
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
        tmp.write(video_bytes)
        tmp_path = tmp.name
    try:
        video = account.create_ad_video(params={"source": tmp_path})
    finally:
        _os.unlink(tmp_path)
    video_id = video["id"]

    # Poll upload processing status. Meta can take tens of seconds.
    deadline = time.time() + 180
    while time.time() < deadline:
        v = AdVideo(video_id)
        v.remote_read(fields=["status"])
        status = (v.get("status") or {}).get("video_status")
        if status == "ready":
            return video_id
        if status == "error":
            raise RuntimeError(f"Video upload failed: {v}")
        time.sleep(5)
    raise TimeoutError(f"Video {video_id} did not become ready in 180s")


# ---------- Lead form ----------


def create_lead_form(
    *,
    name: str,
    privacy_policy_url: str,
    intro_headline: str,
    intro_description: str,
    fields: list[LeadFormField],
    consent_text: str,
) -> str:
    """Create a LeadgenForm on the configured Page. Returns the form id.

    The TCPA consent text goes into a `CUSTOM_DISCLAIMER` block that the
    user must see and the submission requires.
    """
    if _stub_enabled():
        form_id = f"local-form-{uuid.uuid4().hex[:12]}"
        log.info("[stub:meta] create_lead_form name=%r -> %s", name[:60], form_id)
        log.debug("[stub:meta] fields=%s consent_len=%d", [f.key for f in fields], len(consent_text))
        return form_id
    _init()
    page = Page(_page_id())

    questions: list[dict[str, Any]] = []
    for f in fields:
        q: dict[str, Any] = {"type": f.type, "key": f.key}
        # Meta rejects `label` on non-CUSTOM questions — they use their own
        # localized labels for FULL_NAME / EMAIL / PHONE / etc.
        if f.type == "CUSTOM":
            q["label"] = f.label
            if f.options:
                q["options"] = [{"value": o, "key": o} for o in f.options]
        questions.append(q)

    params = {
        "name": name,
        "privacy_policy": {"url": privacy_policy_url},
        "follow_up_action_url": privacy_policy_url,
        "context_card": {
            "title": intro_headline,
            "content": [intro_description],
            "button_text": "Continue",
            "style": "PARAGRAPH_STYLE",
        },
        "questions": questions,
        # CUSTOM_DISCLAIMER surfaces the TCPA consent text with a required checkbox.
        # Shape per Meta docs: body is an object with {"text": ...}, NOT a string.
        # Checkbox uses `is_required` (not `required`).
        "custom_disclaimer": {
            "title": "Consent",
            "body": {"text": consent_text},
            "checkboxes": [
                {
                    "key": "tcpa_consent",
                    "text": "I agree to the terms above.",
                    "is_required": True,
                    "is_checked_by_default": False,
                },
            ],
        },
    }
    resp = page.create_lead_gen_form(params=params)
    return resp["id"]


# ---------- Campaign deploy ----------


def deploy_lead_campaign(
    *,
    campaign_name: str,
    daily_budget_cents: int,
    lead_form_id: str,
    creative_assets: CreativeAssets,
    destination_url: str,
) -> DeployResult:
    """Deploy an Advantage+ lead campaign with a single ad set and ad."""
    if _stub_enabled():
        cid = f"local-camp-{uuid.uuid4().hex[:10]}"
        result = DeployResult(
            campaign_id=cid,
            adset_id=f"local-adset-{uuid.uuid4().hex[:10]}",
            ad_id=f"local-ad-{uuid.uuid4().hex[:10]}",
            creative_id=f"local-crt-{uuid.uuid4().hex[:10]}",
            lead_form_id=lead_form_id,
        )
        log.info(
            "[stub:meta] deploy_lead_campaign %s name=%r budget_cents=%d form=%s",
            cid, campaign_name, daily_budget_cents, lead_form_id,
        )
        log.info(
            "[stub:meta]   headline=%r cta=%s image=%s video=%s url=%s",
            creative_assets.headline[:60], creative_assets.cta_type,
            bool(creative_assets.image_bytes), bool(creative_assets.video_bytes),
            destination_url,
        )
        return result

    _init()
    account = _ad_account()

    # 1. Campaign (paused so we can review before flipping live)
    camp = account.create_campaign(
        params={
            "name": campaign_name,
            "objective": "OUTCOME_LEADS",
            "status": "PAUSED",
            # Class-action awareness ads are not in any of Meta's regulated
            # special categories (CREDIT/EMPLOYMENT/HOUSING/POLITICS/FINANCIAL_
            # PRODUCTS_SERVICES/ONLINE_GAMBLING_AND_GAMING). NONE is correct —
            # confirm with legal before launch. Note: narrow targeting is still
            # restricted on legal-vertical ads regardless of category, so we
            # rely on Advantage+ broad targeting at the ad set level.
            "special_ad_categories": ["NONE"],
            "buying_type": "AUCTION",
            # Required by Meta when campaign has no CBO budget (we budget at
            # ad set level). False = each ad set gets its own full budget,
            # no 20% cross-adset sharing.
            "is_adset_budget_sharing_enabled": False,
        }
    )
    campaign_id = camp["id"]

    # 2. Ad Set — Advantage+ audience, lead destination
    adset = account.create_ad_set(
        params={
            "name": f"{campaign_name} / adset",
            "campaign_id": campaign_id,
            "daily_budget": daily_budget_cents,
            "billing_event": "IMPRESSIONS",
            "optimization_goal": "LEAD_GENERATION",
            "bid_strategy": "LOWEST_COST_WITHOUT_CAP",
            "status": "PAUSED",
            "destination_type": "ON_AD",  # lead form lives on the ad
            "targeting": {
                # Advantage+ audience is the default for special-category ads.
                "geo_locations": {"countries": ["US"]},
                "age_min": 18,
            },
            "promoted_object": {
                "page_id": _page_id(),
            },
        }
    )
    adset_id = adset["id"]

    # 3. Creative (image or video)
    object_story_spec: dict[str, Any] = {
        "page_id": _page_id(),
    }

    if creative_assets.video_bytes:
        video_id = _upload_video(creative_assets.video_bytes)
        object_story_spec["video_data"] = {
            "video_id": video_id,
            "title": creative_assets.headline,
            "message": creative_assets.primary_text,
            "call_to_action": {
                "type": creative_assets.cta_type,
                "value": {"lead_gen_form_id": lead_form_id, "link": destination_url},
            },
        }
    elif creative_assets.image_bytes:
        image_hash = _upload_image(creative_assets.image_bytes)
        object_story_spec["link_data"] = {
            "image_hash": image_hash,
            "link": destination_url,
            "message": creative_assets.primary_text,
            "name": creative_assets.headline,
            "call_to_action": {
                "type": creative_assets.cta_type,
                "value": {"lead_gen_form_id": lead_form_id, "link": destination_url},
            },
        }
    else:
        raise ValueError("CreativeAssets must include either image_bytes or video_bytes")

    creative = account.create_ad_creative(
        params={
            "name": f"{campaign_name} / creative",
            "object_story_spec": object_story_spec,
        }
    )
    creative_id = creative["id"]

    # 4. Ad
    ad = account.create_ad(
        params={
            "name": f"{campaign_name} / ad",
            "adset_id": adset_id,
            "creative": {"creative_id": creative_id},
            "status": "PAUSED",
        }
    )
    ad_id = ad["id"]

    return DeployResult(
        campaign_id=campaign_id,
        adset_id=adset_id,
        ad_id=ad_id,
        creative_id=creative_id,
        lead_form_id=lead_form_id,
    )


def set_campaign_status(campaign_id: str, status: str) -> None:
    """status in ACTIVE, PAUSED, ARCHIVED, DELETED."""
    if _stub_enabled() or campaign_id.startswith("local-"):
        log.info("[stub:meta] set_campaign_status %s -> %s", campaign_id, status)
        return
    _init()
    c = MetaCampaign(campaign_id)
    c.api_update(params={"status": status})


def get_campaign_insights(campaign_id: str) -> dict[str, Any]:
    if _stub_enabled() or campaign_id.startswith("local-"):
        return {"spend": "0.00", "impressions": "0", "clicks": "0", "leads": 0}
    _init()
    c = MetaCampaign(campaign_id)
    # Meta doesn't expose a direct "leads" field — lead counts come back in
    # the `actions` array keyed by action_type "lead" or "leadgen.other".
    insights = c.get_insights(fields=["spend", "impressions", "clicks", "actions"])
    if not insights:
        return {}
    data = insights[0].export_all_data()
    # Flatten lead-related actions into a top-level count for the monitor log.
    import contextlib

    lead_count = 0
    for action in data.get("actions", []) or []:
        if action.get("action_type") in ("lead", "leadgen.other", "onsite_conversion.lead_grouped"):
            with contextlib.suppress(TypeError, ValueError):
                lead_count += int(action.get("value", 0))
    data["leads"] = lead_count
    return data


# ---------- Lead retrieval (called from webhook handler) ----------


def fetch_lead(leadgen_id: str) -> dict[str, Any]:
    """Meta webhooks only send the leadgen_id. We call the Graph API to get the
    actual field_data.

    In stub mode, we look for a file at `.local-leads/<leadgen_id>.json`
    matching the shape that Graph API would return. This lets
    scripts/simulate_lead.py drop a file and fire a webhook with the matching
    id."""
    if _stub_enabled() or leadgen_id.startswith("local-"):
        from pathlib import Path
        path = Path(os.environ.get("LOCAL_LEADS_DIR", ".local-leads")) / f"{leadgen_id}.json"
        if path.exists():
            return json.loads(path.read_text())
        # Default-shaped minimal payload
        return {
            "id": leadgen_id,
            "created_time": "2026-04-12T12:00:00+0000",
            "field_data": [
                {"name": "full_name", "values": ["Jane Local"]},
                {"name": "email", "values": ["jane@local.test"]},
                {"name": "phone_number", "values": ["+15555550123"]},
                {"name": "qualifying_0", "values": ["Yes"]},
            ],
        }

    token = os.environ["META_ACCESS_TOKEN"]
    with httpx.Client(timeout=20.0) as c:
        r = c.get(
            f"https://graph.facebook.com/v21.0/{leadgen_id}",
            params={
                "access_token": token,
                "fields": "id,created_time,field_data,ad_id,form_id",
            },
        )
        r.raise_for_status()
        return r.json()


# ---------- Convenience: build a lead form from a Case ----------


def default_fields_for_case(qualifying_question: str | None) -> list[LeadFormField]:
    fields = [
        LeadFormField(key="full_name", label="Full name", type="FULL_NAME"),
        LeadFormField(key="email", label="Email address", type="EMAIL"),
        LeadFormField(key="phone", label="Phone number", type="PHONE"),
    ]
    if qualifying_question:
        fields.append(
            LeadFormField(
                key="qualifying_0",
                label=qualifying_question,
                type="CUSTOM",
                options=["Yes", "No"],
            )
        )
    return fields


def build_lead_form_for_case(
    *,
    case_title: str,
    case_summary: str,
    privacy_policy_url: str,
    qualifying_question: str | None = None,
    unique_suffix: str | None = None,
) -> str:
    """Create a LeadgenForm with the TCPA consent text pinned to the current version.

    Meta enforces unique form names per Page AND archived forms keep their
    names reserved indefinitely (no hard-delete). Every deploy attempt must
    therefore produce a NEW name, even if we retry the same creative. We
    append a wall-clock epoch suffix in addition to the caller-provided
    `unique_suffix` (typically a creative UUID prefix).
    """
    epoch = int(time.time())
    base = f"Suepercharge — {case_title[:45]}"
    tag = f"[{unique_suffix}-{epoch}]" if unique_suffix else f"[{epoch}]"
    name = f"{base} {tag}"
    return create_lead_form(
        name=name[:95],  # Meta form-name limit is 95 chars
        privacy_policy_url=privacy_policy_url,
        intro_headline=case_title[:60],  # Meta caps context_card.title at 60 chars
        intro_description=case_summary[:1000],
        fields=default_fields_for_case(qualifying_question),
        consent_text=compliance.consent_text(),
    )
