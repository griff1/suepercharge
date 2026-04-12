# Meta Ads MCP — Local Setup

Self-hosted [pipeboard-co/meta-ads-mcp](https://github.com/pipeboard-co/meta-ads-mcp) for managing Meta Ads from Claude Code.

## Prerequisites

- Python 3.10+
- `pipx` (or `uv`)
- A Meta Developer App (Business type) with these permissions:
  `ads_management`, `ads_read`, `pages_read_engagement`, `leads_retrieval`

## 1. Install the MCP server

```sh
git clone https://github.com/pipeboard-co/meta-ads-mcp.git ~/code/meta-ads-mcp
cd ~/code/meta-ads-mcp
pipx install -e .
```

Verify: `meta-ads-mcp --help`

## 2. Get a long-lived Meta access token

1. Go to https://developers.facebook.com/tools/explorer/
2. Select your app, generate a User Access Token with scopes:
   `ads_management`, `ads_read`, `pages_read_engagement`, `leads_retrieval`
3. Exchange for a 60-day token:

```sh
curl -s "https://graph.facebook.com/v21.0/oauth/access_token?\
grant_type=fb_exchange_token&\
client_id=YOUR_APP_ID&\
client_secret=YOUR_APP_SECRET&\
fb_exchange_token=YOUR_SHORT_LIVED_TOKEN" | python3 -m json.tool
```

## 3. Store credentials (never commit these)

Add to your shell profile (`~/.zshrc` or `~/.zprofile`):

```sh
# Meta Ads MCP
export META_APP_ID="your-app-id"
export META_APP_SECRET="your-app-secret"
export META_ACCESS_TOKEN="your-long-lived-token"

# Optional — populate after running get_ad_accounts via MCP
export META_AD_ACCOUNT_ID="act_XXXXXXXXX"
export META_PAGE_ID="XXXXXXXXX"
```

Then `source ~/.zshrc` (or open a new terminal).

## 4. Claude Code MCP config

Already wired in `~/.claude.json` under `mcpServers.meta-ads`. The server
reads `META_APP_ID`, `META_APP_SECRET`, and `META_ACCESS_TOKEN` from your
shell environment. No secrets in the config file.

## 5. Smoke test

Restart Claude Code, then ask it to run:
```
get_ad_accounts (user_id: "me")
```

This is read-only and confirms the token + MCP connection work.

## 6. Token refresh

The long-lived token expires after ~60 days. **Set a reminder for 55 days
after generation** to repeat step 2 and update `META_ACCESS_TOKEN` in your
shell profile.

Last token generated: 2026-04-12
Next refresh due: 2026-06-06

> **Note**: `leads_retrieval` permission requires Meta app review. Not needed
> until the campaign agent pulls real leads — the local stub handles it for now.

## Troubleshooting

- `META_APP_ID environment variable is not set` — your shell profile isn't
  sourced. Run `source ~/.zshrc` or restart your terminal.
- `OAuthException` / `Error validating access token` — token expired or
  wrong scopes. Repeat step 2.
- MCP tools not appearing in Claude Code — restart Claude Code after editing
  `~/.claude.json`. Check `meta-ads-mcp --help` runs from the same shell.
