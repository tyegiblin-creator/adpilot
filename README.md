# AdPilot — Build 03 (Meta OAuth + deployment preparation)

Mobile-first Flask app for small businesses to review paid advertising performance.

## What this build adds
- Meta OAuth authorisation with a cryptographically random state value.
- Requests read-only `ads_read` permission.
- Exchanges the login code and requests a long-lived access token.
- Encrypts stored Meta tokens with Fernet using `META_TOKEN_ENCRYPTION_KEY`.
- Lists accessible ad accounts and lets the operator select one.
- Imports campaign-level insights for the last 30 days and upserts them without duplicating campaigns.
- Disconnect action removes the stored Meta token and account list.
- Render deployment blueprint with a persistent disk for SQLite.

## Run locally
Python 3.10+ recommended.

```bash
python -m venv .venv
# macOS/Linux: source .venv/bin/activate
# Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
```

Populate the environment variables in `.env` (or export them in your shell), then:

```bash
python app.py
```

## Meta developer setup
1. Create a Meta developer account and a Meta app at https://developers.facebook.com/apps/.
2. Add/configure Facebook Login (web) and Marketing API for the app.
3. Add this exact redirect URL to the app's Valid OAuth Redirect URIs: `https://YOUR-ADPILOT-DOMAIN/oauth/meta/callback`.
4. Set `META_APP_ID`, `META_APP_SECRET`, and `META_REDIRECT_URI`.
5. Generate a Fernet key and set `META_TOKEN_ENCRYPTION_KEY`:
   `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`
6. Ensure the Meta app has the `ads_read` permission. App roles/test users can test in development mode; serving other businesses may require Meta App Review and Advanced Access. Check current Meta requirements before launch.
7. Deploy, then reconnect Meta using the public HTTPS URL.

The app uses Meta Graph/Marketing API v27.0 as the configured default. The version is configurable through `META_API_VERSION`; check Meta's version changelog before upgrading.

## Render deployment
The included `render.yaml` describes a Render web service using Gunicorn and a 1 GB persistent disk mounted at `/var/data`. Persistent disks require a paid Render service. SQLite data is configured to live on that disk. Connect this project to a Git repository, create a Blueprint on Render, and add the Meta environment values in the Render service's Environment page. Set `META_REDIRECT_URI` to the final Render URL plus `/oauth/meta/callback`, then add the same URI in Meta developer settings.

Render's standard free web services have ephemeral filesystems and sleep when idle; do not use an ephemeral SQLite file for customer data. See Render docs for current plan and disk costs.

## Important limitations before public launch
- This is still a single-workspace prototype. There are no customer accounts, tenant isolation, subscription billing, or team permissions.
- SQLite + one service instance is suitable only for this prototype. A multi-customer SaaS should migrate to managed PostgreSQL and use per-user/per-tenant token records.
- Add rate limiting, structured audit logs, encrypted backups, account deletion/export workflows, and security review before onboarding customers. Flask-WTF CSRF protection is enabled for state-changing forms.
- OAuth token refresh/expiry handling and scheduled background syncing are not yet implemented. If a token expires, reconnect Meta.
- Campaign revenue/conversions are read from Meta purchase action types only. If a business uses other conversion events or attribution settings, metrics need configurable mapping.
- API access and permission availability depend on Meta app mode, app roles, review and account permissions. This code does not bypass Meta's approval process.
- Never commit `.env`, app secrets, access tokens or encryption keys.
