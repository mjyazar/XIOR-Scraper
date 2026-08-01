# Zernike availability watcher

Personal notifier for student housing availability in Groningen. Polls the
property pages' own availability endpoint and sends a push notification when a
room is listed, including a direct link to the application form.

Runs 24/7 on free infrastructure.

---

## Quick start

```bash
python3 setup_telegram.py <your-bot-token>   # get your chat id
gh auth login                                # one time
./push_to_github.sh                          # create repo, push, set secrets, test
```

`push_to_github.sh` creates the repo, pushes, grants the workflow write access,
prompts for your notification secrets (hidden input, straight into GitHub's
encrypted store) and fires a test notification. The sections below explain each
step if you'd rather do it by hand.

---

## Deployment

**GitHub Actions is the primary runner.** The endpoint rate-limits per IP
address, and GitHub gives a different runner IP on every run, which spreads the
load naturally. A single always-on server polling from one fixed IP gets
blocked and goes blind — which is the worst outcome, because a blind watcher
looks exactly like "nothing available".

| Option | Cost | Cadence |
|---|---|---|
| **GitHub Actions** | Free (public repo = unlimited minutes) | 5-min cron; each run loops internally → checks every ~90s |
| **Cloudflare Workers** | Free (100k req/day) | Cron every 2 min |

Running both gives two independent watchers on different networks, both free.

---

## Setup

### 1. Notifications

Set up **at least two** channels. One missed push can cost you the room.

**Telegram**

1. Message **@BotFather** → `/newbot` → copy the token it gives you.
2. Message your new bot once (it cannot message you until you do).
3. Run the helper:

```bash
python3 setup_telegram.py 8123456789:AAHx9y_YourRealTokenHere
```

It validates the token, finds your chat id, and prints the exact exports.

> If you hit `404 Not Found` calling the API by hand, the URL path is wrong —
> not the token. `bot` must be glued directly to the token with no `<>`
> brackets: `https://api.telegram.org/bot<token>/getUpdates` becomes
> `https://api.telegram.org/bot8123456789:AAHx.../getUpdates`. A genuinely
> wrong token returns `401`, not `404`. The helper above catches all of this.

**ntfy.sh** (no account needed)

1. Install the **ntfy** app (iOS / Android).
2. Subscribe to a long random topic, e.g. `zernike-a8f3c2d91b4e`.
3. That string is your `NTFY_TOPIC`. Anyone who knows it can read it, so make
   it unguessable.

Test both:

```bash
TELEGRAM_BOT_TOKEN=xxx TELEGRAM_CHAT_ID=yyy NTFY_TOPIC=zzz python3 xior_watch.py --test-notify
```

### 2. GitHub Actions

1. Push this folder to a **public** repo.

   Public matters: public repos get unlimited free Actions minutes. A private
   repo gets 2,000/month and this uses roughly 43,000. Secrets are **not**
   exposed by a public repo — they live in GitHub's encrypted secret store.

2. **Settings → Secrets and variables → Actions → New repository secret**:

   | Secret | |
   |---|---|
   | `TELEGRAM_BOT_TOKEN` | recommended |
   | `TELEGRAM_CHAT_ID` | recommended |
   | `NTFY_TOPIC` | recommended |
   | `DISCORD_WEBHOOK` | optional |

3. **Settings → Actions → General → Workflow permissions** → **Read and write
   permissions** (the workflow commits its state file).

4. **Actions** tab → enable workflows → run **XIOR Zernike watcher** manually
   once with **test notify** ticked to confirm delivery.

> GitHub's scheduler is not punctual; a 5-minute cron can drift by several
> minutes under load. That's expected, and why the second runner is worth
> having.
>
> GitHub also disables scheduled workflows after **60 days of no repo
> activity**. The workflow commits state regularly, which normally counts — if
> it ever stops, push any commit to re-enable.

### 3. Cloudflare Worker (optional second runner)

```bash
cd cloudflare
npm install -g wrangler
wrangler login
wrangler kv namespace create XIOR_STATE     # paste the id into wrangler.toml
wrangler secret put TELEGRAM_BOT_TOKEN
wrangler secret put TELEGRAM_CHAT_ID
wrangler secret put NTFY_TOPIC
wrangler secret put WATCH_SECRET            # protects the manual endpoints
wrangler deploy
```

Endpoints: `/check`, `/state`, `/test` (all require `?key=<WATCH_SECRET>`).
Live logs: `wrangler tail`.

> After deploying, run `wrangler tail` and confirm you see
> `EMPTY (no units (204))` rather than `ERROR (http 403)`. Cloudflare egress
> against this target was not verified during development. If it 403s, drop
> the Worker and rely on GitHub Actions.

---

## What you'll receive

```
ROOM AVAILABLE - Zernike Tower (long stay)

2 room(s) just became available at Zernike Tower (long stay):

- #XXGZERN14 | Comfy | 20m2 | EUR 1370/mo | from 01/02/2027 | [Vacant Unrented Not Ready]
- #XXGZERN22 | Deluxe | 36m2 | EUR 1480/mo | from 01/02/2027 | [Notice Unrented]

Direct booking link:
https://...securerc.co.uk/onlineleasing/...
```

That direct link comes from the API and drops you straight on the application
form, past the modal and room-selection steps.

You are also alerted on:

- **A new booking term opening** — often *before* any room is listed. The
  earliest possible warning that a new intake window is opening.
- **A new room type appearing.**
- **The watcher going blind** (sustained errors or rate limiting), so silence
  is never ambiguous.
- **A daily heartbeat**, so you know it's alive.

---

## Tuning

The poll cadence is set deliberately close to the measured limit of what the
endpoint tolerates. **Tightening it gets you blocked, and a blocked watcher
sees nothing.** To go faster, add another independent watcher on a different
network rather than increasing frequency on one.

On rate limiting the watcher stops calling that property entirely and backs off
exponentially (10 → 20 → 40 → 60 min), and never retries a 429 — retrying is
what keeps you blocked.

---

## Local use

```bash
python3 xior_watch.py                      # one pass
python3 xior_watch.py --loop 300 --interval 60
python3 xior_watch.py --status             # show state, no network
python3 xior_watch.py --test-notify        # verify notifications
```

| Symptom | Meaning | Fix |
|---|---|---|
| `EMPTY (no units (204))` | Working, nothing available | Nothing — this is the normal state |
| `RATELIMITED` | Polling too hard from this IP | Wait; it backs off on its own |
| `ERROR (http 403)` | Transport / edge | Usually transient; persistent = check `curl` exists |
| `BROKEN (upstream 400 ...)` | Stale term id | Delete `state/state.json` to force a fresh scrape |
| No alerts ever | Notifications not wired | `--test-notify` |

`state/state.json` is safe to delete; it rebuilds on the next run. The only
consequence is that currently-listed rooms are re-alerted once.

**This finds rooms; it does not book them.** It deliberately does not submit
applications or enter personal or payment details.

---

## Files

```
xior_watch.py               Watcher (stdlib only, no dependencies)
setup_telegram.py           Finds your Telegram chat id
push_to_github.sh           One-shot repo creation + secrets + test run
config.json                 Targets and tuning
state/state.json            Seen units, cached ids, cooldowns
.github/workflows/watch.yml GitHub Actions runner
cloudflare/worker.js        Cloudflare Worker runner
cloudflare/wrangler.toml    Worker config (paste your KV id here)
```
