"""One-time OAuth consent that mints the Gmail refresh token for auto-login.

Run this once, on a machine with a browser (your laptop, not the headless Pi).
It prints the refresh token to paste into the server's `.env`.

    python scripts/gmail_oauth.py --client-id XXX.apps.googleusercontent.com \
        --client-secret GOCSPX-...

Prerequisites, in the Google Cloud console:

    1. Create a project and enable the **Gmail API**.
    2. Create an OAuth client ID of type **Desktop app**; note its id + secret.
    3. Set the consent screen's publishing status to **In production**. Left in
       "Testing", Google expires the refresh token after 7 days and the server
       quietly stops being able to read codes. Because gmail.readonly is a
       restricted scope, an unverified app still warns at consent — click
       Advanced -> "Go to ... (unsafe)" to continue.

**Run this against a dedicated Google account**, not your main one. The token it
produces reads the entire mailbox for as long as the grant lives; the label
filter in `src/auth/otp.py` narrows our query, not the token's authority. Set
your real inbox to forward only the provider's one-time-code mail to that
account. See docs/auto-login.md.
"""

from __future__ import annotations

import argparse
import http.server
import secrets
import sys
import threading
import urllib.parse
import webbrowser

import httpx

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
# Read-only: nothing here writes to the mailbox. Single-use is enforced by the
# watermark in src/auth/otp.py, not by marking messages read, so no write scope
# is warranted. This is the narrowest scope that can read a message body —
# gmail.metadata cannot, and Google has no per-label or per-sender scope.
SCOPES = "https://www.googleapis.com/auth/gmail.readonly"
REDIRECT_HOST, REDIRECT_PORT = "127.0.0.1", 8765
REDIRECT_URI = f"http://{REDIRECT_HOST}:{REDIRECT_PORT}/"


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    """Catches Google's redirect and stashes the one-shot authorization code."""

    code: str | None = None
    state: str | None = None
    error: str | None = None

    def do_GET(self) -> None:  # noqa: N802 - name fixed by BaseHTTPRequestHandler
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        _CallbackHandler.code = (query.get("code") or [None])[0]
        _CallbackHandler.state = (query.get("state") or [None])[0]
        _CallbackHandler.error = (query.get("error") or [None])[0]
        body = b"Authorized. You can close this tab and return to the terminal."
        if _CallbackHandler.error:
            body = f"Authorization failed: {_CallbackHandler.error}".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args) -> None:
        pass  # keep the console clean; we print our own progress


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--client-id", required=True)
    parser.add_argument("--client-secret", required=True)
    args = parser.parse_args()

    # `state` guards against another local process racing our redirect; the
    # loopback port is open to anything on this machine while we wait.
    state = secrets.token_urlsafe(16)
    params = {
        "client_id": args.client_id,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": SCOPES,
        # A refresh token is only issued with consent forced offline; without
        # both of these Google returns an access token that dies in an hour.
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }
    url = f"{AUTH_URL}?{urllib.parse.urlencode(params)}"

    server = http.server.HTTPServer((REDIRECT_HOST, REDIRECT_PORT), _CallbackHandler)
    waiter = threading.Thread(target=server.handle_request, daemon=True)
    waiter.start()

    print("Opening your browser to authorize. Sign in as the mailbox that")
    print("receives the one-time codes (the dedicated account, not your main one).")
    print(f"\nIf nothing opens, visit:\n{url}\n")
    webbrowser.open(url)

    print("Waiting for the redirect...")
    waiter.join(timeout=300)
    server.socket.close()
    if waiter.is_alive():
        print("Timed out waiting for authorization.", file=sys.stderr)
        return 1
    if _CallbackHandler.error:
        print(f"Authorization failed: {_CallbackHandler.error}", file=sys.stderr)
        return 1
    if not _CallbackHandler.code:
        print("No authorization code received.", file=sys.stderr)
        return 1
    if _CallbackHandler.state != state:
        print("State mismatch — discarding this response.", file=sys.stderr)
        return 1

    response = httpx.post(
        TOKEN_URL,
        data={
            "code": _CallbackHandler.code,
            "client_id": args.client_id,
            "client_secret": args.client_secret,
            "redirect_uri": REDIRECT_URI,
            "grant_type": "authorization_code",
        },
        timeout=30,
    )
    if response.status_code >= 400:
        print(f"Token exchange failed ({response.status_code}): {response.text}", file=sys.stderr)
        return 1
    refresh_token = response.json().get("refresh_token")
    if not refresh_token:
        print(
            "No refresh_token in the response. Google only returns one on the first\n"
            "consent — revoke the app at https://myaccount.google.com/permissions\n"
            "and run this again.",
            file=sys.stderr,
        )
        return 1

    print("\nDone. Add these to the server's .env:\n")
    print(f"CHAT2API_GMAIL_CLIENT_ID={args.client_id}")
    print(f"CHAT2API_GMAIL_CLIENT_SECRET={args.client_secret}")
    print(f"CHAT2API_GMAIL_REFRESH_TOKEN={refresh_token}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
