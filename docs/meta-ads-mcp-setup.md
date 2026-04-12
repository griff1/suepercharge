# Meta Ads MCP — Local Setup

Self-hosted [pipeboard-co/meta-ads-mcp](https://github.com/pipeboard-co/meta-ads-mcp) for managing Meta Ads from Claude Code.

## Prerequisites

- Python 3.10+
- `pipx` (or `uv`)
- Access to the Meta Developer App **suepercharge** (App ID: `1281135107502871`)

## 1. Install the MCP server

```sh
git clone https://github.com/pipeboard-co/meta-ads-mcp.git ~/code/meta-ads-mcp
cd ~/code/meta-ads-mcp
pipx install -e .
```

Verify: `meta-ads-mcp --help`

## 2. Get a long-lived Meta access token

1. Go to https://developers.facebook.com/tools/explorer/
2. Select the app **suepercharge** (`1281135107502871`) in the dropdown
3. Click **"Add a Permission"** and select all of these:
   - `ads_management`
   - `ads_read`
   - `pages_read_engagement`
   - `pages_manage_ads` (needed for lead forms and ad creatives)
   - `leads_retrieval` (if available — requires app review, skip if not shown)
4. Click **"Generate Access Token"** and authorize when prompted
5. Exchange for a 60-day long-lived token:

```sh
curl -s "https://graph.facebook.com/v21.0/oauth/access_token?\
grant_type=fb_exchange_token&\
client_id=1281135107502871&\
client_secret=ASK_PIERRE_FOR_THIS&\
fb_exchange_token=PASTE_YOUR_SHORT_LIVED_EAA_TOKEN" | python3 -m json.tool
```

The response `access_token` field is your long-lived token (starts with `EAA...`).

## 3. Get the `.env` file

Ask Pierre for the `.env` file. It already has:

| Field | Value |
|---|---|
| `META_APP_ID` | `1281135107502871` |
| `META_APP_SECRET` | Pre-filled (do NOT commit) |
| `META_AD_ACCOUNT_ID` | `act_106126916139269` (Griff Potrock) |
| `META_PAGE_ID` | `1124366050755593` (Suepercharge) |

Replace `META_ACCESS_TOKEN` with your own long-lived token from step 2.
Place the file at the repo root: `suepercharge/.env` (already gitignored).

## 4. Export env vars for Claude Code

The MCP server reads credentials from your shell environment. Add to `~/.zshrc`:

```sh
# Meta Ads MCP — Suepercharge
export META_APP_ID="1281135107502871"
export META_APP_SECRET="ASK_PIERRE"
export META_ACCESS_TOKEN="YOUR_LONG_LIVED_EAA_TOKEN"
export META_AD_ACCOUNT_ID="act_106126916139269"
export META_PAGE_ID="1124366050755593"
```

Then `source ~/.zshrc` or open a new terminal.

## 5. Wire MCP into Claude Code

Add this to your `~/.claude.json` under the `mcpServers` key:

```json
"mcpServers": {
  "meta-ads": {
    "command": "meta-ads-mcp",
    "args": ["--transport", "stdio"],
    "env": {
      "META_APP_ID": "${META_APP_ID}",
      "META_APP_SECRET": "${META_APP_SECRET}",
      "META_ACCESS_TOKEN": "${META_ACCESS_TOKEN}"
    }
  }
}
```

If `mcpServers` doesn't exist in the file, add it as a top-level key.

## 6. Smoke test

Restart Claude Code, then ask:

> "Use the meta-ads MCP to call get_ad_accounts with user_id 'me'"

You should see 3 ad accounts (Griff Potrock, Spotter Fitness, CerberusAI).

## 7. Existing Meta objects (already created, all PAUSED)

| Object | ID | Notes |
|---|---|---|
| Campaign | `6971774505152` | "Suepercharge — IG Stories Lead Gen [TEST]", OUTCOME_LEADS, $20/day, PAUSED |
| Ad Set | `6971774562752` | IG Stories + Reels placement, US 18+, PAUSED |
| Image | hash `8896d2cb734f895ecd00ad990d6ebdb5` | 1080x1920 placeholder |

View them at https://adsmanager.facebook.com

## 8. Remaining blockers before the ad is complete

### Blocker 1: Switch app to Live Mode

The app is in Development Mode, which blocks ad creative creation.

1. Go to https://developers.facebook.com/apps/1281135107502871/settings/basic/
2. Fill in **Privacy Policy URL**: `https://suepercharge.com/privacy`
3. Toggle **App Mode** from "Development" to "Live" at the top of the page

### Blocker 2: Add `pages_manage_ads` permission

Needed to create lead forms and ad creatives on the Suepercharge page.

1. Go to https://developers.facebook.com/tools/explorer/
2. Regenerate your token with `pages_manage_ads` added
3. Exchange for a new long-lived token (step 2 above)
4. Update `META_ACCESS_TOKEN` in `.env` and `~/.zshrc`

### Blocker 3: `leads_retrieval` permission (not urgent)

Requires formal Meta app review. Only needed when the campaign agent
starts pulling real leads. The local stub handles it for now.

### Once blockers 1 + 2 are cleared, create:

Ask Claude Code (with MCP connected) to:

1. **Create the lead form** on the Suepercharge page — pre-filled Full Name,
   Email, Phone, with TCPA consent text from `compliance.py`
2. **Create the ad creative** using the uploaded placeholder image (swap for
   real video later)
3. **Create the ad** under ad set `6971774562752`, PAUSED

Do NOT set any campaign or ad to ACTIVE without Pierre's approval.

## 9. Token refresh

The long-lived token expires after ~60 days.

| Event | Date |
|---|---|
| Last token generated | 2026-04-12 |
| Refresh reminder | **2026-06-06** |

To refresh: repeat step 2 and update `META_ACCESS_TOKEN` everywhere.

## Troubleshooting

- **`META_APP_ID environment variable is not set`** — shell profile isn't
  sourced. Run `source ~/.zshrc` or restart your terminal.
- **`OAuthException` / `Error validating access token`** — token expired or
  wrong scopes. Repeat step 2.
- **`app is in development mode`** — see Blocker 1 above.
- **`Requires pages_manage_ads permission`** — see Blocker 2 above.
- **MCP tools not appearing in Claude Code** — restart Claude Code after
  editing `~/.claude.json`. Verify `meta-ads-mcp --help` runs from the same shell.
- **Ad account not found** — make sure `META_AD_ACCOUNT_ID` starts with `act_`.
