# Deployment

## Local (from source)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium

cp .env.example .env            # edit as needed

# First run: log in to the target site manually (window is visible).
CHAT2API_HEADLESS=false python -m src.main
# ...log in in the browser window; the session is saved to .browser_profile/

# Subsequent runs can be headless:
python -m src.main              # or: uvicorn src.api.app:app
```

The default provider is **Google AI Mode**, which needs no login, so a bare run
works out of the box; the login step above is only needed for providers that
require it.

## Docker

```bash
docker compose up -d chat2api        # API on :9000
```

`docker-compose.yml` pulls the published image
(`ghcr.io/madhavtummala/chat2api:latest`) by default, so no clone or local build
is needed. Pushing to `main` rebuilds and publishes that image via GitHub Actions
(`.github/workflows/docker.yml`). To build from source instead, comment the
`image:` line and uncomment `build: .`.

The shipped compose defaults to the auth-free **googleaimode** provider, run
headful with the live browser exposed over noVNC (see below) so you can watch/
debug or log straight in if you switch to a gated provider. For a leaner auth-free
setup, set `CHAT2API_HEADLESS=true` and drop `CHAT2API_VNC` and the `6080` port.

## Providers that need a login (or defeat a bot wall)

`expressai` requires a login; `perplexity` sits behind a **Cloudflare** bot check
that a *headless* browser never clears (the composer never appears → "chat UI did
not become ready"). Both are solved the same way: run the server **headful**
under a virtual display (Xvfb) and expose that live browser over
[noVNC](https://novnc.com/) so you can log in / click through the wall directly.

There's no separate login container — you log in on the *running server's* own
browser, and the session persists to the profile volume. The shipped
`docker-compose.yml` already runs headful with noVNC exposed
(`CHAT2API_HEADLESS=false`, `CHAT2API_VNC=true`, port `6080`); just point it at the
gated provider (`CHAT2API_PROVIDER=expressai` or `perplexity`). Then:

```bash
docker compose up -d chat2api
curl http://<host>:9000/health                 # look for "authenticated": false
# open http://<host>:6080/vnc.html → Connect → log in in the browser window
# /health flips to "authenticated": true — no restart needed
```

Because it's a persistent profile, refreshed cookies are saved back, so the
session lasts as long as the site allows. If it ever expires, just VNC in and log
in again — no restart, no extra container. Tip: if the server is live and serving
traffic, open a **new tab** (Ctrl+T) in the VNC browser to log in, so an incoming
request doesn't navigate the tab you're typing in.

Copying a profile from your laptop instead is unreliable: Chrome encrypts its
cookie store with an OS-bound key, so a macOS profile's cookies won't decrypt
inside the Linux container — hence logging in *inside* the container.

> **Security:** noVNC is served with no password. Reach `:6080` only over a
> private network / SSH tunnel (e.g. Tailscale); never publish it to the
> internet. Once logged in you can drop `CHAT2API_VNC` / the `6080` mapping.

**Alternative — `storage_state` JSON:** if you'd rather log in on your laptop,
export a Playwright `storage_state` JSON (decrypted cookies + localStorage, which
*is* portable across OSes) and point `CHAT2API_STORAGE_STATE` at a mounted copy.
Simpler, but the context is ephemeral: cookie refreshes aren't saved back, so
you'll re-export more often than the noVNC/persistent-profile route needs.

## Detecting logout

Before each request the provider verifies it's still logged in. If the session
has expired it raises `AuthenticationRequired` — the API returns a clear
upstream-auth error instead of hanging, and the login state is cached and exposed
on **`GET /health`** (per provider):

```jsonc
{"status": "ok", "provider": "expressai", "authenticated": true,
 "providers": {"expressai": true, "googleaimode": null}}
```

`GET /health?deep=1` actively re-probes the live UI. To recover, re-authenticate
once with `CHAT2API_HEADLESS=false` (the login persists in the browser profile)
or supply a fresh `CHAT2API_STORAGE_STATE`.

## Configuration reference

All settings use the `CHAT2API_` env prefix (optionally via a `.env` file).

| Variable | Default | Purpose |
|----------|---------|---------|
| `HOST` / `PORT` | `0.0.0.0` / `9000` | HTTP bind address |
| `LOG_LEVEL` | `info` | Log verbosity |
| `API_KEYS` | *(empty)* | Comma-separated bearer keys; empty = auth off |
| `PROVIDER` | `googleaimode` | Default provider for unprefixed models |
| `PROVIDERS` | *(empty)* | Allowlist of routable providers; empty = all registered |
| `MCP_CONFIG_PATH` | `mcp.json` | MCP server config; auto-loaded when present |
| `ENABLE_RESPONSES` | `true` | Expose `/v1/responses` (agentic loop + MCP execution) |
| `MAX_AGENT_TURNS` | `6` | Max model↔tool round-trips per `/v1/responses` request |
| `HEADLESS` | `true` | Run Chromium headless (`false` for login / Cloudflare) |
| `USER_DATA_DIR` | `.browser_profile` | Persistent Chromium profile (logins/cookies) |
| `STORAGE_STATE` | *(none)* | Playwright storage_state JSON (alternative to profile) |
| `MAX_CONCURRENCY` | `2` | Tabs per provider pool (concurrent requests each) |
| `MAX_TAB_USES` | `200` | Recycle a tab after N uses (`0` = never) |
| `NAV_TIMEOUT_MS` | `45000` | Per-request navigation / element timeout |
| `RESPONSE_TIMEOUT_S` | `180` | Max wait for a full model response |
| `POLL_INTERVAL_S` | `0.2` | DOM polling interval while streaming |
| `EXPRESSAI_BASE_URL` | `https://app.expressai.com` | ExpressAI site URL |
| `PERPLEXITY_BASE_URL` | `https://www.perplexity.ai` | Perplexity site URL |
