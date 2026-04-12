# Meta Ads MCP — Teammate Handoff

## What's done
- MCP server installed (`pipeboard-co/meta-ads-mcp`)
- Campaign `6971774505152` + Ad Set `6971774562752` created in Meta (PAUSED)
- Placeholder image uploaded
- Claude Code MCP config wired

## What you need from Pierre
1. The `.env` file (has app secret + all IDs)
2. App secret for the token exchange curl command

## Your setup (15 min)
```sh
# 1. Clone & install MCP server
git clone https://github.com/pipeboard-co/meta-ads-mcp.git ~/code/meta-ads-mcp
cd ~/code/meta-ads-mcp && pipx install -e .

# 2. Get your own token
#    - Go to https://developers.facebook.com/tools/explorer/
#    - Select app "suepercharge"
#    - Add permissions: ads_management, ads_read, pages_read_engagement, pages_manage_ads
#    - Generate Access Token, authorize
#    - Exchange for 60-day token:
curl -s "https://graph.facebook.com/v21.0/oauth/access_token?\
grant_type=fb_exchange_token&\
client_id=1281135107502871&\
client_secret=ASK_PIERRE&\
fb_exchange_token=YOUR_EAA_TOKEN" | python3 -m json.tool

# 3. Drop .env in repo root (already gitignored), replace META_ACCESS_TOKEN with yours

# 4. Add to ~/.zshrc
export META_APP_ID="1281135107502871"
export META_APP_SECRET="ASK_PIERRE"
export META_ACCESS_TOKEN="YOUR_LONG_LIVED_TOKEN"
export META_AD_ACCOUNT_ID="act_106126916139269"
export META_PAGE_ID="1124366050755593"

# 5. Add to ~/.claude.json
```
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
```sh
# 6. Restart Claude Code, ask: "get_ad_accounts for user me"
```

## 3 blockers before ad goes live
1. **Switch app to Live Mode** → https://developers.facebook.com/apps/1281135107502871/settings/basic/
2. **Add `pages_manage_ads` permission** to token (regenerate)
3. **`leads_retrieval`** — needs Meta app review (not urgent, stub works locally)

## Rules
- **Nothing goes ACTIVE without Pierre's approval**
- **Never commit `.env` or tokens**
- Token expires ~2026-08-11, refresh by **2026-06-06**

Full details: `docs/meta-ads-mcp-setup.md`
