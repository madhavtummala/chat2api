# Unattended re-authentication (auto-login)

ExpressAI's session cookie lasts **24 hours** (per
[their privacy policy](https://app.expressai.com/privacy)), and the re-login
sends a one-time code by email. Left alone, that means a human at the
[noVNC](deployment.md#providers-that-need-a-login-or-defeat-a-bot-wall) session
every single day.

Auto-login closes that loop: when a provider is found logged out, chat2api
drives its sign-in form, reads the emailed code from a mailbox, and submits it.

It is **off by default** — it needs stored credentials and read access to a
mailbox, so it should be a deliberate choice.

## Before you start

Two things are worth checking first, because either may make this unnecessary:

- **A "remember this device" option** on the provider's login. If it yields a
  30-day session, that's a checkbox instead of everything below.
- **TOTP / authenticator-app 2FA.** If the provider offers it, prefer it — the
  code is generated locally from a seed and no mailbox access is involved
  anywhere. (ExpressAI does not offer it today, which is why this exists.)

Also check the provider's terms on automated access before relying on this.

## Read this before pointing it at your main mailbox

**Use a dedicated Google account.** A Gmail OAuth token is scoped to an
*account*, not to a label. The `CHAT2API_GMAIL_OTP_LABEL` setting narrows the
query chat2api sends; it does not narrow what the token can do. Anyone who
obtains that refresh token can read the **entire** mailbox, for as long as the
grant lives.

The setup that keeps this small:

1. Create a throwaway Google account that you use for nothing else.
2. In your **real** inbox, add a filter: `from:(the provider's OTP sender)` →
   *Forward to* the throwaway account. (Gmail requires you to verify the
   forwarding address once.)
3. Authorize chat2api against the **throwaway** account only.

Now the worst case for a leaked token is a mailbox containing nothing but
expired one-time codes.

Two further notes:

- Storing the password and the code source on one machine collapses 2FA back to
  a single factor. That is the inherent trade for unattended operation — worth
  making knowingly.
- Automated logins can trip a provider's risk heuristics. Auto-login may
  therefore *increase* lockouts rather than reduce them; the cooldown below
  exists to keep that from compounding.

## Setup

### 1. Mint a Gmail refresh token

In the [Google Cloud console](https://console.cloud.google.com/):

1. Create a project and enable the **Gmail API**.
2. Create an OAuth client ID, type **Desktop app**. Note the id and secret.
3. On the consent screen, set the publishing status to **In production**.

Step 3 matters more than it looks. While the app sits in **Testing**, Google
expires its refresh tokens after **7 days** — the server would silently stop
being able to read codes every week. Publishing removes that limit.

`gmail.readonly` is a *restricted* scope, so publishing an unverified app means
the consent screen shows a "Google hasn't verified this app" warning: click
**Advanced → Go to … (unsafe)** to proceed. Verification is only needed to
remove that warning and to serve users other than yourself.

Then, on a machine with a browser (your laptop — not the headless Pi):

```bash
python scripts/gmail_oauth.py \
  --client-id XXXX.apps.googleusercontent.com \
  --client-secret GOCSPX-...
```

Sign in as the dedicated account. The script prints the three env vars to copy
to the server.

The scope requested is `gmail.readonly`, and nothing in chat2api writes to the
mailbox. A single-use code cannot be replayed because each login takes a
watermark before touching the form and ignores any message older than it — so
no write access is needed to track what has been consumed.

This is the narrowest scope that can read a message body: `gmail.metadata`
cannot, and Google offers no per-label or per-sender scope. That limit is
exactly why the dedicated account above matters.

### 2. Narrow the search

Nothing to do for a built-in provider: **the sender and code format are declared
by the provider itself**, in its `LoginFlow`. ExpressAI already carries
`info@info.expressvpn.com` and a pattern matching "Verification code: NNNNNN".

`CHAT2API_GMAIL_OTP_LABEL` can narrow further to a mailbox label, but it is
empty by default and you should leave it alone unless you actually created
one — the sender filter already does the job.

### 3. Configure the server

```bash
CHAT2API_AUTO_LOGIN=true
CHAT2API_OTP_SOURCE=gmail
CHAT2API_GMAIL_CLIENT_ID=...
CHAT2API_GMAIL_CLIENT_SECRET=...
CHAT2API_GMAIL_REFRESH_TOKEN=...
CHAT2API_EXPRESSAI_EMAIL=you@example.com
```

No password: ExpressVPN's sign-in is **passwordless** (email, then an emailed
code). No sender or pattern either — ExpressAI declares both.

## How it behaves

When `_ensure_ready` finds a login-required provider logged out, it attempts the
login instead of failing the request. Three behaviours are worth knowing:

- **One login at a time.** `MAX_CONCURRENCY` tabs can discover a dead session
  simultaneously; they queue behind a single login, and the ones that queued
  re-check and return without logging in again. Two concurrent logins would race
  for one single-use code and both lose.
- **Fresh codes only.** A watermark is taken *before* the form is touched, and
  any message older than it is ignored. Without this the first poll finds
  yesterday's code, submits an already-used value, and the failure looks like a
  broken selector.
- **Failures back off.** After a failed login, further attempts are refused for
  `LOGIN_RETRY_COOLDOWN_S` (default 15 min) and the error says so. Each attempt
  spends a code and counts against the provider's rate limit, so a wrong password
  must not be retried on every request.

A login that is *configured but fails* surfaces the real error rather than the
generic "log in via noVNC" message — a wrong password and a changed form should
not look the same.

## Adding another provider

The mechanism is provider-agnostic; only the form differs. Add a `LoginFlow`
alongside the provider's existing `Selectors`:

```python
_LOGIN_FLOW = LoginFlow(
    start_button="button:has-text('Sign in')",
    email_input="input[type='email']",
    password_input="input[type='password']",
    otp_input="input[autocomplete='one-time-code']",
    # How *this* provider's code mail looks. Kept here rather than in global
    # settings so two providers needing auto-login never collide.
    otp_sender="noreply@foo.example",
    otp_pattern=r"(?i)your code is (\d{6})",
)

class FooProvider(BrowserChatProvider):
    login_flow = _LOGIN_FLOW
```

Every step is optional — an empty selector, or one that never appears, is
skipped, so single-page, multi-step and **passwordless** forms all work without
a bespoke code path. Leaving `password_input` empty is how you declare a
passwordless flow; auto-login then stops requiring a password to be configured.
Where a step has no submit button, Enter is pressed in the field instead.
Credentials are read from `CHAT2API_FOO_EMAIL` / `CHAT2API_FOO_PASSWORD` by
convention; override `login_credentials()` to change that.

For sites that split the code across one-character boxes, set
`otp_input_is_split=True` and point `otp_input` at the boxes.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `no one-time code arrived within Ns` | The filter isn't applying the label, forwarding isn't set up, or `OTP_CODE_PATTERN` doesn't match. |
| `Gmail token refresh failed (400)` | Refresh token revoked or expired. Re-run `scripts/gmail_oauth.py`. |
| `the one-time-code field never appeared` | The password was rejected, or the login form changed. |
| `login field '...' never appeared` | Selectors are stale — re-check with `scripts/inspect_provider.py`. |
| `not retrying for another Ns` | A previous attempt failed; the cooldown is holding. Check the logs above it for the real cause. |
