#!/usr/bin/env python3
"""
Zambia News Bot
Watches trusted Zambian news sites and posts each new story to a Facebook Page
(headline + source credit + link back to the original article).

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

def is_duplicate(title: str, recent: list[str], threshold: float) -> bool:
    simple = simplify_title(title)
    return any(SequenceMatcher(None, simple, other).ratio() >= threshold for other in recent)


def choose_stories(stories: list[dict], state: dict, settings: dict, limit: int,
                   dated_only: bool = False) -> list[dict]:
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
        if blocked and contains_any(s["title"], blocked):
            state["seen"][s["key"]] = now_utc().isoformat()
            continue
        if s["require_keywords"] and not contains_any(f"{s['title']} {s['summary_text']}", s["require_keywords"]):
            state["seen"][s["key"]] = now_utc().isoformat()
            continue
        if state["failures"].get(s["key"], {}).get("count", 0) >= MAX_FAILURES_PER_STORY:
            continue
        fresh.append(s)

    fresh.sort(key=lambda s: s["published"], reverse=True)  # newest first

    picked, used_sources, picked_titles = [], set(), []

    def try_pick(s: dict) -> None:
        if is_duplicate(s["title"], recent + picked_titles, threshold):
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


# ───────────────────────────── AI writer ─────────────────────────────

AI_DEFAULTS = {
    "enabled": False,
    "provider": "anthropic",          # "anthropic" (Claude) or "gemini"
    "model": "claude-haiku-5-5",
    "min_words": 150,
    "max_words": 250,
    "read_full_article": True,        # read the article page to collect facts
    "combine_sources": True,          # merge facts when several outlets cover the same story
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
5. Length: about {min_words}-{max_words} words for the body. If the source material is short, write a SHORTER post (as little as 60 words) instead of padding or inventing.
6. Do not mention any news outlet, website or journalist.

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
        return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    if provider == "gemini":
        # Google renames models from time to time: try the configured one, then known free Flash models.
        candidates = list(dict.fromkeys([ai["model"]] + GEMINI_FALLBACKS))
        last_error = ""
        for model in candidates:
            r = requests.post(f"{AI_BASE['gemini']}/v1beta/models/{model}:generateContent", timeout=90,
                              headers={"x-goog-api-key": api_key, "content-type": "application/json"},
                              json={
                                  "system_instruction": {"parts": [{"text": system}]},
                                  "contents": [{"role": "user", "parts": [{"text": user}]}],
                                  "generationConfig": {"maxOutputTokens": 2048, "temperature": 0.7},
                              })
            data = r.json() if r.content else {}
            if r.status_code == 404:          # model name not available – try the next one
                last_error = f"model '{model}' not found"
                continue
            if not r.ok:
                raise RuntimeError(f"Gemini API HTTP {r.status_code}: {str(data.get('error', data))[:200]}")
            if model != ai["model"]:
                log(f"   ↪ Gemini model '{ai['model']}' not available – used '{model}' instead")
                ai["model"] = model           # remember for the rest of this run
            parts = (data.get("candidates") or [{}])[0].get("content", {}).get("parts", [])
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


def gather_reports(story: dict, all_stories: list[dict], ai: dict, threshold: float) -> list[dict]:
    """The story itself plus (optionally) the same story from up to 2 other outlets."""
    group = [story]
    if ai.get("combine_sources"):
        simple = simplify_title(story["title"])
        for other in all_stories:
            if len(group) >= 3:
                break
            if other["source"] in {g["source"] for g in group}:
                continue
            if SequenceMatcher(None, simple, simplify_title(other["title"])).ratio() >= threshold:
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

    for attempt in (1, 2):
        raw = call_ai(ai, api_key, system, user)
        headline, body = clean_ai_output(raw)
        words = len(body.split())
        copies = copied_phrases(f"{headline}\n{body}", sources)
        if headline and 30 <= words <= int(ai["max_words"]) * 1.5 and copies == 0:
            log(f"   ✍️  AI wrote {words} words from {len(reports)} report(s)")
            return headline, body
        problem = (f"it copied {copies} phrase(s) from the source" if copies
                   else f"the length was {words} words")
        log(f"   ↪ AI draft rejected ({problem}) – {'retrying' if attempt == 1 else 'giving up'}")
        user += (f"\n\nYour previous draft was rejected because {problem}. "
                 "Rewrite it completely in your own words, following every rule.")
    return None


# ───────────────────────────── Facebook ─────────────────────────────

class FacebookError(Exception):
    def __init__(self, message: str, code: int | None = None, subcode: int | None = None):
        super().__init__(message)
        self.code, self.subcode = code, subcode


FATAL_CODES = {190, 102, 10, 200, 210, 230}       # bad/expired token or missing permission
RATE_LIMIT_CODES = {4, 17, 32, 341, 613}           # slow down
SPAM_BLOCK_CODES = {368}                           # Facebook temporarily blocked posting


def post_to_facebook(settings: dict, page_id: str, token: str, message: str, link: str) -> str:
    url = f"{GRAPH_BASE}/{settings['graph_api_version']}/{page_id}/feed"
    r = requests.post(url, data={"message": message, "link": link, "access_token": token}, timeout=40)
    try:
        data = r.json()
    except ValueError:
        data = {}
    if r.ok and data.get("id"):
        return data["id"]
    err = data.get("error", {})
    raise FacebookError(err.get("message") or f"HTTP {r.status_code}: {r.text[:200]}",
                        err.get("code"), err.get("error_subcode"))


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
        to_post = choose_stories(stories, state, settings, limit)

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
        if dry_run:
            print("\n──────── would post ────────")
            print(message)
            print(f"[link preview] {story['link']}")
            print("────────────────────────────")
            continue
        if i:
            time.sleep(SECONDS_BETWEEN_POSTS)
        try:
            post_id = post_to_facebook(settings, page_id, token, message, story["link"])
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
        state["recent_titles"].append({"t": simplify_title(story["title"]), "at": now_utc().isoformat()})
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
