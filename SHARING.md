# Sharing / remote access — via Tailscale

DealScout keeps running on the host machine (it uses that machine's Claude Code login). Other people
use it from their own browser/phone over **Tailscale** — a free private network between your devices.
Nothing is exposed to the public internet. People on your tailnet are recognised automatically (no
password); the shared password exists only as a fallback. Remote users get everything except the
terminal pane, which is replaced by an "Ask DealScout" chat.

## One-time setup (≈10 minutes)

### 1. Install Tailscale on the host (needs sudo — run in your own terminal)
    sudo pacman -S tailscale        # or your distro's package
    sudo systemctl enable --now tailscaled
    sudo tailscale up
The last command prints a login URL — open it and sign in.

### 2. Serve the dashboard over HTTPS on your tailnet
    tailscale serve --bg --https=443 http://127.0.0.1:5006
    tailscale serve status        # shows the URL, e.g. https://<machine>.<tailnet>.ts.net
`serve` adds the viewer's Tailscale identity to each request, so the app knows who it is and lets them
in without a password. Put the URL into `config.toml` → `[server] public_url = "https://…ts.net"`
(header gets a copy-link button), then restart the app (e.g. `systemctl --user restart dealscout`).
(If HTTPS complains that it's disabled: admin console https://login.tailscale.com/admin/dns → enable
MagicDNS + HTTPS Certificates, then rerun the serve command.)

### 3. Invite the other person to your tailnet
Admin console → **Users** → *Invite users* → their email (free plan allows 3 users). They then:
- install the Tailscale app on their phone/laptop (App Store / Play Store / tailscale.com/download),
- sign in with that email and accept the invite,
- open your https://….ts.net URL. That's it — no password, no codes.
On a phone they can "Add to Home Screen" to get an app-like icon.

### 4. (Optional) restrict who on the tailnet may use the app
`config.toml` → `[server] allowed_tailscale_users = ["you@example.com", "them@example.com"]`
(empty list = anyone on your tailnet). The shared password (`./dealscout.sh set-password`) remains a
fallback login for anything that arrives without a Tailscale identity.

## Day to day
- `./dealscout.sh share-status` shows tailscale + serve state and the URL.
- Everything remote users star/note/ask is shared (one household account); the header shows who's viewing.
- They can press *Scan now*, *Judge with Claude*, *Collect signals*, *Diligence verdict* — those run on
  the host and use its Claude quota (daily cap in Settings; header shows *judged today N/M*).
- The host must be on and online (sleep = unreachable). Tailscale reconnects by itself.
- Revoke access: remove the user from the tailnet (admin console) — instant.

## Security notes (plain language)
- The app still listens only on 127.0.0.1; `tailscale serve` is the only way in, and only your tailnet
  devices can reach it. No router ports opened, no public URL.
- The Tailscale identity header is only trusted when the request comes from tailscaled on this machine —
  it can't be faked from elsewhere.
- Remote users cannot reach the terminal (blocked server-side); 5 wrong passwords → 10-minute lockout.
