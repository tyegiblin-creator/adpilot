import os
import sqlite3
import secrets
from datetime import datetime, timezone, timedelta
from urllib.parse import urlencode

import requests
from cryptography.fernet import Fernet, InvalidToken
from dotenv import load_dotenv
from flask_wtf.csrf import CSRFProtect
from pathlib import Path
from flask import Flask, g, render_template, request, redirect, url_for, flash, abort, session

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
DATABASE = Path(os.environ.get("DATABASE_PATH", str(BASE_DIR / "adpilot.db")))
GRAPH_VERSION = os.environ.get("META_API_VERSION", "v27.0")
GRAPH_BASE = f"https://graph.facebook.com/{GRAPH_VERSION}"
META_AUTH_URL = f"https://www.facebook.com/{GRAPH_VERSION}/dialog/oauth"

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-only-change-this-before-deploying")
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "0") == "1",
    PERMANENT_SESSION_LIFETIME=timedelta(hours=1),
)
csrf = CSRFProtect(app)

PROVIDERS = {
    "meta": {
        "name": "Meta Ads",
        "description": "Facebook and Instagram campaign performance.",
        "client_id_env": "META_APP_ID",
        "client_secret_env": "META_APP_SECRET",
        "docs": "https://developers.facebook.com/docs/marketing-apis/",
    },
    "google": {
        "name": "Google Ads",
        "description": "Search, display and video campaign performance.",
        "client_id_env": "GOOGLE_CLIENT_ID",
        "client_secret_env": "GOOGLE_CLIENT_SECRET",
        "docs": "https://developers.google.com/google-ads/api/docs/start",
    },
    "tiktok": {
        "name": "TikTok Ads",
        "description": "TikTok campaign and ad-group performance.",
        "client_id_env": "TIKTOK_APP_ID",
        "client_secret_env": "TIKTOK_APP_SECRET",
        "docs": "https://business-api.tiktok.com/portal/docs",
    },
}


def get_db():
    if "db" not in g:
        DATABASE.parent.mkdir(parents=True, exist_ok=True)
        g.db = sqlite3.connect(DATABASE)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(_error=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = sqlite3.connect(DATABASE)
    db.execute("""
        CREATE TABLE IF NOT EXISTS campaigns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            platform TEXT NOT NULL,
            spend REAL NOT NULL DEFAULT 0,
            impressions INTEGER NOT NULL DEFAULT 0,
            clicks INTEGER NOT NULL DEFAULT 0,
            conversions INTEGER NOT NULL DEFAULT 0,
            revenue REAL NOT NULL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS integrations (
            provider TEXT PRIMARY KEY,
            account_label TEXT,
            status TEXT NOT NULL DEFAULT 'not_connected',
            connected_at TIMESTAMP,
            last_synced_at TIMESTAMP
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS integration_tokens (
            provider TEXT PRIMARY KEY,
            encrypted_access_token BLOB NOT NULL,
            expires_at TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS meta_ad_accounts (
            account_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            currency TEXT,
            timezone_name TEXT,
            account_status INTEGER,
            selected INTEGER NOT NULL DEFAULT 0
        )
    """)
    # Safe additive migration for projects created by earlier AdPilot builds.
    campaign_columns = {row[1] for row in db.execute("PRAGMA table_info(campaigns)")}
    if "provider" not in campaign_columns:
        db.execute("ALTER TABLE campaigns ADD COLUMN provider TEXT NOT NULL DEFAULT 'manual'")
    if "external_id" not in campaign_columns:
        db.execute("ALTER TABLE campaigns ADD COLUMN external_id TEXT")
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_campaign_provider_external ON campaigns(provider, external_id)")
    db.commit()
    db.close()


def metrics(row):
    spend = float(row["spend"] or 0)
    impressions = int(row["impressions"] or 0)
    clicks = int(row["clicks"] or 0)
    conversions = int(row["conversions"] or 0)
    revenue = float(row["revenue"] or 0)
    return {
        "ctr": (clicks / impressions * 100) if impressions else 0,
        "cpc": (spend / clicks) if clicks else 0,
        "cpa": (spend / conversions) if conversions else 0,
        "roas": (revenue / spend) if spend else 0,
        "profit": revenue - spend,
    }


def integration_cards():
    saved = {
        row["provider"]: row
        for row in get_db().execute("SELECT * FROM integrations").fetchall()
    }
    cards = []
    for key, provider in PROVIDERS.items():
        row = saved.get(key)
        configured = bool(os.environ.get(provider["client_id_env"]) and
                          os.environ.get(provider["client_secret_env"]))
        cards.append({
            "key": key,
            "name": provider["name"],
            "description": provider["description"],
            "docs": provider["docs"],
            "configured": configured,
            "status": row["status"] if row else "not_connected",
            "account_label": row["account_label"] if row else None,
            "last_synced_at": row["last_synced_at"] if row else None,
            "accounts": [dict(a) for a in get_db().execute("SELECT * FROM meta_ad_accounts ORDER BY name").fetchall()] if key == "meta" else [],
            "token_available": bool(get_meta_token()) if key == "meta" else False,
        })
    return cards


@app.get("/")
def dashboard():
    rows = get_db().execute(
        "SELECT * FROM campaigns ORDER BY created_at DESC, id DESC"
    ).fetchall()
    total_spend = sum(float(r["spend"]) for r in rows)
    total_revenue = sum(float(r["revenue"]) for r in rows)
    total_clicks = sum(int(r["clicks"]) for r in rows)
    total_conversions = sum(int(r["conversions"]) for r in rows)
    total_impressions = sum(int(r["impressions"]) for r in rows)
    summary = {
        "spend": total_spend,
        "revenue": total_revenue,
        "profit": total_revenue - total_spend,
        "roas": total_revenue / total_spend if total_spend else 0,
        "ctr": total_clicks / total_impressions * 100 if total_impressions else 0,
        "conversions": total_conversions,
    }
    campaign_rows = []
    for row in rows:
        item = dict(row)
        item.update(metrics(row))
        campaign_rows.append(item)
    return render_template("dashboard.html", summary=summary, campaigns=campaign_rows)


@app.get("/integrations")
def integrations():
    return render_template("integrations.html", providers=integration_cards())


def meta_credentials_ready():
    required = ["META_APP_ID", "META_APP_SECRET", "META_REDIRECT_URI", "META_TOKEN_ENCRYPTION_KEY"]
    return all(os.environ.get(key) for key in required)


def token_cipher():
    key = os.environ.get("META_TOKEN_ENCRYPTION_KEY", "")
    if not key:
        raise RuntimeError("META_TOKEN_ENCRYPTION_KEY is not configured")
    try:
        return Fernet(key.encode("ascii"))
    except Exception as exc:
        raise RuntimeError("META_TOKEN_ENCRYPTION_KEY must be a valid Fernet key") from exc


def save_meta_token(access_token, expires_in=None):
    encrypted = token_cipher().encrypt(access_token.encode("utf-8"))
    expires_at = None
    if expires_in:
        expires_at = (datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))).isoformat()
    db = get_db()
    db.execute("""INSERT INTO integration_tokens(provider, encrypted_access_token, expires_at)
        VALUES ('meta', ?, ?) ON CONFLICT(provider) DO UPDATE SET
        encrypted_access_token=excluded.encrypted_access_token,
        expires_at=excluded.expires_at, updated_at=CURRENT_TIMESTAMP""", (encrypted, expires_at))
    db.execute("""INSERT INTO integrations(provider, status, connected_at)
        VALUES ('meta', 'connected', CURRENT_TIMESTAMP)
        ON CONFLICT(provider) DO UPDATE SET status='connected', connected_at=CURRENT_TIMESTAMP""")
    db.commit()


def get_meta_token():
    row = get_db().execute("SELECT encrypted_access_token, expires_at FROM integration_tokens WHERE provider='meta'").fetchone()
    if not row:
        return None
    if row["expires_at"]:
        expiry = datetime.fromisoformat(row["expires_at"])
        if expiry <= datetime.now(timezone.utc):
            return None
    try:
        return token_cipher().decrypt(row["encrypted_access_token"]).decode("utf-8")
    except (InvalidToken, RuntimeError, ValueError):
        return None


def graph_get(path, access_token, params=None):
    query = dict(params or {})
    query["access_token"] = access_token
    response = requests.get(f"{GRAPH_BASE}/{path.lstrip('/')}", params=query, timeout=25)
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    if not response.ok or "error" in payload:
        message = payload.get("error", {}).get("message", "Meta returned an unexpected response")
        raise RuntimeError(message[:350])
    return payload


def fetch_meta_ad_accounts(access_token):
    payload = graph_get("me/adaccounts", access_token, {
        "fields": "id,name,account_status,currency,timezone_name", "limit": 100
    })
    accounts = payload.get("data", [])
    while payload.get("paging", {}).get("next"):
        response = requests.get(payload["paging"]["next"], timeout=25)
        if not response.ok:
            raise RuntimeError("Unable to retrieve all Meta ad accounts")
        payload = response.json()
        accounts.extend(payload.get("data", []))
        if len(accounts) > 500:
            break
    return accounts


@app.post("/integrations/<provider_key>/connect")
def connect_integration(provider_key):
    if provider_key != "meta":
        flash("This provider is scheduled for a later milestone. Meta Ads is the first live integration.", "error")
        return redirect(url_for("integrations"))
    if not meta_credentials_ready():
        flash("Meta setup is incomplete. Configure META_APP_ID, META_APP_SECRET, META_REDIRECT_URI and META_TOKEN_ENCRYPTION_KEY first.", "error")
        return redirect(url_for("integrations"))
    state = secrets.token_urlsafe(32)
    session["meta_oauth_state"] = state
    session.permanent = True
    params = {
        "client_id": os.environ["META_APP_ID"],
        "redirect_uri": os.environ["META_REDIRECT_URI"],
        "state": state,
        "scope": "ads_read",
        "response_type": "code",
    }
    return redirect(f"{META_AUTH_URL}?{urlencode(params)}")


@app.get("/oauth/meta/callback")
def meta_oauth_callback():
    error = request.args.get("error_description") or request.args.get("error")
    if error:
        flash("Meta authorisation was not completed: " + error[:220], "error")
        return redirect(url_for("integrations"))
    returned_state = request.args.get("state", "")
    expected_state = session.pop("meta_oauth_state", None)
    if not expected_state or not secrets.compare_digest(returned_state, expected_state):
        abort(400, "Invalid OAuth state. Please restart the Meta connection.")
    code = request.args.get("code")
    if not code or not meta_credentials_ready():
        flash("Meta did not return an authorisation code, or app configuration is incomplete.", "error")
        return redirect(url_for("integrations"))
    try:
        token_response = requests.get(f"{GRAPH_BASE}/oauth/access_token", params={
            "client_id": os.environ["META_APP_ID"],
            "client_secret": os.environ["META_APP_SECRET"],
            "redirect_uri": os.environ["META_REDIRECT_URI"],
            "code": code,
        }, timeout=25)
        token_payload = token_response.json()
        if not token_response.ok or "access_token" not in token_payload:
            raise RuntimeError(token_payload.get("error", {}).get("message", "Token exchange failed"))
        short_token = token_payload["access_token"]
        long_response = requests.get(f"{GRAPH_BASE}/oauth/access_token", params={
            "grant_type": "fb_exchange_token",
            "client_id": os.environ["META_APP_ID"],
            "client_secret": os.environ["META_APP_SECRET"],
            "fb_exchange_token": short_token,
        }, timeout=25)
        long_payload = long_response.json()
        if not long_response.ok or "access_token" not in long_payload:
            raise RuntimeError(long_payload.get("error", {}).get("message", "Long-lived token exchange failed"))
        access_token = long_payload["access_token"]
        save_meta_token(access_token, long_payload.get("expires_in"))
        accounts = fetch_meta_ad_accounts(access_token)
        db = get_db()
        db.execute("DELETE FROM meta_ad_accounts")
        for account in accounts:
            db.execute("""INSERT OR REPLACE INTO meta_ad_accounts
                (account_id, name, currency, timezone_name, account_status, selected)
                VALUES (?, ?, ?, ?, ?, 0)""", (
                    account.get("id"), account.get("name") or account.get("id"),
                    account.get("currency"), account.get("timezone_name"), account.get("account_status")
                ))
        db.commit()
        flash(f"Meta connected. Found {len(accounts)} advertising account(s); choose one to import.", "success")
    except (requests.RequestException, RuntimeError, ValueError, KeyError) as exc:
        flash("Meta connection failed: " + str(exc)[:250], "error")
    return redirect(url_for("integrations"))


@app.post("/integrations/meta/account")
def select_meta_account():
    account_id = request.form.get("account_id", "").strip()
    db = get_db()
    exists = db.execute("SELECT account_id FROM meta_ad_accounts WHERE account_id=?", (account_id,)).fetchone()
    if not exists:
        abort(400, "Choose a valid Meta ad account")
    db.execute("UPDATE meta_ad_accounts SET selected=0")
    db.execute("UPDATE meta_ad_accounts SET selected=1 WHERE account_id=?", (account_id,))
    db.commit()
    flash("Meta ad account selected. You can now sync its recent campaign performance.", "success")
    return redirect(url_for("integrations"))


@app.post("/integrations/meta/sync")
def sync_meta_campaigns():
    access_token = get_meta_token()
    account = get_db().execute("SELECT * FROM meta_ad_accounts WHERE selected=1").fetchone()
    if not access_token or not account:
        flash("Connect Meta and select an ad account before syncing.", "error")
        return redirect(url_for("integrations"))
    try:
        payload = graph_get(f"{account['account_id']}/insights", access_token, {
            "level": "campaign",
            "date_preset": "last_30d",
            "fields": "campaign_id,campaign_name,spend,impressions,clicks,actions,action_values",
            "limit": 100,
        })
        records = payload.get("data", [])
        while payload.get("paging", {}).get("next"):
            page = requests.get(payload["paging"]["next"], timeout=25)
            if not page.ok:
                raise RuntimeError("Meta campaign report pagination failed")
            payload = page.json()
            records.extend(payload.get("data", []))
            if len(records) > 2000:
                break
        db = get_db()
        for item in records:
            campaign_id = item.get("campaign_id")
            if not campaign_id:
                continue
            action_values = item.get("action_values", [])
            actions = item.get("actions", [])
            purchase_types = {"purchase", "omni_purchase", "offsite_conversion.fb_pixel_purchase"}
            conversions = sum(float(a.get("value", 0)) for a in actions if a.get("action_type") in purchase_types)
            revenue = sum(float(a.get("value", 0)) for a in action_values if a.get("action_type") in purchase_types)
            db.execute("""INSERT INTO campaigns
                (name, platform, spend, impressions, clicks, conversions, revenue, provider, external_id)
                VALUES (?, 'Meta Ads', ?, ?, ?, ?, ?, 'meta', ?)
                ON CONFLICT(provider, external_id) DO UPDATE SET
                name=excluded.name, spend=excluded.spend, impressions=excluded.impressions,
                clicks=excluded.clicks, conversions=excluded.conversions, revenue=excluded.revenue""", (
                    item.get("campaign_name") or "Meta campaign",
                    float(item.get("spend", 0)), int(item.get("impressions", 0)),
                    int(item.get("clicks", 0)), conversions, revenue, campaign_id
                ))
        db.execute("UPDATE integrations SET last_synced_at=CURRENT_TIMESTAMP WHERE provider='meta'")
        db.commit()
        flash(f"Synced {len(records)} Meta campaign report(s) for the last 30 days.", "success")
    except (requests.RequestException, RuntimeError, ValueError, TypeError) as exc:
        flash("Meta sync failed: " + str(exc)[:250], "error")
    return redirect(url_for("dashboard"))


@app.post("/integrations/meta/disconnect")
def disconnect_meta():
    db = get_db()
    db.execute("DELETE FROM integration_tokens WHERE provider='meta'")
    db.execute("DELETE FROM meta_ad_accounts")
    db.execute("UPDATE integrations SET status='not_connected', account_label=NULL WHERE provider='meta'")
    db.commit()
    flash("Meta access token removed from this AdPilot instance.", "success")
    return redirect(url_for("integrations"))

@app.route("/campaigns/new", methods=["GET", "POST"])
def new_campaign():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        platform = request.form.get("platform", "").strip()
        if not name or not platform:
            flash("Please enter a campaign name and platform.", "error")
            return render_template("new_campaign.html")
        try:
            values = {
                "spend": float(request.form.get("spend", "0")),
                "impressions": int(request.form.get("impressions", "0")),
                "clicks": int(request.form.get("clicks", "0")),
                "conversions": int(request.form.get("conversions", "0")),
                "revenue": float(request.form.get("revenue", "0")),
            }
            if any(v < 0 for v in values.values()):
                raise ValueError
            if values["clicks"] > values["impressions"] or values["conversions"] > values["clicks"]:
                raise ValueError
        except (ValueError, TypeError):
            flash("Enter valid non-negative numbers. Clicks cannot exceed impressions, and conversions cannot exceed clicks.", "error")
            return render_template("new_campaign.html")
        db = get_db()
        db.execute("""
            INSERT INTO campaigns
            (name, platform, spend, impressions, clicks, conversions, revenue)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (name, platform, values["spend"], values["impressions"],
              values["clicks"], values["conversions"], values["revenue"]))
        db.commit()
        flash("Campaign added successfully.", "success")
        return redirect(url_for("dashboard"))
    return render_template("new_campaign.html")


@app.post("/campaigns/<int:campaign_id>/delete")
def delete_campaign(campaign_id):
    db = get_db()
    result = db.execute("DELETE FROM campaigns WHERE id = ?", (campaign_id,))
    db.commit()
    if result.rowcount == 0:
        abort(404)
    flash("Campaign deleted.", "success")
    return redirect(url_for("dashboard"))


@app.get("/health")
def health():
    return {"status": "ok", "app": "AdPilot"}


init_db()

if __name__ == "__main__":
    app.run(debug=os.environ.get("FLASK_DEBUG") == "1", host="0.0.0.0", port=int(os.environ.get("PORT", "5000")))
