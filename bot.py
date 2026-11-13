#!/usr/bin/env python3
"""IELTSATION daily Telegram channel bot.

Once a day: pick today's category -> ask an AI model for a fresh post ->
check it is not a repeat -> publish to the channel -> remember the topic.

Uses only the Python standard library (nothing to install).

Environment variables (set as GitHub secrets):
  TELEGRAM_BOT_TOKEN   token from @BotFather
  TELEGRAM_CHAT_ID     @yourchannel or the numeric -100... id
  AI_API_KEY           key for the provider chosen in config.json

Usage:
  python3 bot.py              post today's category (skips if already posted today)
  python3 bot.py --dry-run    generate and print, do not publish or save
  python3 bot.py --force      post even if something was already posted today
  python3 bot.py --day friday pretend today is Friday (handy with --dry-run)
"""
import argparse
import difflib
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
HISTORY_PATH = ROOT / "history.json"

DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
ALLOWED_TAGS = ("b", "i", "u", "s", "code", "blockquote")
TELEGRAM_LIMIT = 4096
MAX_ATTEMPTS = 4


# ----------------------------------------------------------------- helpers

def log(msg):
    print(msg, flush=True)


def post_json(url, payload, headers=None, label="request", timeout=45):
    """POST JSON, return parsed JSON. Errors never include the URL (it may hold a token)."""
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:600]
        err = RuntimeError(f"{label} failed: HTTP {e.code}: {body}")
        err.status = e.code
        raise err from None
    except (urllib.error.URLError, OSError) as e:  # network trouble, incl. read timeouts
        err = RuntimeError(f"{label} failed: {getattr(e, 'reason', None) or e}")
        err.status = None
        raise err from None


def load_json(path, default):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return default


# ------------------------------------------------------------ AI providers

def ask_gemini(ai, key, system, prompt, model):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    data = post_json(
        url,
        {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 1.0, "responseMimeType": "application/json"},
        },
        headers={"x-goog-api-key": key},
        label=f"Gemini ({model})",
    )
    parts = data["candidates"][0]["content"]["parts"]
    return "".join(p.get("text", "") for p in parts)


def ask_openai_compatible(ai, key, system, prompt, model):
    """Works with OpenAI, Groq, OpenRouter and other OpenAI-style APIs."""
    base = (ai.get("base_url") or "https://api.openai.com/v1").rstrip("/")
    data = post_json(
        f"{base}/chat/completions",
        {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
        },
        headers={"Authorization": f"Bearer {key}"},
        label=f"AI ({model})",
    )
    return data["choices"][0]["message"]["content"]


def ask_anthropic(ai, key, system, prompt, model):
    data = post_json(
        "https://api.anthropic.com/v1/messages",
        {
            "model": model,
            "max_tokens": 2000,
            "system": system,
            "messages": [{"role": "user", "content": prompt}],
        },
        headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
        label=f"Claude ({model})",
    )
    return "".join(b.get("text", "") for b in data["content"] if b.get("type") == "text")


PROVIDERS = {
    "gemini": ask_gemini,
    "openai": ask_openai_compatible,
    "anthropic": ask_anthropic,
}


def ask_ai(ai, key, system, prompt):
    """Try each configured model in order; move on if a model name is retired or busy."""
    fn = PROVIDERS.get(ai.get("provider", "gemini"))
    if fn is None:
        raise SystemExit(f"Unknown ai.provider in config.json. Use one of: {', '.join(PROVIDERS)}")
    models = ai["model"] if isinstance(ai["model"], list) else [ai["model"]]
    last = None
    for model in models:
        try:
            return fn(ai, key, system, prompt, model)
        except RuntimeError as e:
            last = e
            log(f"  ! {str(e).splitlines()[0]}")
            if getattr(e, "status", None) in (401, 403):
                raise  # bad key: no point trying further
            # retired name (400/404), busy (429/5xx) or timeout: try the next model
    raise last


# ------------------------------------------------------------------ prompt

def build_prompts(cfg, day_cfg, avoid_same, avoid_other, rejected):
    style = cfg["style"]
    examples = [e for e in style.get("example_posts", []) if e.strip()]
    translate = style.get("translation_language", "").strip()

    system = [
        f'You write the daily post for the Telegram channel "{cfg["channel_name"]}".',
        f'Audience: {cfg["audience"]}',
        f'Voice: {style["voice"]}',
        "",
        "Rules for every post:",
        f'- Length: {style["min_chars"]}-{style["max_chars"]} characters. One screen, easy to skim.',
        "- First line: one emoji + a short bold title.",
        "- Use emojis as visual bullets and section markers (roughly one per block, never rows of them).",
        "- Formatting is Telegram HTML ONLY: <b>, <i>, <u>, <code>, <blockquote>. "
        "No Markdown (no ** or #), no <br>, no lists tags, no links. Use plain line breaks.",
        "- Every language point needs a natural example sentence.",
        "- Everything must be accurate, standard English. If you are not sure a fact or rule is "
        "correct, choose a different one.",
        "- End with one short question or mini-task that invites comments.",
        "- Do NOT write hashtags inside the post; return them separately.",
        "- Do NOT mention that you are an AI, and do not greet with the weekday every time.",
    ]
    if translate:
        system.append(f"- Add a short {translate} translation in brackets after each key word or phrase.")
    for rule in style.get("extra_rules", []):
        system.append(f"- {rule}")
    if examples:
        system += ["", "Match the style of these real posts from the channel (style only, not content):"]
        for i, ex in enumerate(examples, 1):
            system += [f"--- example {i} ---", ex.strip()]

    user = [
        f'Category: {day_cfg["title"]}',
        f'Brief: {day_cfg["brief"]}',
        "",
        "Pick ONE fresh, specific topic for this category.",
    ]
    if avoid_same:
        user += ["", "Already published in this category - do NOT repeat or closely overlap these:"]
        user += [f"- {t}" for t in avoid_same]
    if avoid_other:
        user += ["", "Recently published in other categories - avoid reusing the same words/points:"]
        user += [f"- {t}" for t in avoid_other]
    if rejected:
        user += ["", "These ideas were just rejected as repeats - choose something clearly different:"]
        user += [f"- {t}" for t in rejected]
    user += [
        "",
        "Reply with JSON only, in exactly this shape:",
        '{"topic": "specific label that names the exact words/rule/question covered, max 12 words", '
        '"post": "the full post in Telegram HTML", '
        '"hashtags": ["#OneTopicTag", "#AnotherTopicTag"]}',
        "Give 1-2 topic hashtags in CamelCase without spaces.",
    ]
    return "\n".join(system), "\n".join(user)


# --------------------------------------------------------- parsing & checks

def parse_reply(text):
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        data = json.loads(text, strict=False)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise ValueError("AI reply was not JSON")
        data = json.loads(text[start : end + 1], strict=False)
    if isinstance(data, list) and data:
        data = data[0]
    topic = str(data.get("topic", "")).strip()
    post = str(data.get("post", "")).strip()
    tags = data.get("hashtags") or []
    if isinstance(tags, str):
        tags = tags.split()
    if not topic or not post:
        raise ValueError("AI reply is missing 'topic' or 'post'")
    return topic, post, [str(t) for t in tags]


def sanitize_html(text):
    """Keep only Telegram-safe tags; escape everything else so Telegram never rejects it."""
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text, flags=re.S)  # stray Markdown bold
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</?(?:p|ul|ol|div|span)>", "", text, flags=re.I)
    text = re.sub(r"<li>", "• ", text, flags=re.I)
    text = re.sub(r"</li>", "", text, flags=re.I)
    text = re.sub(r"<(/?)strong>", r"<\1b>", text, flags=re.I)
    text = re.sub(r"<(/?)em>", r"<\1i>", text, flags=re.I)

    tag_re = re.compile(r"</?(?:%s)>" % "|".join(ALLOWED_TAGS), re.I)
    out, pos = [], 0
    for m in tag_re.finditer(text):
        out.append(html.escape(html.unescape(text[pos : m.start()]), quote=False))
        out.append(m.group(0).lower())
        pos = m.end()
    out.append(html.escape(html.unescape(text[pos:]), quote=False))
    cleaned = "".join(out)
    cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip()


def strip_trailing_hashtags(text):
    """Remove a hashtag line the model added at the end despite instructions."""
    lines = text.rstrip().split("\n")
    while lines and (not lines[-1].strip() or re.fullmatch(r"(\s*#\w+)+\s*", lines[-1])):
        lines.pop()
    return "\n".join(lines)


def clean_tag(tag):
    word = re.sub(r"[^\w]", "", tag, flags=re.UNICODE)
    return f"#{word}" if word else ""


def normalize(topic):
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", topic.lower()).split())


def find_repeat(topic, past_topics, threshold):
    """Return the earlier topic this one repeats, or None."""
    new = normalize(topic)
    new_words = set(new.split())
    for old_raw in past_topics:
        old = normalize(old_raw)
        if not old:
            continue
        if difflib.SequenceMatcher(None, new, old).ratio() >= threshold:
            return old_raw
        old_words = set(old.split())
        overlap = len(new_words & old_words) / max(1, min(len(new_words), len(old_words)))
        if min(len(new_words), len(old_words)) >= 3 and overlap >= 0.8:
            return old_raw
    return None


def join_line(cfg, chat_id):
    """Footer inviting readers to join. Uses join_link from config, or the channel @username."""
    label = cfg.get("join_text", "").strip()
    link = cfg.get("join_link", "").strip() or (chat_id if chat_id.startswith("@") else "")
    if not label or not link:
        if label:
            log("  ! no join link: channel id is not an @username and join_link is empty in config.json")
        return ""
    return html.escape(f"{label} {link}", quote=False)


# ---------------------------------------------------------------- telegram

def send_to_telegram(token, chat_id, text):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    base = {"chat_id": chat_id, "link_preview_options": {"is_disabled": True}}
    try:
        data = post_json(url, {**base, "text": text, "parse_mode": "HTML"}, label="Telegram")
    except RuntimeError as e:
        if getattr(e, "status", None) != 400 or "parse" not in str(e).lower():
            raise
        log("  ! Telegram rejected the formatting; sending as plain text instead")
        plain = html.unescape(re.sub(r"</?[a-z-]+>", "", text))
        data = post_json(url, {**base, "text": plain}, label="Telegram")
    return data["result"]["message_id"]


# -------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description="IELTSATION daily channel bot")
    ap.add_argument("--dry-run", action="store_true", help="generate and print only")
    ap.add_argument("--force", action="store_true", help="post even if already posted today")
    ap.add_argument("--day", choices=DAYS, help="override the weekday")
    args = ap.parse_args()

    cfg = load_json(CONFIG_PATH, None)
    if cfg is None:
        raise SystemExit("config.json not found")
    history = load_json(HISTORY_PATH, {"posts": []})
    posts = history.setdefault("posts", [])

    now = datetime.now(timezone(timedelta(hours=cfg.get("utc_offset_hours", 5))))
    today = now.strftime("%Y-%m-%d")
    day = args.day or DAYS[now.weekday()]
    day_cfg = cfg["schedule"][day]
    category = day_cfg["category"]
    log(f"{today} ({day}) -> {day_cfg['title']}")

    if not args.dry_run and not args.force and any(p["date"] == today for p in posts):
        log("Already posted today - nothing to do. (Use --force to post again.)")
        return

    ai_key = os.environ.get("AI_API_KEY", "").strip()
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    missing = [n for n, v in [("AI_API_KEY", ai_key)] if not v]
    if not args.dry_run:
        missing += [n for n, v in [("TELEGRAM_BOT_TOKEN", token), ("TELEGRAM_CHAT_ID", chat_id)] if not v]
    if missing:
        raise SystemExit("Missing secrets: " + ", ".join(missing))

    same = [p["topic"] for p in posts if p["category"] == category][-150:]
    other = [p["topic"] for p in posts if p["category"] != category][-40:]
    all_topics = [p["topic"] for p in posts]
    threshold = cfg.get("repeat_similarity_threshold", 0.8)

    rejected, final = [], None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        log(f"Generating (attempt {attempt}/{MAX_ATTEMPTS})...")
        system, prompt = build_prompts(cfg, day_cfg, same, other, rejected)
        try:
            topic, post, tags = parse_reply(ask_ai(cfg["ai"], ai_key, system, prompt))
        except (ValueError, KeyError, IndexError, json.JSONDecodeError) as e:
            log(f"  ! unusable reply: {e}")
            continue
        except RuntimeError as e:
            if getattr(e, "status", None) in (401, 403):
                raise
            log("  ! every model is busy right now - waiting 30s before the next attempt")
            time.sleep(30)
            continue

        repeat = find_repeat(topic, all_topics + rejected, threshold)
        if repeat:
            log(f"  ! '{topic}' repeats '{repeat}' - asking again")
            rejected.append(topic)
            continue

        body = strip_trailing_hashtags(sanitize_html(post))
        seen, hashtags = set(), []
        for t in list(day_cfg.get("hashtags", [])) + tags[:2]:
            t = clean_tag(t)
            if t and t.lower() not in seen:
                seen.add(t.lower())
                hashtags.append(t)
        text = body + "\n\n" + " ".join(hashtags)
        join = join_line(cfg, chat_id)
        if join:
            text += "\n\n" + join

        if len(text) > TELEGRAM_LIMIT:
            log(f"  ! post too long ({len(text)} chars) - asking again")
            continue
        final = (topic, text)
        break

    if final is None:
        raise SystemExit("Could not produce a valid, non-repeating post. Nothing was published.")

    topic, text = final
    log(f"Topic: {topic}\n" + "-" * 40 + f"\n{text}\n" + "-" * 40)

    if args.dry_run:
        log("Dry run - not published, history unchanged.")
        return

    message_id = send_to_telegram(token, chat_id, text)
    posts.append({"date": today, "day": day, "category": category, "topic": topic, "message_id": message_id})
    HISTORY_PATH.write_text(json.dumps(history, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    log(f"Published (message {message_id}). History now has {len(posts)} posts.")


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as e:
        sys.exit(f"ERROR: {e}")
