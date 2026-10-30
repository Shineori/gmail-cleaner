# Self-hosting this fork

This fork of [Gururagavendra/gmail-cleaner](https://github.com/Gururagavendra/gmail-cleaner) adds four safety features and makes the app safe to run on an always-on server.

| Feature | What it does |
|---|---|
| 🛡️ **Protected senders** | Addresses or domains (`njit.edu`, `boss@company.com`) that Delete, Archive, Mark as Read and Unsubscribe always skip, even when selected. Enforced on the server twice: excluded from the Gmail search, then every message's real `From` is re-checked before anything changes. If the list file is unreadable, actions refuse to run. |
| ↩ **Undo + history** | Every trash / archive / mark-read run is saved with the exact message IDs it touched (`data/history.json`, last 50). Undo puts them back. Trash undo works for 30 days (Gmail's Trash limit). |
| 👀 **Preview** | Before anything runs you see the exact count, a per-sender breakdown, which protected senders will be skipped, and sample subject lines. Nothing changes until you confirm. |
| 📭 **Unsubscribe tracker** | Remembers every unsubscribe (`data/unsubscribed.json`) and checks Gmail for mail that arrives after it. Senders get a 14-day grace period (the US legal limit is 10 business days); after that they show ✅ Worked or 🚨 Still sending. Scan results show the same badges, and a "Trash their old emails" button follows every unsubscribe. |
| 🔐 **Login gate** | Optional [Cloudflare Access](https://developers.cloudflare.com/cloudflare-one/applications/) check (use Authentik or any IdP behind it). The app verifies the signed Access token on every request, so going around the tunnel straight to the port gets a 403. Cross-site POSTs are always blocked. |

Smaller changes: date picker served locally (no CDN), hardened Dockerfile (non-root, pinned `uv`, no dev deps, healthcheck), `/healthz` endpoint, fixes for sideways scrolling on phones.

---

## Option A: on your own computer

```bash
git clone https://github.com/<you>/gmail-cleaner.git && cd gmail-cleaner
# put credentials.json here (see "Google OAuth client" below)
mkdir data
docker compose up -d --build
# open http://localhost:8766, sign in, clean up
docker compose down
```

Ports are bound to `127.0.0.1`, so nothing else on your network can reach it.

---

## Option B: on a server, behind Cloudflare Access

```
 Browser ──HTTPS──► Cloudflare Access ──► Cloudflare Tunnel ──► server:8766 ──► Gmail API
                    (login via your IdP,     (no open ports)      (app re-checks
                     adds a signed JWT)                            the JWT itself)
```

### 1. Google OAuth client

1. [Google Cloud Console](https://console.cloud.google.com/) → new project → enable the **Gmail API**.
2. OAuth consent screen → External → add yourself as a test user.
3. Credentials → Create OAuth client ID → **Desktop app** → download JSON → save as `credentials.json`.

> **Testing vs Production:** while the consent screen is in *Testing*, Google expires the refresh token after 7 days, so you'll sign in again weekly. Switching it to *In production* (unverified is fine for personal use, you click past one warning) removes that limit.

### 2. Server folder

```bash
git clone https://github.com/<you>/gmail-cleaner.git ~/docker/gmail-cleaner
cd ~/docker/gmail-cleaner
cp /path/to/credentials.json .
mkdir data && sudo chown 1000:1000 data     # the container runs as uid 1000
cat > .env <<'EOF'
BIND_IP=192.168.x.x
ACCESS_TEAM_DOMAIN=yourteam.cloudflareaccess.com
ACCESS_AUD=paste-the-aud-tag-here
ACCESS_ALLOWED_EMAILS=you@example.com
EOF
chmod 600 .env credentials.json
```

### 3. Cloudflare Access application

Zero Trust dashboard → **Access → Applications → Add → Self-hosted**:

- **Domain:** `gmail.yourdomain.com`
- **Identity providers:** only your IdP (e.g. Authentik), turn on *Instant Auth*
- **Session duration:** 24h (or shorter)
- **Policy:** Allow, Include → Emails → your address
- After saving, copy the **Application Audience (AUD) Tag** into `ACCESS_AUD`
- Your team domain (Settings → Custom pages) goes in `ACCESS_TEAM_DOMAIN`

No bypass rule is needed: nothing but your browser talks to this app.

### 4. Tunnel route

Tunnel → Public hostname → `gmail.yourdomain.com` → `http://BIND_IP:8766`.

### 5. Start it

```bash
docker compose -f docker-compose.server.yml up -d --build
docker compose -f docker-compose.server.yml ps        # should say (healthy)
curl -s -o /dev/null -w '%{http_code}\n' http://BIND_IP:8766/   # 403 = gate is working
```

### 6. Sign in to Gmail

1. Open `https://gmail.yourdomain.com`, log in through Access, click **Sign in with Google**.
2. Click **Open Google sign-in** and approve.
3. Google sends you to `http://localhost:8767/?state=...&code=...`. That page won't load, which is expected.
4. Copy that full address, paste it into the box in the app, click **Finish sign-in**.

The token is saved in `data/token.json`. The pasted address only works for the sign-in that's currently waiting (CSRF `state` check), within 5 minutes.

### 7. Monitoring

`GET /healthz` returns `{"status":"ok"}` without a login, for Uptime Kuma or similar. Point the monitor at the LAN address, not the public one.

---

## Files in `data/`

| File | Contents |
|---|---|
| `token.json` | Gmail OAuth token. Treat like a password. |
| `protected_senders.json` | `{"senders": [...]}` |
| `history.json` | Last 50 actions with message IDs, for undo |
| `unsubscribed.json` | Who you unsubscribed from and when, for the tracker |

Back up the whole folder if you care about the history and protected list.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Every page is `403: sign in through your Cloudflare Access...` | `ACCESS_AUD` or `ACCESS_TEAM_DOMAIN` is wrong, or the container can't reach `https://<team>/cdn-cgi/access/certs`. Check `docker logs gmail-cleaner` for "Blocked request". |
| "This address is from a different sign-in attempt" | You pasted an older redirect. Click Sign in again and use the newest link. |
| "No sign-in is waiting" | The 5-minute window ran out. Click Sign in again. |
| `PermissionError` writing `data/...` | `sudo chown 1000:1000 data` |
| Signed out every 7 days | Consent screen is in Testing mode (see step 1). |
| Stuck on "Code received, finishing sign-in..." and the logs say `Gmail API has not been used in project ...` | The Gmail API isn't enabled in your Google Cloud project. Enable it from the link in the log, wait a few minutes, reload. |
| "Could not get the Google link" / "Can't read credentials.json" | The container runs as uid 1000: `sudo chown 1000:1000 credentials.json` |
| "Protected senders list is unreadable" | `data/protected_senders.json` is corrupted. Fix or delete it; actions stay blocked until then, on purpose. |
