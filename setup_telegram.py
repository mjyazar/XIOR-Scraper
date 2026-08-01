#!/usr/bin/env python3
"""
Find your Telegram chat id, with diagnostics for the usual failure modes.

    python3 setup_telegram.py

Run it with no arguments and it prompts for the token with hidden input, which
keeps it out of your shell history. You can also pass it directly if you prefer:

    python3 setup_telegram.py 8123456789:AAHx9y_YourRealTokenHere

Message the bot from every Telegram account you want alerted, then run this -
it prints a combined TELEGRAM_CHAT_ID covering all of them.
"""

import json
import re
import sys
import urllib.request

HINT = """
How to get the token:
  1. Open Telegram, message @BotFather
  2. Send /newbot and follow the prompts
  3. BotFather replies with a line like:
       8123456789:AAHx9y_SomeLongStringOfLettersAndNumbers
     That whole line is the token - copy all of it.
"""


def api(token, method):
    url = f"https://api.telegram.org/bot{token}/{method}"
    try:
        with urllib.request.urlopen(url, timeout=20) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode())
        except Exception:
            return {"ok": False, "error_code": e.code, "description": str(e)}
    except Exception as e:
        return {"ok": False, "error_code": 0, "description": str(e)}


def main():
    if len(sys.argv) >= 2:
        token = sys.argv[1].strip()
    else:
        # Prompted rather than passed as an argument: nothing to fumble, and the
        # token stays out of shell history (~/.zsh_history is a real leak path).
        import getpass
        print("Paste your bot token from @BotFather (input is hidden), then Enter.",
              flush=True)
        try:
            token = getpass.getpass("token: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 2
        if not token:
            print("Nothing entered.")
            print(HINT)
            return 2

    # Catch the mistakes that produce a confusing 404 before calling the API.
    if token.startswith("<") or token.endswith(">"):
        print("!! You pasted the placeholder including the angle brackets.")
        print("   Remove the < > and use your real token.")
        print(HINT)
        return 1
    if token.lower().startswith("bot"):
        # "bot" belongs in the URL, not in the token.
        token = token[3:]
        print("note: stripped a leading 'bot' from the token you supplied")
    if token.startswith("https://") or "/" in token:
        print("!! That looks like a whole URL. Pass only the token itself.")
        print(HINT)
        return 1
    if not re.fullmatch(r"\d{6,}:[A-Za-z0-9_-]{30,}", token):
        print("!! That does not look like a Telegram bot token.")
        print("   Expected: digits, a colon, then ~35 characters.")
        print(f"   Got: {token[:14]}... (length {len(token)})")
        print(HINT)
        return 1

    me = api(token, "getMe")
    if not me.get("ok"):
        code = me.get("error_code")
        print(f"!! getMe failed: {code} {me.get('description')}")
        if code == 401:
            print("   401 = the token is well-formed but not valid.")
            print("   Re-copy it from @BotFather, or /revoke and make a new one.")
        elif code == 404:
            print("   404 = malformed URL path. Check for stray spaces or brackets.")
        return 1

    bot = me["result"]
    print(f"Bot OK: @{bot.get('username')} ({bot.get('first_name')})")

    upd = api(token, "getUpdates")
    if not upd.get("ok"):
        print(f"!! getUpdates failed: {upd.get('error_code')} {upd.get('description')}")
        return 1

    chats = {}
    for u in upd.get("result", []):
        msg = u.get("message") or u.get("channel_post") or u.get("my_chat_member") or {}
        chat = msg.get("chat")
        if chat:
            chats[chat["id"]] = chat

    if not chats:
        print()
        print("No messages found yet - Telegram only reveals your chat id after")
        print(f"you message the bot first.")
        print()
        print(f"  1. Open Telegram and send any message to @{bot.get('username')}")
        print("     (or use this link:  "
              f"https://t.me/{bot.get('username')} )")
        print("  2. Run this script again.")
        return 1

    print()
    print(f"Found {len(chats)} chat(s) that have messaged this bot:")
    for cid, chat in chats.items():
        who = chat.get("username") or chat.get("title") or chat.get("first_name") or ""
        print(f"  {cid}   ({chat.get('type')}  {who})")

    # Several ids are supported, comma separated - the watcher alerts all of
    # them, so every phone/account you own gets the message.
    combined = ",".join(str(c) for c in chats)
    print()
    print("Use this as TELEGRAM_CHAT_ID (alerts every one of them):")
    print()
    print(f"  {combined}")
    print()
    print("Set and test with:")
    print()
    # The token is deliberately not echoed - terminal output gets pasted into
    # chats and issue trackers, and that is how tokens leak.
    print('  export TELEGRAM_BOT_TOKEN="<the token you just passed>"')
    print(f'  export TELEGRAM_CHAT_ID="{combined}"')
    print("  python3 xior_watch.py --test-notify")
    print()
    print("To add another Telegram account: message the bot from that account,")
    print("then run this script again - it will include the new id above.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
