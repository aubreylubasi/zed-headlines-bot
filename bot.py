#!/usr/bin/env python3
"""
Zambia News Bot
Watches trusted Zambian news sites and posts each new story to a Facebook Page
as a branded picture card (made by the bot itself) with the write-up as the caption.

Usage:
  python bot.py              # normal run: fetch, pick new stories, post them
  python bot.py --dry-run    # show what would be posted; posts nothing, saves nothing
  python bot.py --check      # test every news source and report which feeds work

Needs two environment variables for real posting:
  FB_PAGE_ID     – your Facebook Page's numeric ID
  FB_PAGE_TOKEN  – a Page access token with pages_manage_posts permission
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import sys
import time
import tomllib
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import feedparser
import requests

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("BOT_CONFIG", ROOT / "config.toml"))
STATE_PATH = Path(os.environ.get("BOT_STATE", ROOT / "state" / "posted.json"))
GRAPH_BASE = os.environ.get("FB_GRAPH_BASE", "https://graph.facebook.com")

LUSAKA = timezone(timedelta(hours=2), "CAT")  # Zambia: UTC+2, no daylight saving
UTC = timezone.utc

SEEN_KEEP_DAYS = 30
TITLE_MEMORY_HOURS = 48
MAX_FAILURES_PER_STORY = 3
SECONDS_BETWEEN_POSTS = 15

DEFAULT_SETTINGS = {
    "page_name": "My Zambia News Page",
    "max_posts_per_run": 2,
    "max_posts_per_day": 30,
    "max_age_hours": 12,
    "quiet_hours_start": 23,
    "quiet_hours_end": 5,
    "first_run_posts": 1,
    "duplicate_similarity": 0.80,
    "block_keywords": [],
    "post_template": "{title}\n\n{summary}\n\n📰 Source: {source}\n{hashtags}",
    "include_summary": False,
    "summary_max_chars": 220,
    "hashtags": "#Zambia #ZambiaNews",
    "graph_api_version": "v26.0",
}

TRACKING_PARAMS = {"fbclid", "gclid", "amp", "ref", "ref_src", "mc_cid", "mc_eid"}

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (compatible; ZambiaNewsBot/1.0)",
    "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, text/xml;q=0.8, */*;q=0.5",
})


# ───────────────────────────── helpers ─────────────────────────────

def log(msg: str) -> None:
    print(f"[{datetime.now(LUSAKA):%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def now_utc() -> datetime:
    return datetime.now(UTC)


def normalize_url(url: str) -> str:
    """Canonical form of a link so the same story isn't posted twice."""
    parts = urlsplit(url.strip())
    query = [
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not k.lower().startswith("utm_") and k.lower() not in TRACKING_PARAMS
    ]
    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit(("https", host, path, urlencode(query), ""))


def story_key(url: str) -> str:
    return hashlib.sha1(normalize_url(url).encode()).hexdigest()[:16]


def simplify_title(title: str) -> str:
    title = title.lower()
    title = re.sub(r"[^a-z0-9 ]+", " ", title)
    return re.sub(r"\s+", " ", title).strip()


def strip_html(text: str) -> str:
    text = re.sub(r"<(script|style)\b.*?</\1>", " ", text or "", flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def clean_summary(raw: str, title: str, max_chars: int) -> str:
    text = strip_html(raw)
    # WordPress feed footers and "read more" tails
    text = re.sub(r"\bThe post .{0,300}? (first appeared|appeared first) on .{0,80}?\.\s*$", "", text, flags=re.I)
    text = re.sub(r"\s*(\[(…|\.\.\.|&hellip;)\]|Continue reading.*|Read more.*)$", "", text, flags=re.I)
    text = text.strip()
    if not text or simplify_title(text).startswith(simplify_title(title)) and len(text) <= len(title) + 5:
        return ""
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    sentence_end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    if sentence_end > max_chars * 0.5:
        return cut[: sentence_end + 1]
    return cut.rsplit(" ", 1)[0].rstrip(",;:—-") + "…"


def contains_any(text: str, words: list[str]) -> bool:
    return any(re.search(rf"\b{re.escape(w)}\b", text, flags=re.I) for w in words)


def in_quiet_hours(settings: dict, when: datetime) -> bool:
    start, end = int(settings["quiet_hours_start"]), int(settings["quiet_hours_end"])
    if start == end:
        return False
    hour = when.astimezone(LUSAKA).hour
    return (start <= hour < end) if start < end else (hour >= start or hour < end)


# ───────────────────────────── config & state ─────────────────────────────

def load_config() -> tuple[dict, list[dict]]:
    with open(CONFIG_PATH, "rb") as f:
        cfg = tomllib.load(f)
    settings = {**DEFAULT_SETTINGS, **cfg.get("settings", {})}
    settings["ai"] = {**AI_DEFAULTS, **cfg.get("ai", {})}
    settings["image"] = {**IMAGE_DEFAULTS, **cfg.get("image", {})}
    sources = cfg.get("sources", [])
    for s in sources:
        if not s.get("name") or not (s.get("feed") or s.get("site")):
            raise SystemExit(f"config.toml: every source needs a name and a feed or site → {s}")
    return settings, sources


def load_state() -> dict:
    state = {
        "initialized": False,
        "seen": {},            # story key -> ISO time first seen
        "recent_titles": [],   # [{"t": simplified title, "at": ISO time}]
        "daily": {"date": "", "count": 0},
        "feed_cache": {},      # source name -> working feed URL
        "failures": {},        # story key -> {"count": n, "at": ISO time}
        "undated": {},         # story key -> ISO time first noticed (for feeds without dates)
        "last_post": None,
    }
    if STATE_PATH.exists():
        try:
            state.update(json.loads(STATE_PATH.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            log("⚠️  state/posted.json was unreadable – starting fresh.")
    return state


def save_state(state: dict) -> None:
    cutoff_seen = now_utc() - timedelta(days=SEEN_KEEP_DAYS)
    cutoff_titles = now_utc() - timedelta(hours=TITLE_MEMORY_HOURS)
    cutoff_fail = now_utc() - timedelta(days=7)
    state["seen"] = {k: v for k, v in state["seen"].items() if datetime.fromisoformat(v) > cutoff_seen}
    state["recent_titles"] = [t for t in state["recent_titles"] if datetime.fromisoformat(t["at"]) > cutoff_titles]
    state["failures"] = {k: v for k, v in state["failures"].items() if datetime.fromisoformat(v["at"]) > cutoff_fail}
    state["undated"] = {k: v for k, v in state["undated"].items() if datetime.fromisoformat(v) > cutoff_seen}
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=1, ensure_ascii=False, sort_keys=True), encoding="utf-8")


# ───────────────────────────── fetching feeds ─────────────────────────────

def fetch_feed(url: str):
    r = session.get(url, timeout=25)
    r.raise_for_status()
    parsed = feedparser.parse(r.content)
    if not parsed.entries:
        problem = getattr(parsed, "bozo_exception", None)
        raise ValueError(f"no stories found ({problem})" if problem else "no stories found")
    return parsed


def discover_feeds(site: str) -> list[str]:
    """Read the homepage and return any RSS/Atom feed links it advertises."""
    r = session.get(site, timeout=25, headers={"Accept": "text/html,*/*"})
    r.raise_for_status()
    found = []
    for tag in re.findall(r"<link\b[^>]*>", r.text, flags=re.I):
        if re.search(r"type\s*=\s*[\"']application/(rss|atom)\+xml", tag, flags=re.I):
            m = re.search(r"href\s*=\s*[\"']([^\"']+)", tag, flags=re.I)
            if m:
                href = urljoin(site, html.unescape(m.group(1)))
                if "comments" not in href.lower():
                    found.append(href)
    return found


def resolve_feed(source: dict, state: dict):
    """Try the configured feed, then a remembered one, then common WordPress paths,
    then whatever the homepage advertises. Returns (url, parsed_feed)."""
    tried, errors = [], []
    site = source.get("site", "")
    candidates = [source.get("feed"), state["feed_cache"].get(source["name"])]
    if site:
        base = site.rstrip("/")
        candidates += [f"{base}/feed/", f"{base}/?feed=rss2", f"{base}/rss/", f"{base}/rss.xml"]
    for url in filter(None, candidates):
        if url in tried:
            continue
        tried.append(url)
        try:
            return url, fetch_feed(url)
        except Exception as e:  # noqa: BLE001 – any failure means "try the next one"
            errors.append(f"{url} → {short_error(e)}")
    if site:
        try:
            for url in discover_feeds(site):
                if url in tried:
                    continue
                tried.append(url)
                try:
                    return url, fetch_feed(url)
                except Exception as e:  # noqa: BLE001
                    errors.append(f"{url} → {short_error(e)}")
        except Exception as e:  # noqa: BLE001
            errors.append(f"{site} (homepage) → {short_error(e)}")
    raise RuntimeError("; ".join(errors[-3:]) or "no feed address to try")


def short_error(e: Exception) -> str:
    if isinstance(e, requests.HTTPError) and e.response is not None:
        return f"HTTP {e.response.status_code}"
    return f"{type(e).__name__}: {str(e)[:120]}"


def fmt_age(when: datetime) -> str:
    mins = int((now_utc() - when).total_seconds() // 60)
    return f"{mins} min" if mins < 120 else f"{mins // 60} h"


def entry_time(entry) -> datetime | None:
    for field in ("published_parsed", "updated_parsed", "created_parsed"):
        t = entry.get(field)
        if t:
            try:
                return datetime(*t[:6], tzinfo=UTC)
            except (TypeError, ValueError):
                pass
    return None


def collect_stories(sources: list[dict], state: dict, settings: dict) -> list[dict]:
    stories = []
    for source in sources:
        if not source.get("enabled", True):
            continue
        try:
            url, parsed = resolve_feed(source, state)
        except RuntimeError as e:
            log(f"❌ {source['name']}: could not read feed ({e})")
            continue
        if url != source.get("feed"):
            state["feed_cache"][source["name"]] = url
        count = 0
        for entry in parsed.entries:
            link = entry.get("link") or ""
            title = strip_html(entry.get("title") or "")
            if not link.startswith("http") or not title:
                continue
            summary_raw = entry.get("summary") or ""
            if not summary_raw and entry.get("content"):
                summary_raw = entry["content"][0].get("value", "")
            key = story_key(link)
            published = entry_time(entry)
            dated = published is not None
            if published and published > now_utc() + timedelta(hours=1):
                published = now_utc()  # site clock is off
            if not published:
                # No date in the feed: use the first time the bot noticed this story.
                first = state["undated"].setdefault(key, now_utc().isoformat())
                published = datetime.fromisoformat(first)
            stories.append({
                "key": key,
                "dated": dated,
                "link": link,
                "title": title,
                "summary": clean_summary(summary_raw, title, int(settings["summary_max_chars"])),
                "summary_text": strip_html(summary_raw),
                "published": published,
                "source": source["name"],
                "require_keywords": source.get("require_keywords") or [],
            })
            count += 1
        mine = stories[-count:] if count else []
        fresh = sum(1 for s in mine if s["key"] not in state["seen"])
        newest = max((s["published"] for s in mine if s["dated"]), default=None)
        age = f", newest {fmt_age(newest)} old" if newest else ""
        log(f"✅ {source['name']}: {count} stories in feed, {fresh} new{age}")
    return stories


# ───────────────────────────── choosing what to post ─────────────────────────────

STOPWORDS = set("""a an the and or but of to in on at for from by with without as is are was were be been being has have had
it its this that these those he she they his her their them we our you your i not no yes over under after before into out up down
about says said say new will would can could may might must should than then also more most very just amid against via
mr mrs ms dr hon govt government zambia zambian zambians lusaka today week year""".split())


ROLE_WORDS = set("""president vice minister ministry deputy permanent secretary chief mayor party leader leaders opposition
upnd court high police state national district province provincial council official officials spokesperson""".split())


def content_words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9']+", text.lower()) if len(w) > 2 and w not in STOPWORDS}


def name_words(text: str, is_title: bool = False) -> set[str]:
    """Capitalised words (people, places, organisations). Title Case headlines are ignored,
    because there every word is capitalised."""
    words = re.findall(r"[A-Za-z][A-Za-z'-]+", text)
    caps = [w for w in words if w[0].isupper() and w.lower() not in STOPWORDS and len(w) > 2]
    if is_title and words and len(caps) / len(words) > 0.6:
        return set()
    return {w.lower() for w in caps}


def jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def same_story(a: dict, b: dict, threshold: float) -> bool:
    """True when two items from different outlets are about the same event, even if worded differently."""
    ta, tb = a["title"], b["title"]
    if SequenceMatcher(None, simplify_title(ta), simplify_title(tb)).ratio() >= threshold:
        return True
    if jaccard(content_words(ta), content_words(tb)) >= 0.5:
        return True
    full_a = f"{ta}. {a.get('summary_text', '')[:500]}"
    full_b = f"{tb}. {b.get('summary_text', '')[:500]}"
    names_a = name_words(ta, is_title=True) | name_words(a.get("summary_text", "")[:500])
    names_b = name_words(tb, is_title=True) | name_words(b.get("summary_text", "")[:500])
    shared_names = (names_a & names_b) - ROLE_WORDS
    # Same people/places AND the same topic: sharing only a name (e.g. the President) is not enough.
    topic_a = content_words(full_a) - names_a - ROLE_WORDS
    topic_b = content_words(full_b) - names_b - ROLE_WORDS
    shared_topic = topic_a & topic_b
    return (len(shared_names) >= 2 and len(shared_topic) >= 1
            and jaccard(content_words(full_a), content_words(full_b)) >= 0.15)


def is_duplicate(title: str, recent: list[str], threshold: float) -> bool:
    simple = simplify_title(title)
    words = content_words(simple)
    return any(SequenceMatcher(None, simple, other).ratio() >= threshold
               or jaccard(words, content_words(other)) >= 0.5 for other in recent)


def choose_stories(stories: list[dict], state: dict, settings: dict, limit: int,
                   dated_only: bool = False, hold_minutes: float = 0) -> list[dict]:
    max_age = timedelta(hours=float(settings["max_age_hours"]))
    blocked = settings.get("block_keywords") or []
    threshold = float(settings["duplicate_similarity"])
    recent = [t["t"] for t in state["recent_titles"]]

    fresh, seen_this_run = [], set()
    for s in stories:
        if s["key"] in state["seen"] or s["key"] in seen_this_run:
            continue
        seen_this_run.add(s["key"])
        if now_utc() - s["published"] > max_age:
            continue
        if dated_only and not s["dated"]:
            continue
        if blocked and contains_any(f"{s['title']} {s.get('summary_text', '')}", blocked):
            state["seen"][s["key"]] = now_utc().isoformat()
            continue
        if s["require_keywords"] and not contains_any(f"{s['title']} {s['summary_text']}", s["require_keywords"]):
            state["seen"][s["key"]] = now_utc().isoformat()
            continue
        if state["failures"].get(s["key"], {}).get("count", 0) >= MAX_FAILURES_PER_STORY:
            continue
        if hold_minutes and now_utc() - s["published"] < timedelta(minutes=hold_minutes):
            others = [o for o in stories if o["source"] != s["source"] and same_story(s, o, threshold)]
            if not others:
                waited = int((now_utc() - s["published"]).total_seconds() // 60)
                log(f"   ⏳ waiting for other outlets ({waited}/{int(hold_minutes)} min): {s['title'][:70]} ({s['source']})")
                continue
        fresh.append(s)

    fresh.sort(key=lambda s: s["published"], reverse=True)  # newest first

    picked, used_sources, picked_titles = [], set(), []

    recent_items = [{"title": t.get("title") or t["t"], "summary_text": t.get("s", "")}
                    for t in state["recent_titles"]]

    def try_pick(s: dict) -> None:
        if (is_duplicate(s["title"], recent + picked_titles, threshold)
                or any(same_story(s, other, threshold) for other in picked + recent_items)):
            log(f"   ↪ skipping duplicate: {s['title'][:80]} ({s['source']})")
            state["seen"][s["key"]] = now_utc().isoformat()
            return
        picked.append(s)
        picked_titles.append(simplify_title(s["title"]))
        used_sources.add(s["source"])

    # Pass 1: at most one story per source, so one site can't take over the page.
    for s in fresh:
        if len(picked) >= limit:
            break
        if s["source"] not in used_sources:
            try_pick(s)
    # Pass 2: fill any remaining slots.
    for s in fresh:
        if len(picked) >= limit:
            break
        if s not in picked and s["key"] not in state["seen"]:
            try_pick(s)
    return picked


def build_message(story: dict, settings: dict) -> str:
    if story.get("ai_text"):
        summary = story["ai_text"]
    elif settings.get("include_summary"):
        summary = story["summary"]
    else:
        summary = ""
    message = settings["post_template"].format(
        title=story.get("ai_title") or story["title"],
        summary=summary,
        source=story["source"],
        hashtags=settings.get("hashtags", ""),
        link=story["link"],
    )
    message = re.sub(r"[ \t]+\n", "\n", message)
    return re.sub(r"\n{3,}", "\n\n", message).strip()


# ───────────────────────────── picture card ─────────────────────────────
# Each post gets its own picture: a branded Zed Headlines card with the headline on it.
# Made right here with Pillow, so it costs nothing and never uses the news site's photo.
# Optional [image] section in config.toml can change any of these.

IMAGE_DEFAULTS = {
    "enabled": True,
    "brand": "ZED HEADLINES",
    "tagline": "Zambia's news, simply told",
    "footer": "#ZedHeadlines",
    "size": 1080,                 # square, px
    "background": "#0B1F2A",      # deep navy
    "background_2": "#123344",    # bottom of the gradient
    "accent": "#2EA043",          # green bar beside the headline
    "text_color": "#FFFFFF",
    "muted_color": "#9FB3C1",
    "attach_link": False,         # False = no link to the news site in the post
}
ZAMBIA_STRIPES = ["#198A00", "#DE2010", "#000000", "#EF7D00"]   # green, red, black, orange
FONT_BOLD = ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
             "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
             "/System/Library/Fonts/Supplemental/Arial Bold.ttf", "C:/Windows/Fonts/arialbd.ttf"]
FONT_REGULAR = ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
                "/System/Library/Fonts/Supplemental/Arial.ttf", "C:/Windows/Fonts/arial.ttf"]


def load_font(candidates: list[str], size: int):
    from PIL import ImageFont
    for path in candidates:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default(size=size)


def wrap_text(draw, text: str, font, max_width: int) -> list[str]:
    lines, line = [], ""
    for word in text.split():
        trial = f"{line} {word}".strip()
        if draw.textlength(trial, font=font) <= max_width or not line:
            line = trial
        else:
            lines.append(line)
            line = word
    if line:
        lines.append(line)
    return lines


def make_card(headline: str, img: dict, when: datetime) -> bytes:
    """Draw a square branded news card and return it as JPEG bytes."""
    from io import BytesIO
    from PIL import Image, ImageDraw

    W = H = int(img["size"])
    s = W / 1080                                   # scale everything from a 1080px design
    px = lambda v: int(v * s)                      # noqa: E731
    top, bottom = img["background"], img["background_2"]
    card = Image.new("RGB", (W, H), top)
    draw = ImageDraw.Draw(card)

    # soft vertical gradient
    t1 = tuple(int(top[i:i + 2], 16) for i in (1, 3, 5))
    t2 = tuple(int(bottom[i:i + 2], 16) for i in (1, 3, 5))
    for y in range(H):
        f = y / H
        draw.line([(0, y), (W, y)], fill=tuple(int(a + (b - a) * f) for a, b in zip(t1, t2)))

    # header: brand name + Zambian flag stripes
    margin = px(80)
    brand_font = load_font(FONT_BOLD, px(50))
    tag_font = load_font(FONT_REGULAR, px(28))
    draw.text((margin, px(70)), img["brand"], font=brand_font, fill=img["text_color"])
    draw.text((margin, px(135)), img["tagline"], font=tag_font, fill=img["muted_color"])
    stripe_w, stripe_h = px(46), px(12)
    for i, colour in enumerate(ZAMBIA_STRIPES):
        x = W - margin - (len(ZAMBIA_STRIPES) - i) * stripe_w
        draw.rectangle([x, px(88), x + stripe_w - 1, px(88) + stripe_h], fill=colour)

    # headline: biggest font that fits the middle area
    box_top, box_bottom = px(230), H - px(170)
    text_x = margin + px(34)
    max_w = W - text_x - margin
    headline = re.sub(r"\s+", " ", html.unescape(headline)).strip()
    for size in range(96, 39, -4):
        font = load_font(FONT_BOLD, px(size))
        lines = wrap_text(draw, headline, font, max_w)
        line_h = int(px(size) * 1.22)
        if len(lines) * line_h <= box_bottom - box_top:
            break
    else:                                           # extremely long headline: cut it
        lines = lines[: (box_bottom - box_top) // line_h]
        lines[-1] = lines[-1].rstrip(" ,.;:") + "…"
    block_h = len(lines) * line_h
    y = box_top + (box_bottom - box_top - block_h) // 2
    draw.rectangle([margin, y + px(8), margin + px(10), y + block_h - px(8)], fill=img["accent"])
    for line in lines:
        draw.text((text_x, y), line, font=font, fill=img["text_color"])
        y += line_h

    # footer: date + hashtag
    foot_font = load_font(FONT_REGULAR, px(30))
    draw.line([(margin, H - px(130)), (W - margin, H - px(130))], fill=img["muted_color"], width=max(1, px(2)))
    local = when.astimezone(LUSAKA)
    draw.text((margin, H - px(105)), f"{local.day} {local:%B %Y}", font=foot_font, fill=img["muted_color"])
    fw = draw.textlength(img["footer"], font=foot_font)
    draw.text((W - margin - fw, H - px(105)), img["footer"], font=foot_font, fill=img["text_color"])

    out = BytesIO()
    card.save(out, format="JPEG", quality=92, optimize=True)
    return out.getvalue()


# ───────────────────────────── AI writer ─────────────────────────────

AI_DEFAULTS = {
    "enabled": False,
    "provider": "anthropic",          # "anthropic" (Claude) or "gemini"
    "model": "claude-haiku-5-5",
    "min_words": 150,
    "max_words": 250,
    "read_full_article": True,        # read the article page to collect facts
    "combine_sources": True,          # merge facts when several outlets cover the same story
    "wait_minutes": 45,               # wait this long for other outlets before posting a single-source story
}
AI_BASE = {
    "anthropic": os.environ.get("ANTHROPIC_BASE", "https://api.anthropic.com"),
    "gemini": os.environ.get("GEMINI_BASE", "https://generativelanguage.googleapis.com"),
}
GEMINI_FALLBACKS = ["gemini-3.5-flash", "gemini-3.1-flash-lite", "gemini-flash-latest", "gemini-2.5-flash"]
COPY_NGRAM = 10        # this many identical words in a row counts as copying
BOILERPLATE = re.compile(
    r"(share this|related posts?|related articles?|copyright|all rights reserved|follow us|subscribe|"
    r"click here|read more|advertisement|appeared first on|first appeared on|leave a comment|"
    r"sign up|newsletter|whatsapp channel|join our)", re.I)

AI_SYSTEM_PROMPT = """You are the news writer for Zed Headlines, a Zambian Facebook news page.
Write ONE original news post based only on the source material you are given.

Strict rules:
1. Facts: use ONLY facts stated in the source material. Never invent or guess names, titles, numbers, dates, places, quotes, reactions or background. If a detail is unclear, leave it out.
2. Originality: write entirely in your own words and your own structure. Never copy a sentence, and never reuse more than 6 words in a row from the sources (names, job titles and official names of things are fine). Do not use direct quotes; report what people said in your own words ("The minister said that...").
3. Fairness: stay neutral. No opinions, no praise or criticism of politicians, parties, companies or people. Report claims as claims and attribute them ("according to police", "the ministry said").
4. Style: clear, lively plain English for Zambian readers. Start with a strong opening sentence that tells readers why the story matters. Short paragraphs of 1-3 sentences. No clickbait, no exaggeration, no ALL CAPS, no markdown, no hashtags, no links, no emojis.
5. Several reports: if you are given more than one report, combine their facts into one story. Compare them: where they agree, state the fact plainly; where they differ (numbers, names, dates, who said what), say that accounts differ ("one report puts the figure at X, another at Y") instead of silently choosing one.
6. Length: about {min_words}-{max_words} words for the body. If the source material is short, write a SHORTER post (as little as 60 words) instead of padding or inventing.
7. Do not mention any news outlet, website or journalist by name; you may say "reports" in general.
8. Output ONLY the finished post. No notes, explanations, numbered lists, source references, word counts or comments about the rules.

Output format, exactly:
Line 1: a short, accurate, catchy headline (max 12 words).
Line 2: empty.
Then the body paragraphs.
Final line: one short, neutral question inviting readers to comment."""


def fetch_article_text(url: str, max_chars: int = 6000) -> str:
    """Pull the readable paragraphs out of an article page (used only as fact material for the AI)."""
    r = session.get(url, timeout=25, headers={"Accept": "text/html,*/*"})
    r.raise_for_status()
    page = r.text
    page = re.sub(r"<(script|style|noscript|nav|footer|aside|form|header)\b.*?</\1>", " ", page, flags=re.S | re.I)
    body = page
    for pattern in (r"<article\b.*?</article>",
                    r"<div[^>]+class=[\"'][^\"']*(entry-content|post-content|td-post-content|article-content|story-content)[^\"']*[\"'].*?</div>\s*</div>"):
        m = re.search(pattern, page, flags=re.S | re.I)
        if m:
            body = m.group(0)
            break
    paras = []
    for p in re.findall(r"<p\b[^>]*>(.*?)</p>", body, flags=re.S | re.I):
        text = strip_html(p)
        if len(text) >= 40 and not BOILERPLATE.search(text):
            paras.append(text)
    text = "\n".join(dict.fromkeys(paras))  # drop repeated paragraphs, keep order
    return text[:max_chars]


def word_ngrams(text: str, n: int) -> set[tuple[str, ...]]:
    words = re.findall(r"[a-z0-9']+", text.lower())
    return {tuple(words[i:i + n]) for i in range(len(words) - n + 1)}


def copied_phrases(output: str, sources: list[str], n: int = COPY_NGRAM) -> int:
    src = set()
    for s in sources:
        src |= word_ngrams(s, n)
    return len(word_ngrams(output, n) & src)


def call_ai(ai: dict, api_key: str, system: str, user: str) -> str:
    provider = ai["provider"].lower()
    if provider == "anthropic":
        r = requests.post(f"{AI_BASE['anthropic']}/v1/messages", timeout=90, headers={
            "x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json",
        }, json={
            "model": ai["model"], "max_tokens": 1200, "temperature": 0.7,
            "system": system, "messages": [{"role": "user", "content": user}],
        })
        data = r.json() if r.content else {}
        if not r.ok:
            raise RuntimeError(f"Claude API HTTP {r.status_code}: {str(data.get('error', data))[:200]}")
        if data.get("stop_reason") == "max_tokens":
            raise RuntimeError("Claude stopped early (max_tokens)")
        return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    if provider == "gemini":
        # Google renames models from time to time: try the configured one, then known free Flash models.
        candidates = list(dict.fromkeys([ai["model"]] + GEMINI_FALLBACKS))
        last_error = ""
        for model in candidates:
            gen = {"maxOutputTokens": 8192, "temperature": 0.7}
            # Ask newer "thinking" models to think briefly – much faster, and plenty for a news post.
            thinking = ({"thinkingLevel": "low"} if not model.startswith("gemini-2")
                        else {"thinkingBudget": 512})
            body = {
                "system_instruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": user}]}],
                "generationConfig": {**gen, "thinkingConfig": thinking},
            }
            r = requests.post(f"{AI_BASE['gemini']}/v1beta/models/{model}:generateContent", timeout=150,
                              headers={"x-goog-api-key": api_key, "content-type": "application/json"}, json=body)
            if r.status_code == 400 and "think" in r.text.lower():
                body["generationConfig"] = gen          # model doesn't accept the setting – send without it
                r = requests.post(f"{AI_BASE['gemini']}/v1beta/models/{model}:generateContent", timeout=150,
                                  headers={"x-goog-api-key": api_key, "content-type": "application/json"},
                                  json=body)
            data = r.json() if r.content else {}
            if r.status_code == 404:          # model name not available – try the next one
                last_error = f"model '{model}' not found"
                continue
            if not r.ok:
                raise RuntimeError(f"Gemini API HTTP {r.status_code}: {str(data.get('error', data))[:200]}")
            if model != ai["model"]:
                log(f"   ↪ Gemini model '{ai['model']}' not available – used '{model}' instead")
                ai["model"] = model           # remember for the rest of this run
            cand = (data.get("candidates") or [{}])[0]
            if cand.get("finishReason") not in (None, "STOP"):
                raise RuntimeError(f"Gemini stopped early ({cand.get('finishReason')})")
            parts = cand.get("content", {}).get("parts", [])
            return "".join(p.get("text", "") for p in parts if not p.get("thought"))
        raise RuntimeError(f"Gemini: no working model ({last_error})")
    raise RuntimeError(f"Unknown AI provider '{ai['provider']}' (use 'anthropic' or 'gemini')")


def clean_ai_output(text: str) -> tuple[str, str]:
    """Return (headline, body). Removes markdown, links and hashtags the model might add anyway."""
    text = text.replace("\r", "").strip()
    text = re.sub(r"\*\*|__|^#+\s*", "", text, flags=re.M)          # markdown
    text = re.sub(r"https?://\S+", "", text)                          # links
    text = re.sub(r"(^|\s)#\w+", r"\1", text)                         # hashtags
    text = re.sub(r"^(headline|title)\s*:\s*", "", text, flags=re.I)
    lines = text.split("\n")
    headline = lines[0].strip().strip('"“”').rstrip(".")
    body = "\n".join(lines[1:]).strip()
    body = re.sub(r"\n{3,}", "\n\n", body)
    return headline, body


def post_problem(headline: str, body: str, source_chars: int, max_words: int) -> str | None:
    """Return a reason if the AI output doesn't look like a clean, finished post."""
    words = len(body.split())
    min_words = 60 if source_chars >= 600 else 25
    lines = [l.strip() for l in body.split("\n") if l.strip()]
    if not headline or not (3 <= len(headline.split()) <= 16):
        return "the headline was missing or the wrong length"
    if any(c in headline for c in '"“”()[]'):
        return "the headline contained quotes or notes"
    if words < min_words or words > max_words * 1.5:
        return f"the length was {words} words"
    if re.search(r"\(source|\bsource:|\breport \d|word count|\bnote:", f"{headline}\n{body}", re.I):
        return "it included notes or source references"
    if any(re.match(r"^(\d+[.)]|[-•*])\s", l) for l in lines):
        return "it contained a list instead of paragraphs"
    if not lines or not lines[-1].endswith("?"):
        return "it didn't end with a question (it may have been cut off)"
    if not re.search(r"[.!?]$", lines[-2] if len(lines) > 1 else lines[-1]):
        return "a paragraph was cut off mid-sentence"
    return None


def gather_reports(story: dict, all_stories: list[dict], ai: dict, threshold: float) -> list[dict]:
    """The story itself plus (optionally) the same story from up to 2 other outlets."""
    group = [story]
    if ai.get("combine_sources"):
        for other in all_stories:
            if len(group) >= 3:
                break
            if other["source"] in {g["source"] for g in group}:
                continue
            if same_story(story, other, threshold):
                group.append(other)
    reports = []
    for s in group:
        text = ""
        if ai.get("read_full_article"):
            try:
                text = fetch_article_text(s["link"])
            except Exception as e:  # noqa: BLE001
                log(f"   ↪ couldn't read article page ({short_error(e)}) – using feed teaser")
        if len(text) < 200:
            text = clean_summary(s.get("summary_text", ""), s["title"], 2000) or text
        reports.append({"headline": s["title"], "text": text})
    return reports


def ai_write(story: dict, all_stories: list[dict], settings: dict, ai: dict, api_key: str) -> tuple[str, str] | None:
    reports = gather_reports(story, all_stories, ai, float(settings["duplicate_similarity"]))
    material = "\n\n".join(
        f"REPORT {i}\nHeadline: {r['headline']}\nText: {r['text'] or '(no text available)'}"
        for i, r in enumerate(reports, 1))
    total_source = sum(len(r["text"]) for r in reports)
    system = AI_SYSTEM_PROMPT.format(min_words=ai["min_words"], max_words=ai["max_words"])
    user = (f"Source material ({len(reports)} report{'s' if len(reports) > 1 else ''}, "
            f"{total_source} characters):\n\n{material}\n\nWrite the post now.")
    sources = [r["text"] for r in reports] + [r["headline"] for r in reports]

    base_user = user
    for attempt in (1, 2, 3):
        try:
            raw = call_ai(ai, api_key, system, user)
        except requests.Timeout:
            log("   ↪ AI took too long – trying once more")
            if attempt >= 2:
                return None
            continue
        except RuntimeError as e:
            if "stopped early" not in str(e):
                raise
            raw = ""
        headline, body = clean_ai_output(raw) if raw else ("", "")
        copies = copied_phrases(f"{headline}\n{body}", sources) if raw else 0
        problem = (f"it copied {copies} phrase(s) from the source" if copies
                   else post_problem(headline, body, total_source, int(ai["max_words"])) if raw
                   else "the answer was cut off")
        if not problem:
            log(f"   ✍️  AI wrote {len(body.split())} words from {len(reports)} report(s)")
            return headline, body
        log(f"   ↪ AI draft rejected ({problem}) – {'retrying' if attempt < 3 else 'giving up, posting headline only'}")
        # Start each retry from the original request so the model never sees its own bad draft.
        user = (base_user + f"\n\nImportant: an earlier attempt was rejected because {problem}. "
                "Write a completely fresh post in your own words. Output only the post.")
    return None


# ───────────────────────────── Facebook ─────────────────────────────

class FacebookError(Exception):
    def __init__(self, message: str, code: int | None = None, subcode: int | None = None):
        super().__init__(message)
        self.code, self.subcode = code, subcode


FATAL_CODES = {190, 102, 10, 200, 210, 230}       # bad/expired token or missing permission
RATE_LIMIT_CODES = {4, 17, 32, 341, 613}           # slow down
SPAM_BLOCK_CODES = {368}                           # Facebook temporarily blocked posting


def _facebook_result(r: requests.Response) -> str:
    try:
        data = r.json()
    except ValueError:
        data = {}
    if r.ok and (data.get("post_id") or data.get("id")):
        return data.get("post_id") or data["id"]
    err = data.get("error", {})
    raise FacebookError(err.get("message") or f"HTTP {r.status_code}: {r.text[:200]}",
                        err.get("code"), err.get("error_subcode"))


def post_to_facebook(settings: dict, page_id: str, token: str, message: str, link: str | None) -> str:
    """Text post (with a link preview only if a link is given)."""
    url = f"{GRAPH_BASE}/{settings['graph_api_version']}/{page_id}/feed"
    data = {"message": message, "access_token": token}
    if link:
        data["link"] = link
    return _facebook_result(requests.post(url, data=data, timeout=40))


def post_photo_to_facebook(settings: dict, page_id: str, token: str, caption: str, image: bytes) -> str:
    """Picture post: the card is the photo, the write-up is the caption."""
    url = f"{GRAPH_BASE}/{settings['graph_api_version']}/{page_id}/photos"
    r = requests.post(url, data={"caption": caption, "published": "true", "access_token": token},
                      files={"source": ("zed-headlines.jpg", image, "image/jpeg")}, timeout=90)
    return _facebook_result(r)


# ───────────────────────────── modes ─────────────────────────────

def run_check(sources: list[dict]) -> int:
    state = load_state()
    ok = 0
    print(f"\nChecking {len(sources)} sources…\n")
    for source in sources:
        flag = "" if source.get("enabled", True) else "  (disabled in config)"
        try:
            url, parsed = resolve_feed(source, state)
        except RuntimeError as e:
            print(f"❌ {source['name']}{flag}\n     {e}\n")
            continue
        ok += 1
        dated = [t for t in (entry_time(e) for e in parsed.entries) if t]
        newest = max(dated) if dated else None
        age = f"{(now_utc() - newest).total_seconds() / 3600:.1f} h ago" if newest else "no dates in feed"
        first = strip_html(parsed.entries[0].get("title", ""))[:90]
        note = "" if url == source.get("feed") else "   ← found automatically; you can put this in config.toml"
        print(f"✅ {source['name']}{flag}\n     feed: {url}{note}\n"
              f"     {len(parsed.entries)} stories, newest {age}\n     latest: {first}\n")
    print(f"{ok}/{len(sources)} sources working.")
    return 0


def run(dry_run: bool) -> int:
    settings, sources = load_config()
    state = load_state()
    page_id = os.environ.get("FB_PAGE_ID", "").strip()
    token = os.environ.get("FB_PAGE_TOKEN", "").strip()
    if not dry_run and (not page_id or not token):
        log("❌ FB_PAGE_ID and FB_PAGE_TOKEN are not set. Add them as GitHub secrets (see README).")
        return 1

    now = now_utc()
    quiet = in_quiet_hours(settings, now)
    if quiet and dry_run:
        log("🌙 It's quiet hours, so a real run would post nothing now – showing what it would pick anyway.")
    elif quiet and state["initialized"]:
        log(f"🌙 Quiet hours ({settings['quiet_hours_start']}:00–{settings['quiet_hours_end']}:00 Lusaka) – not posting.")
        return 0

    today = now.astimezone(LUSAKA).date().isoformat()
    if state["daily"].get("date") != today:
        state["daily"] = {"date": today, "count": 0}
    left_today = int(settings["max_posts_per_day"]) - state["daily"]["count"]

    log(f"📡 Checking sources for '{settings['page_name']}'" + (" [DRY RUN]" if dry_run else ""))
    stories = collect_stories(sources, state, settings)
    if not stories:
        log("No stories could be read from any source.")
        if not dry_run:
            save_state(state)
        return 0

    if not state["initialized"]:
        limit = 0 if (quiet and not dry_run) else int(settings["first_run_posts"])
        to_post = choose_stories(stories, state, settings, limit, dated_only=True)
        for s in stories:
            if s not in to_post:
                state["seen"][s["key"]] = now.isoformat()
        log(f"🆕 First run: marked {len(stories) - len(to_post)} existing stories as seen; "
            f"posting the newest {len(to_post)}.")
        state["initialized"] = True
    else:
        limit = max(0, min(int(settings["max_posts_per_run"]), left_today))
        if limit == 0:
            log(f"Daily limit of {settings['max_posts_per_day']} posts reached – waiting for tomorrow.")
            if not dry_run:
                save_state(state)
            return 0
        ai_cfg = settings["ai"]
        hold = (float(ai_cfg.get("wait_minutes", 0))
                if ai_cfg.get("enabled") and ai_cfg.get("combine_sources") and os.environ.get("AI_API_KEY") else 0)
        to_post = choose_stories(stories, state, settings, limit, hold_minutes=hold)

    if not to_post:
        log("Nothing new to post – the sites haven't published anything since the last run.")

    ai = settings["ai"]
    ai_key = os.environ.get("AI_API_KEY", "").strip()
    use_ai = bool(ai.get("enabled")) and bool(to_post)
    if use_ai and not ai_key:
        log("⚠️  AI writer is enabled in config.toml but the AI_API_KEY secret is missing – posting headlines only.")
        use_ai = False

    exit_code = 0
    for i, story in enumerate(to_post):
        if use_ai:
            log(f"✍️  Writing article for: {story['title'][:80]}")
            try:
                written = ai_write(story, stories, settings, ai, ai_key)
            except Exception as e:  # noqa: BLE001 – never let the AI stop the posting
                log(f"   ↪ AI writer failed ({short_error(e)}) – posting without the write-up")
                written = None
            if written:
                story["ai_title"], story["ai_text"] = written
        message = build_message(story, settings)
        img_cfg = settings["image"]
        link = story["link"] if img_cfg.get("attach_link") else None
        card = None
        if img_cfg.get("enabled"):
            try:
                card = make_card(story.get("ai_title") or story["title"], img_cfg, now_utc())
            except Exception as e:  # noqa: BLE001 – never let the picture stop the posting
                log(f"   ↪ couldn't make the picture card ({short_error(e)}) – posting text only")
        if dry_run:
            print("\n──────── would post ────────")
            print(message)
            if card:
                preview = ROOT / "preview" / f"card-{i + 1}.jpg"
                preview.parent.mkdir(exist_ok=True)
                preview.write_bytes(card)
                print(f"[picture card] saved to {preview}")
            if link:
                print(f"[link preview] {link}")
            print("────────────────────────────")
            continue
        if i:
            time.sleep(SECONDS_BETWEEN_POSTS)
        try:
            if card:
                post_id = post_photo_to_facebook(settings, page_id, token, message, card)
            else:
                post_id = post_to_facebook(settings, page_id, token, message, link)
        except FacebookError as e:
            if e.code in FATAL_CODES:
                log(f"❌ Facebook rejected the token/permissions (code {e.code}): {e}")
                log("   Get a new Page token (README → step 3) and update the FB_PAGE_TOKEN secret.")
                exit_code = 1
                break
            if e.code in SPAM_BLOCK_CODES:
                log(f"⛔ Facebook has temporarily blocked posting (code {e.code}): {e}")
                log("   Lower max_posts_per_run / max_posts_per_day in config.toml.")
                exit_code = 1
                break
            if e.code in RATE_LIMIT_CODES:
                log(f"⏳ Facebook rate limit (code {e.code}) – will try again next run.")
                break
            fail = state["failures"].get(story["key"], {"count": 0})
            state["failures"][story["key"]] = {"count": fail["count"] + 1, "at": now_utc().isoformat()}
            log(f"⚠️  Could not post '{story['title'][:70]}' (code {e.code}): {e}")
            continue
        except requests.RequestException as e:
            log(f"⚠️  Network problem talking to Facebook: {short_error(e)} – will retry next run.")
            break
        state["seen"][story["key"]] = now_utc().isoformat()
        state["recent_titles"].append({"t": simplify_title(story["title"]), "title": story["title"],
                                       "s": story.get("summary_text", "")[:500], "at": now_utc().isoformat()})
        for other in stories:   # the same story from other outlets is now covered too
            if other is not story and other["source"] != story["source"] and \
                    same_story(story, other, float(settings["duplicate_similarity"])):
                state["seen"][other["key"]] = now_utc().isoformat()
        state["daily"]["count"] += 1
        state["last_post"] = {"id": post_id, "title": story["title"], "at": now_utc().isoformat()}
        log(f"📤 Posted: {story['title'][:90]}  ({story['source']})  → post {post_id}")

    if dry_run:
        log("Dry run finished – nothing was posted and nothing was saved.")
    else:
        save_state(state)
    return exit_code


def main() -> int:
    p = argparse.ArgumentParser(description="Auto-post Zambian news to a Facebook Page.")
    p.add_argument("--dry-run", action="store_true", help="show what would be posted, post nothing")
    p.add_argument("--check", action="store_true", help="test every news source")
    args = p.parse_args()
    dry = args.dry_run or os.environ.get("DRY_RUN", "").lower() in {"1", "true", "yes"}
    check = args.check or os.environ.get("CHECK_SOURCES", "").lower() in {"1", "true", "yes"}
    if check:
        _, sources = load_config()
        return run_check(sources)
    return run(dry)


if __name__ == "__main__":
    sys.exit(main())
