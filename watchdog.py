from datetime import datetime
import discord
import json
import os
import math
import tempfile
import gzip
import shutil
import urllib.request as urlreq
import uuid
import requests
from discord.ext import tasks
from collections import defaultdict, deque, Counter
import asyncio

# --- Spam watchdog config ---
SPAM_REPORT_CHANNEL_ID = {CHANNEL_ID HERE}

SPAM_WINDOW_SECONDS = 9
SPAM_MIN_MESSAGES = 3
SPAM_MIN_CHANNELS = 3

SPAM_REQUIRE_DUPLICATE_PAYLOAD = True
SPAM_MIN_DUPLICATES = 3

SPAM_ACTION_COOLDOWN_SECONDS = 60

_recent_user_messages = defaultdict(lambda: deque())
_last_spam_action = {}  # (guild_id, user_id) -> datetime

# --- Scam / solicitation pitch watchdog ---
SCAM_PITCH_ENABLED = True

SCAM_PITCH_MIN_TEXT_LEN = 280          # long pitchy posts
SCAM_PITCH_MIN_SCORE = 7               # tune this
SCAM_PITCH_NEW_MEMBER_MAX_DAYS = 14    # only punish new joiners

# If you want to only enforce in certain channels, set this list.
# Leave empty to enforce everywhere except SPAM_REPORT_CHANNEL_ID.
SCAM_PITCH_CHANNEL_ALLOWLIST = []  # e.g. [123, 456]

SCAM_PITCH_PHRASES = [
    "open to projects",
    "open to roles",
    "looking for paid",
    "long-term contracts",
    "full-time roles",
    "hiring",
    "dm me",
    "d*m me",
    "message me",
    "reach out",
]

SCAM_PITCH_KEYWORDS = [
    # common buzzwords in these scams
    "blockchain",
    "web3",
    "defi",
    "nft",
    "dao",
    "solidity",
    "rust",
    "evm",
    "solana",
    "ai",
    "llm",
    "rag",
    "autonomous",
    "agents",
    "workflow automation",
    "multimodal",
    "saas",
]

# Load the config file
with open('config.json') as config_file:
    config = json.load(config_file)

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

# Constants
COLUMNS = 3  # Number of columns to display
COLUMNS_ALIAS = 2  # Number of columns to display for aliases
EMBED_TIMEOUT = 60  # Timeout in seconds

def generate_session_hash():
    return str(uuid.uuid4())[:8]  # Generate a short unique hash

@client.event
async def on_ready():
    print(f'Logged in as {client.user}!')

@client.event
async def on_message(message):
    if message.author == client.user:
        return

    # --- Spam watchdog (ban + report) ---
    try:
        if await spam_watchdog(message):
            return
    except Exception as e:
        # Don’t let watchdog errors break the bot
        print(f"Spam watchdog error: {e}")

    # Handle publishing messages in a specific channel
    if message.channel.id == {CHANNEL_ID HERE}:
        try:
            await message.publish()
            print(f"Published message {message.id} in channel {message.channel.id}")
        except Exception as e:
            print(f"Failed to publish message {message.id} in channel {message.channel.id}: {e}")
        return

    message_content = message.content.strip()
    if not message_content:
        return

    # Normalize the message content to lower case
    message_content_lower = message_content.lower()

        # List of valid prefixes
    prefixes = ['!']

    # Check for commands anywhere in the message
    words = message_content_lower.split()

    for word in words:
        if any(word.startswith(prefix) for prefix in prefixes):
            # Identify the prefix used
            for prefix in prefixes:
                if word.startswith(prefix):
                    command = word[len(prefix):].strip()
                    break

            if command == 'ping':
                before = datetime.utcnow()
                msg = await message.channel.send("🏓 Pong?")
                after = datetime.utcnow()

                rtt_ms = (after - before).total_seconds() * 1000
                ws_ms = client.latency * 1000

                await msg.edit(
                    content=f"🏓 **Pong!**\n"
                            f"WebSocket latency: `{ws_ms:.1f} ms`\n"
                            f"Round-trip latency: `{rtt_ms:.1f} ms`"
                )
                return

async def send_long_message(channel, text):
    while len(text) > 2000:
        split_index = text.rfind('\n', 0, 2000)
        if split_index == -1:
            split_index = 2000
        await channel.send(text[:split_index])
        text = text[split_index:].lstrip('\n')

    if text:
        await channel.send(text)

def _now_utc():
    return datetime.utcnow()

def _normalize_text(s: str) -> str:
    s = (s or "").strip().lower()
    s = " ".join(s.split())
    return s

def _attachment_sig(att: discord.Attachment) -> str:
    # No file hashing needed; metadata is enough for “same image pasted everywhere”
    # (filename/size/content_type are stable in typical spam)
    return f"{att.filename}|{att.size}|{att.content_type or ''}"

def _message_payload_signature(message: discord.Message) -> str:
    """
    One string representing what was posted:
    - normalized text (if any)
    - attachment metadata (if any)
    - embed urls (rare, but helps)
    """
    parts = []

    txt = _normalize_text(message.content)
    if txt:
        parts.append(f"txt:{txt}")

    if message.attachments:
        atts = ",".join(_attachment_sig(a) for a in message.attachments)
        parts.append(f"att:{atts}")

    # sometimes spam comes as embeds (link previews)
    if message.embeds:
        urls = []
        for e in message.embeds:
            if getattr(e, "url", None):
                urls.append(e.url)
        if urls:
            parts.append("emb:" + ",".join(urls))

    return " || ".join(parts)

async def _get_channel_safe(channel_id: int):
    ch = client.get_channel(channel_id)
    if ch:
        return ch
    try:
        return await client.fetch_channel(channel_id)
    except Exception:
        return None

async def _delete_message_by_id(guild: discord.Guild, channel_id: int, message_id: int) -> bool:
    ch = guild.get_channel(channel_id)
    if ch is None:
        try:
            ch = await guild.fetch_channel(channel_id)
        except Exception:
            return False

    try:
        msg = await ch.fetch_message(message_id)
        await msg.delete()
        return True
    except discord.NotFound:
        return True  # already gone is fine
    except discord.Forbidden:
        return False
    except Exception:
        return False

async def _ban_and_report_for_spam(message: discord.Message, evidence: list[dict], reason: str):
    guild = message.guild
    if not guild:
        return

    # 1) Delete evidence messages first (best-effort)
    deleted = 0
    failed_delete = 0
    for e in evidence:
        ok = await _delete_message_by_id(guild, e["channel_id"], e["message_id"])
        if ok:
            deleted += 1
        else:
            failed_delete += 1

    # 2) Softban: Ban (purge) then Unban (so it's effectively a kick + cleanup)
    ban_error = None
    unban_error = None

    try:
        try:
            # discord.py newer
            await guild.ban(message.author, reason=reason, delete_message_seconds=3600)
        except TypeError:
            # discord.py older
            await guild.ban(message.author, reason=reason, delete_message_days=1)
    except Exception as e:
        ban_error = e

    if ban_error is None:
        # Small delay helps avoid occasional race conditions between ban/unban
        await asyncio.sleep(1)

        try:
            # Use an Object by ID so this works even if Member object is stale post-ban
            await guild.unban(discord.Object(id=message.author.id), reason=f"Softban release: {reason}")
        except Exception as e:
            unban_error = e

    # 3) Report (and include whether unban succeeded)
    report_ch = await _get_channel_safe(SPAM_REPORT_CHANNEL_ID)
    if not report_ch:
        return

    if ban_error is not None:
        embed = discord.Embed(title="Spam watchdog: softban failed (ban step)", color=discord.Color.red())
        embed.add_field(name="User", value=f"{message.author} ({message.author.id})", inline=False)
        embed.add_field(name="Reason", value=reason, inline=False)
        embed.add_field(name="Delete results", value=f"deleted={deleted}, failed={failed_delete}", inline=False)
        embed.add_field(name="Error", value=str(ban_error)[:1024], inline=False)
        await report_ch.send(embed=embed)
        return

    chan_ids = [e["channel_id"] for e in evidence]
    unique_channels = sorted(set(chan_ids))
    channel_mentions = ", ".join(f"<#{cid}>" for cid in unique_channels[:25]) or "None"

    links = [e.get("jump_url") for e in evidence if e.get("jump_url")]
    payloads = [e.get("payload_sig") for e in evidence if e.get("payload_sig")]
    sample_payload = payloads[-1] if payloads else None

    title = "Spam watchdog: user softbanned"
    color = discord.Color.orange()

    embed = discord.Embed(title=title, color=color)
    embed.add_field(name="User", value=f"{message.author} (<@{message.author.id}>)", inline=False)
    embed.add_field(name="Reason", value=reason, inline=False)
    embed.add_field(name="Delete results", value=f"deleted={deleted}, failed={failed_delete}", inline=False)
    embed.add_field(name="Channels hit (window)", value=channel_mentions[:1024], inline=False)

    if unban_error is None:
        embed.add_field(name="Unban", value="✅ Unbanned (softban complete)", inline=False)
    else:
        embed.add_field(
            name="Unban",
            value=f"⚠️ Unban failed — user may still be banned\n{str(unban_error)[:900]}",
            inline=False
        )

    if links:
        embed.add_field(name="Message links", value="\n".join(links[:10])[:1024], inline=False)

    if sample_payload:
        embed.add_field(name="Sample payload", value=sample_payload[:1024], inline=False)

    await report_ch.send(embed=embed)

async def spam_watchdog(message: discord.Message) -> bool:
    if not message.guild:
        return False
    if message.author.bot:
        return False
    if message.channel.id == SPAM_REPORT_CHANNEL_ID:
        return False

    # avoid banning staff/mods
    perms = message.author.guild_permissions
    if perms.administrator or perms.manage_guild or perms.manage_messages or perms.ban_members or perms.kick_members:
        return False

    now = _now_utc()
    key = (message.guild.id, message.author.id)

    last = _last_spam_action.get(key)
    if last and (now - last).total_seconds() < SPAM_ACTION_COOLDOWN_SECONDS:
        return False

    payload_sig = _message_payload_signature(message)
    if not payload_sig:
        return False  # ignore empty/noise

    # --- Scam pitch watchdog (single message) ---
    if SCAM_PITCH_ENABLED:
        if message.channel.id != SPAM_REPORT_CHANNEL_ID and _scam_pitch_allowed_in_channel(message.channel.id):
            member = message.author if isinstance(message.author, discord.Member) else None

            # Guardrails: only auto-action on new members (reduce false positives)
            if member and _is_new_member(member):
                score = _scam_pitch_score(message)
                if score >= SCAM_PITCH_MIN_SCORE:
                    _last_spam_action[key] = now

                    evidence = [{
                        "ts": now,
                        "channel_id": message.channel.id,
                        "message_id": message.id,
                        "jump_url": getattr(message, "jump_url", None),
                        "payload_sig": payload_sig,
                    }]

                    reason = f"Spam watchdog (softban): solicitation/scam pitch heuristic (score={score})"
                    await _ban_and_report_for_spam(message, evidence, reason)
                    return True

    bucket = _recent_user_messages[key]
    bucket.append({
        "ts": now,
        "channel_id": message.channel.id,
        "message_id": message.id,
        "jump_url": getattr(message, "jump_url", None),
        "payload_sig": payload_sig,
    })

    # prune
    window_start = now.timestamp() - SPAM_WINDOW_SECONDS
    while bucket and bucket[0]["ts"].timestamp() < window_start:
        bucket.popleft()

    if len(bucket) < SPAM_MIN_MESSAGES:
        return False

    channels = {e["channel_id"] for e in bucket}
    if len(channels) < SPAM_MIN_CHANNELS:
        return False

    if SPAM_REQUIRE_DUPLICATE_PAYLOAD:
        sigs = [e["payload_sig"] for e in bucket if e.get("payload_sig")]
        most_common = Counter(sigs).most_common(1)[0][1] if sigs else 0
        if most_common < SPAM_MIN_DUPLICATES:
            return False

    _last_spam_action[key] = now

    reason = (
        f"Spam watchdog: {len(bucket)} msgs in {SPAM_WINDOW_SECONDS}s "
        f"across {len(channels)} channels"
        + (f", duplicate_payload={SPAM_MIN_DUPLICATES}+" if SPAM_REQUIRE_DUPLICATE_PAYLOAD else "")
    )

    evidence = list(bucket)
    bucket.clear()

    await _ban_and_report_for_spam(message, evidence, reason)
    return True

def _text_contains_any(text: str, phrases: list[str]) -> bool:
    t = _normalize_text(text)
    return any(p in t for p in phrases)

def _count_hits(text: str, phrases: list[str]) -> int:
    t = _normalize_text(text)
    return sum(1 for p in phrases if p in t)

def _lines_with_colon(text: str) -> int:
    # These scam pitches often have "Blockchain:", "AI:", "Fullstack:" etc.
    lines = (text or "").splitlines()
    return sum(1 for ln in lines if ":" in ln and len(ln.strip()) <= 60)

def _scam_pitch_score(message: discord.Message) -> int:
    """
    Score a single message for solicitation/pitch scam patterns.
    Higher score => more likely scam.
    """
    text = message.content or ""
    t = _normalize_text(text)
    if not t:
        return 0

    score = 0

    # Long, structured pitch
    if len(t) >= SCAM_PITCH_MIN_TEXT_LEN:
        score += 2

    # Contains DM solicitation language
    if _text_contains_any(t, SCAM_PITCH_PHRASES):
        score += 4

    # Lots of buzzwords
    kw_hits = _count_hits(t, SCAM_PITCH_KEYWORDS)
    if kw_hits >= 4:
        score += 3
    elif kw_hits >= 2:
        score += 2
    elif kw_hits >= 1:
        score += 1

    # "Category:" formatting lines
    colons = _lines_with_colon(text)
    if colons >= 3:
        score += 2
    elif colons >= 2:
        score += 1

    # Bullet-ish structure often used
    if "\n" in text and any(prefix in text for prefix in ["•", "-", "—"]):
        score += 1

    return score

def _is_new_member(member: discord.Member) -> bool:
    if not member:
        return False
    if not getattr(member, "joined_at", None):
        return False
    delta = datetime.utcnow() - member.joined_at.replace(tzinfo=None)
    return delta.days <= SCAM_PITCH_NEW_MEMBER_MAX_DAYS

def _scam_pitch_allowed_in_channel(channel_id: int) -> bool:
    if not SCAM_PITCH_CHANNEL_ALLOWLIST:
        return True
    return channel_id in SCAM_PITCH_CHANNEL_ALLOWLIST

# Run the bot
client.run(config['bot_token'])
