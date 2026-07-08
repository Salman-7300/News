#!/usr/bin/env python3
"""
KI-Digest (GitHub-Actions-Version, serverlos)

Läuft einmal pro Ausführung komplett durch:
  1. Feeds aus feeds.yaml holen und nach Interessen ranken
  2. Top-Einträge per Claude API auf Deutsch zusammenfassen
     (ohne API-Key: einfache Linkliste als Fallback)
  3. Digest als Markdown in digests/ ablegen
  4. Statisches Dashboard nach docs/index.html rendern (GitHub Pages)
  5. Telegram-Push senden

Alle Secrets kommen aus Umgebungsvariablen (GitHub Actions Secrets):
  ANTHROPIC_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
"""
import hashlib
import html
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import mktime
from zoneinfo import ZoneInfo

import feedparser
import httpx
import yaml

BASE = Path(__file__).resolve().parent
DIGEST_DIR = BASE / "digests"
DOCS_DIR = BASE / "docs"
TZ = ZoneInfo(os.environ.get("TIMEZONE", "Europe/Berlin"))

LOOKBACK_HOURS = int(os.environ.get("LOOKBACK_HOURS", "26"))
MAX_ITEMS = int(os.environ.get("MAX_ITEMS", "25"))
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")

USER_AGENT = "KI-Digest/1.0 (personal serverless feed reader)"
TAG_RE = re.compile(r"<[^>]+>")

SYSTEM_PROMPT = (
    "Du bist ein persönlicher Tech-Nachrichten-Kurator für einen IT-Profi "
    "(Interessen: KI/LLMs, Selfhosting, Python/FastAPI, Automatisierung, Gamedev, IT-Security). "
    "Erstelle aus den gelieferten Einträgen einen kompakten deutschen Morgen-Digest.\n\n"
    "Regeln:\n"
    "- Gruppiere nach den vorhandenen Kategorien, jede Kategorie als '## Kategorie'-Überschrift.\n"
    "- Pro Eintrag genau eine Zeile: '- **Titel** – 1 Satz, warum es relevant ist. [Link](URL)'\n"
    "- Wähle nur die wirklich interessanten Einträge aus (max. 4 pro Kategorie), Rest weglassen.\n"
    "- Starte mit einer Zeile '**Top-Story:** ...' für den wichtigsten Eintrag des Tages.\n"
    "- Kein Vorwort, kein Nachwort, keine Erfindungen – nur Inhalte aus den Einträgen.\n"
    "- Übernimm die URLs exakt und unverändert."
)


# ── 1. Feeds holen & ranken ────────────────────────────────────────────────
def clean(text: str) -> str:
    return re.sub(r"\s+", " ", TAG_RE.sub(" ", text or "")).strip()


def entry_published(entry) -> datetime | None:
    for attr in ("published_parsed", "updated_parsed"):
        parsed = getattr(entry, attr, None)
        if parsed:
            return datetime.fromtimestamp(mktime(parsed), tz=timezone.utc)
    return None


def fetch_items() -> list[dict]:
    cfg = yaml.safe_load((BASE / "feeds.yaml").read_text(encoding="utf-8"))
    interests = [kw.lower() for kw in cfg.get("interests", [])]
    cutoff = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
    seen: set[str] = set()
    items: list[dict] = []

    with httpx.Client(timeout=20, headers={"User-Agent": USER_AGENT},
                      follow_redirects=True) as client:
        for feed in cfg["feeds"]:
            try:
                resp = client.get(feed["url"])
                resp.raise_for_status()
            except Exception as exc:
                print(f"WARN: Feed '{feed['name']}' nicht erreichbar: {exc}", file=sys.stderr)
                continue

            for entry in feedparser.parse(resp.content).entries[:30]:
                link = getattr(entry, "link", "") or ""
                title = clean(getattr(entry, "title", ""))
                if not link or not title:
                    continue
                guid = hashlib.sha256(link.encode()).hexdigest()
                if guid in seen:
                    continue
                published = entry_published(entry) or datetime.now(timezone.utc)
                if published < cutoff:
                    continue
                summary = clean(getattr(entry, "summary", ""))[:600]
                haystack = f"{title} {summary}".lower()
                hits = sum(1 for kw in interests if kw in haystack)
                seen.add(guid)
                items.append({
                    "title": title, "link": link, "summary": summary,
                    "source": feed["name"], "category": feed.get("category", "Sonstiges"),
                    "score": round(float(feed.get("weight", 1.0)) + hits * 0.5, 2),
                    "published": published.isoformat(),
                })

    items.sort(key=lambda x: (x["score"], x["published"]), reverse=True)
    print(f"{len(items)} Einträge im Zeitfenster, Top {MAX_ITEMS} gehen ins Digest")
    return items[:MAX_ITEMS]


# ── 2. Zusammenfassen (Claude API, sonst Plain-Fallback) ───────────────────
def summarize(items: list[dict]) -> tuple[str, str]:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    lines = [
        f"[{it['category']}] {it['title']}\n"
        f"Quelle: {it['source']} | URL: {it['link']}\n"
        f"Teaser: {it['summary'][:300]}\n"
        for it in items
    ]
    user_prompt = "Hier die heutigen Einträge:\n\n" + "\n".join(lines)

    if api_key:
        try:
            resp = httpx.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": api_key, "anthropic-version": "2023-06-01",
                         "content-type": "application/json"},
                json={"model": ANTHROPIC_MODEL, "max_tokens": 2500,
                      "system": SYSTEM_PROMPT,
                      "messages": [{"role": "user", "content": user_prompt}]},
                timeout=120,
            )
            resp.raise_for_status()
            return resp.json()["content"][0]["text"].strip(), f"anthropic:{ANTHROPIC_MODEL}"
        except Exception as exc:
            print(f"WARN: Claude API fehlgeschlagen: {exc}", file=sys.stderr)

    out = ["**Top-Themen (ohne KI-Zusammenfassung):**\n"]
    current = None
    for it in sorted(items, key=lambda x: x["category"]):
        if it["category"] != current:
            current = it["category"]
            out.append(f"\n## {current}")
        out.append(f"- **{it['title']}** [Link]({it['link']})")
    return "\n".join(out), "fallback:plain"


# ── 3. Digest speichern ────────────────────────────────────────────────────
def save_digest(md: str, item_count: int, llm: str) -> Path:
    DIGEST_DIR.mkdir(exist_ok=True)
    now = datetime.now(TZ)
    path = DIGEST_DIR / f"{now.strftime('%Y-%m-%d')}.md"
    meta = {"created": now.isoformat(), "items": item_count, "llm": llm}
    path.write_text(f"<!-- {json.dumps(meta)} -->\n{md}\n", encoding="utf-8")
    print(f"Digest gespeichert: {path.name}")
    return path


# ── 4. Statisches Dashboard rendern ────────────────────────────────────────
_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_BOLD = re.compile(r"\*\*([^*]+)\*\*")
_META = re.compile(r"^<!-- (\{.*\}) -->")


def md_to_safe_html(md: str) -> str:
    out = []
    for raw in md.splitlines():
        line = html.escape(raw)
        line = _LINK.sub(r'<a href="\2" target="_blank" rel="noopener noreferrer">\1</a>', line)
        line = _BOLD.sub(r"<strong>\1</strong>", line)
        if line.startswith("## "):
            out.append(f"<h2>{line[3:].strip()}</h2>")
        elif line.startswith("- "):
            out.append(f"<li>{line[2:]}</li>")
        elif line.strip():
            out.append(f"<p>{line}</p>")
    return "\n".join(out)


def render_dashboard() -> None:
    DOCS_DIR.mkdir(exist_ok=True)
    template = (BASE / "template.html").read_text(encoding="utf-8")
    articles = []

    for path in sorted(DIGEST_DIR.glob("*.md"), reverse=True)[:14]:
        text = path.read_text(encoding="utf-8")
        meta = {"created": path.stem, "items": "?", "llm": "?"}
        m = _META.match(text)
        if m:
            meta.update(json.loads(m.group(1)))
            text = text[m.end():]
        try:
            created = datetime.fromisoformat(meta["created"]).strftime("%a, %d.%m.%Y %H:%M")
        except (ValueError, TypeError):
            created = path.stem
        is_first = not articles
        articles.append(
            f'<details{" open" if is_first else ""}><article>'
            f'<summary>{html.escape(created)}'
            f'<span class="badge">{html.escape(str(meta["items"]))} Quellen-Items · '
            f'{html.escape(str(meta["llm"]))}</span></summary>'
            f'<div class="content">{md_to_safe_html(text)}</div></article></details>'
        )

    body = "\n".join(articles) if articles else '<div class="empty">Noch kein Digest vorhanden.</div>'
    stamp = datetime.now(TZ).strftime("%d.%m.%Y %H:%M")
    page = template.replace("<!--DIGESTS-->", body).replace("<!--UPDATED-->", stamp)
    (DOCS_DIR / "index.html").write_text(page, encoding="utf-8")
    (DOCS_DIR / ".nojekyll").write_text("", encoding="utf-8")
    print(f"Dashboard gerendert: docs/index.html ({len(articles)} Digests)")


# ── 5. Telegram-Push ───────────────────────────────────────────────────────
def md_to_tg_html(md: str) -> str:
    out = []
    for line in md.splitlines():
        if line.startswith("## "):
            out.append(f"\n<b>— {html.escape(line[3:].strip())} —</b>")
            continue
        links, bolds = [], []
        tmp = _LINK.sub(lambda m: (links.append((html.escape(m.group(1)), m.group(2))),
                                   f"\x00L{len(links) - 1}\x00")[1], line)
        tmp = _BOLD.sub(lambda m: (bolds.append(html.escape(m.group(1))),
                                   f"\x00B{len(bolds) - 1}\x00")[1], tmp)
        tmp = html.escape(tmp)
        for i, (text, url) in enumerate(links):
            tmp = tmp.replace(f"\x00L{i}\x00", f'<a href="{url}">{text}</a>')
        for i, text in enumerate(bolds):
            tmp = tmp.replace(f"\x00B{i}\x00", f"<b>{text}</b>")
        out.append(tmp)
    return "\n".join(out).strip()


def send_telegram(md: str) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        print("Telegram nicht konfiguriert – Push übersprungen")
        return

    header = f"☕ Dein Tech-Digest – {datetime.now(TZ).strftime('%A, %d.%m.%Y')}"
    body = f"<b>{html.escape(header)}</b>\n\n{md_to_tg_html(md)}"

    chunks, current = [], ""
    for line in body.splitlines(keepends=True):
        if len(current) + len(line) > 4000:
            chunks.append(current)
            current = ""
        current += line
    if current.strip():
        chunks.append(current)

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    for chunk in chunks:
        resp = httpx.post(url, json={"chat_id": chat_id, "text": chunk,
                                     "parse_mode": "HTML",
                                     "disable_web_page_preview": True}, timeout=30)
        if resp.status_code != 200:
            print(f"WARN: Telegram-Fehler {resp.status_code}: {resp.text[:300]}",
                  file=sys.stderr)
            return
    print("Digest per Telegram versendet")


# ── Main ───────────────────────────────────────────────────────────────────
def main() -> int:
    items = fetch_items()
    if not items:
        print("Keine Einträge im Zeitfenster – Abbruch ohne Digest")
        return 0
    md, llm = summarize(items)
    save_digest(md, len(items), llm)
    render_dashboard()
    send_telegram(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
