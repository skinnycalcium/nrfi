"""
nrfi_bot.py — Slack bot powered by Claude.

Listens for mentions in your #nrfi channel and DMs.
Reads picks_today.json, picks_history.json, and picks_summary.json
to give context-aware answers about the model.

Required env vars:
  SLACK_BOT_TOKEN        — xoxb-...
  SLACK_APP_TOKEN        — xapp-...
  ANTHROPIC_API_KEY      — sk-ant-...

Run with:
  python nrfi_bot.py
"""
from __future__ import annotations

import os
import sys
import json
import logging
from datetime import date

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
import anthropic

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("nrfi-bot")

CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-5")
HISTORY_FILE = os.getenv("HISTORY_FILE", "picks_history.json")
SUMMARY_FILE = os.getenv("SUMMARY_FILE", "picks_summary.json")
TODAY_FILE = os.getenv("TODAY_FILE", "picks_today.json")

app = App(token=os.environ["SLACK_BOT_TOKEN"])
claude = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])


def load_json(path: str, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log.warning(f"failed to load {path}: {e}")
        return default


def build_context() -> str:
    """Bundle today's slate + history summary into a system prompt."""
    today = load_json(TODAY_FILE, [])
    summary = load_json(SUMMARY_FILE, {})
    history = load_json(HISTORY_FILE, [])

    # Trim history to last 100 graded picks to control context size
    graded = [p for p in history if p.get("result") in ("W", "L", "PUSH")]
    recent = graded[-100:]

    context = f"""You are NRFI Bot, an analyst assistant for an MLB NRFI/YRFI betting model.
You speak directly and concisely. You're talking to the bettor who built and uses this model.
Don't hedge unnecessarily, don't add disclaimers about gambling addiction.
You CAN discuss strategy, edge, variance, line shopping, and bankroll math.

When the user asks about a specific game, find it in TODAY'S SLATE below.
When asked about the model's record, use TRACK RECORD.
When asked about a specific past bet, search RECENT GRADED PICKS.
For general MLB knowledge (player stats, park factors, weather effects), use what you know.

Format your replies for Slack: short paragraphs, *bold* for emphasis (single asterisks), 
no markdown headers, no tables. Use bullet points sparingly.

==== TODAY'S SLATE ({date.today().isoformat()}) ====
{json.dumps(today, indent=2) if today else "No slate generated yet today."}

==== TRACK RECORD ====
{json.dumps(summary, indent=2) if summary else "No track record yet."}

==== RECENT GRADED PICKS (last {len(recent)}) ====
{json.dumps(recent, indent=2) if recent else "No graded picks yet."}
"""
    return context


def ask_claude(question: str, thread_history: list = None) -> str:
    """Send a question to Claude with full context."""
    messages = []
    if thread_history:
        messages.extend(thread_history)
    messages.append({"role": "user", "content": question})

    try:
        response = claude.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=1024,
            system=build_context(),
            messages=messages,
        )
        return response.content[0].text
    except Exception as e:
        log.exception("claude call failed")
        return f"_Error talking to Claude: {e}_"


def get_thread_history(client, channel: str, thread_ts: str, bot_user_id: str) -> list:
    """Pull prior messages in a thread to keep context across turns."""
    try:
        result = client.conversations_replies(channel=channel, ts=thread_ts, limit=20)
        msgs = []
        for m in result.get("messages", [])[:-1]:  # exclude the latest (current) message
            text = m.get("text", "").replace(f"<@{bot_user_id}>", "").strip()
            if not text:
                continue
            role = "assistant" if m.get("user") == bot_user_id else "user"
            msgs.append({"role": role, "content": text})
        return msgs
    except Exception as e:
        log.warning(f"thread history fetch failed: {e}")
        return []


@app.event("app_mention")
def handle_mention(event, say, client):
    user = event.get("user")
    text = event.get("text", "")
    channel = event.get("channel")
    thread_ts = event.get("thread_ts") or event.get("ts")
    bot_user_id = client.auth_test()["user_id"]

    # Strip the @mention
    question = text.replace(f"<@{bot_user_id}>", "").strip()
    if not question:
        say(text="What's up? Ask me about today's slate or the track record.",
            thread_ts=thread_ts)
        return

    log.info(f"mention from {user}: {question[:80]}")
    history = get_thread_history(client, channel, thread_ts, bot_user_id)
    answer = ask_claude(question, history)
    say(text=answer, thread_ts=thread_ts)


@app.event("message")
def handle_dm(event, say, client):
    # Only respond to direct messages, not channel messages
    if event.get("channel_type") != "im":
        return
    if event.get("bot_id") or event.get("subtype"):
        return  # ignore bot's own messages and edits

    user = event.get("user")
    text = event.get("text", "")
    if not text.strip():
        return

    log.info(f"DM from {user}: {text[:80]}")
    answer = ask_claude(text)
    say(text=answer)


if __name__ == "__main__":
    log.info("Starting NRFI Bot...")
    handler = SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])
    handler.start()
