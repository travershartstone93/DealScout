# Sharing DealScout with someone else (e.g. Mum) — via Tailscale

DealScout keeps running on **this** laptop (it uses your Claude Code login). Mum uses it from her own
browser/phone over **Tailscale** — a free private network between your devices. Nothing is exposed to the
public internet. People on your tailnet are recognised automatically (no password); the shared password
exists only as a fallback. Remote users get everything except the terminal pane, which is replaced by an
"Ask DealScout" chat.

## One-time setup (≈10 minutes)

### 1. Install Tailscale on this laptop (needs sudo — run in your own terminal)
    sudo pacman -S tailscale
    sudo systemctl enable --now tailscaled
    sudo tailscale up
The last command prints a login URL — open it and sign in (Google login with your gmail works).

### 2. Serve the dashboard over HTTPS on your tailnet
    tailscale serve --bg --https=443 http://127.0.0.1:5006
    tailscale serve status        # shows the URL, e.g. https://<machine>.<tailnet>.ts.net
`serve` adds the viewer's Tailscale identity to each request, so the app knows it's Mum and lets her in
without a password. Put the URL into `~/dealscout/config.toml` → `[server] public_url = "https://…ts.net"`
(header gets a copy-link button), then `systemctl --user restart dealscout`.
(If HTTPS complains that it's disabled: admin console https://login.tailscale.com/admin/dns → enable
MagicDNS + HTTPS Certificates, then rerun the serve command.)

### 3. Invite Mum to your tailnet
Admin console → **Users** → *Invite users* → her email (free plan allows 3 users). She then:
- installs the Tailscale app on her phone/laptop (App Store / Play Store / tailscale.com/download),
- signs in with that email and accepts the invite,
- opens your https://….ts.net URL. That's it — no password, no codes.
On her phone she can "Add to Home Screen" to get an app-like icon.

### 4. (Optional) restrict who on the tailnet may use the app
`config.toml` → `[server] allowed_tailscale_users = ["you@gmail.com", "mum@gmail.com"]`
(empty list = anyone on your tailnet). The shared password (`./dealscout.sh set-password`) remains a
fallback login for anything that arrives without a Tailscale identity.

## Day to day
- `./dealscout.sh share-status` shows tailscale + serve state and the URL.
- Everything she stars/notes/asks is shared with you (one household account); the header shows who's viewing.
- She can press *Scan now*, *Judge with Claude*, *Collect signals*, *Diligence verdict* — those run on your
  laptop and use your Claude quota (daily cap in Settings; header shows *judged today N/M*).
- The laptop must be on and online (sleep = unreachable). Tailscale reconnects by itself.
- Revoke access: remove her from the tailnet (admin console) — instant.

## Security notes (plain language)
- The app still listens only on 127.0.0.1; `tailscale serve` is the only way in, and only your tailnet
  devices can reach it. No router ports opened, no public URL.
- The Tailscale identity header is only trusted when the request comes from tailscaled on this machine —
  it can't be faked from elsewhere.
- Remote users cannot reach the terminal (blocked server-side); 5 wrong passwords → 10-minute lockout.
