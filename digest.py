#!/usr/bin/env python3
"""
KI-Digest v2 (GitHub-Actions-Version, serverlos)

Neu in v2:
  - Dubletten-Clustering: dieselbe Story aus mehreren Quellen wird zu EINEM
    Eintrag mit allen Quellen-Links zusammengeführt
  - Volltext-Anreicherung: für die Top-3-Einträge wird der Artikel geladen,
    damit die Zusammenfassung substanzieller wird
  - Feedback-Loop: 👍/👎-Buttons unterm Telegram-Push; Reaktionen werden beim
    nächsten Lauf per getUpdates eingesammelt (state/feedback.json) und im
    Dashboard angezeigt. WICHTIG: Für den Bot darf KEIN Webhook gesetzt sein.
  - Freitags-Wochenrückblick: zusätzlicher Digest über die letzten 7 Tage

Secrets: ANTHROPIC_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
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
STATE_DIR = BASE / "state"
TZ = ZoneInfo(os.environ.get("TIMEZONE", "Europe/Berlin"))

LOOKBACK_HOURS = int(os.environ.get("LOOKBACK_HOURS", "26"))
MAX_ITEMS = int(os.environ.get("MAX_ITEMS", "25"))
FULLTEXT_TOP_N = int(os.environ.get("FULLTEXT_TOP_N", "3"))
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")

USER_AGENT = "KI-Digest/2.0 (personal serverless feed reader)"
TAG_RE = re.compile(r"<[^>]+>")

STOPWORDS = {
    "der", "die", "das", "und", "für", "mit", "von", "auf", "ein", "eine", "einen",
    "ist", "sind", "wird", "nach", "beim", "über", "zum", "zur", "the", "and",
    "for", "with", "from", "into", "your", "you", "how", "why", "what", "this",
    "that", "new", "now", "its", "are", "was", "has", "have", "not", "can",
}

SYSTEM_PROMPT = (
    "Du bist ein persönlicher Tech-Nachrichten-Kurator für einen IT-Profi "
    "(Interessen: KI/LLMs, Selfhosting, Python/FastAPI, Automatisierung, Gamedev, IT-Security). "
    "Erstelle aus den gelieferten Einträgen einen kompakten deutschen Morgen-Digest.\n\n"
    "Regeln:\n"
    "- Gruppiere nach den vorhandenen Kategorien, jede Kategorie als '## Kategorie'-Überschrift.\n"
    "- Pro Eintrag genau eine Zeile: '- **Titel** – 1-2 Sätze, warum es relevant ist. [Link](URL)'\n"
    "- Hat ein Eintrag mehrere Quellen-URLs, hänge sie alle an: [Quelle1](URL1) [Quelle2](URL2)\n"
    "- Einträge mit Volltext-Auszug verdienen 2 Sätze mit konkretem Inhalt statt Floskeln.\n"
    "- Wähle nur die wirklich interessanten Einträge aus (max. 4 pro Kategorie), Rest weglassen.\n"
    "- Starte mit einer Zeile '**Top-Story:** ...' für den wichtigsten Eintrag des Tages.\n"
    "- Kein Vorwort, kein Nachwort, keine Erfindungen – nur Inhalte aus den Einträgen.\n"
    "- Übernimm die URLs exakt und unverändert."
)

WEEKLY_PROMPT = (
    "Du bekommst die Tages-Digests der letzten Woche. Erstelle daraus einen deutschen "
    "Wochenrückblick: Was war diese Woche WIRKLICH wichtig?\n"
    "- Maximal 6 Punkte, jeweils '- **Thema** – 1-2 Sätze Einordnung. [Link](URL)'\n"
    "- Beginne mit '**Die Woche in einem Satz:** ...'\n"
    "- Erkenne Entwicklungen über mehrere Tage (z.B. Release Montag, Kritik Mittwoch).\n"
    "- Keine Wiederholung von Boilerplate, keine Erfindungen, URLs unverändert."
)


# ── State-Helfer ───────────────────────────────────────────────────────────
def load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return default


def save_json(path: Path, data) -> None:
    STATE_DIR.mkdir(exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


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
    # Selbstlernende Kategorie-Gewichte aus 👍/👎-Feedback
    cat_weights = load_json(STATE_DIR / "feedback.json", {}).get("cat_weights", {})
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
                category = feed.get("category", "Sonstiges")
                seen.add(guid)
                items.append({
                    "title": title, "link": link, "summary": summary,
                    "source": feed["name"], "category": category,
                    "score": round(float(feed.get("weight", 1.0)) + hits * 0.5
                                   + float(cat_weights.get(category, 0.0)), 2),
                    "published": published.isoformat(),
                    "alt_sources": [],  # wird beim Clustering gefüllt
                    "fulltext": "",
                })

    items.sort(key=lambda x: (x["score"], x["published"]), reverse=True)
    print(f"{len(items)} Einträge im Zeitfenster")
    return items


# ── 2. Dubletten-Clustering (Titel-Wort-Overlap) ───────────────────────────
def title_tokens(title: str) -> set[str]:
    words = re.findall(r"[a-zäöüß0-9\-]{3,}", title.lower())
    return {w for w in words if w not in STOPWORDS}


def cluster_items(items: list[dict], threshold: float = 0.45) -> list[dict]:
    """Jaccard-Ähnlichkeit über Titel-Wörter. Bei Dublette gewinnt der Eintrag
    mit dem höheren Score; die anderen werden als alt_sources angehängt."""
    kept: list[dict] = []
    merged = 0
    for item in items:  # bereits nach Score sortiert
        toks = title_tokens(item["title"])
        match = None
        for k in kept:
            ktoks = title_tokens(k["title"])
            if not toks or not ktoks:
                continue
            jacc = len(toks & ktoks) / len(toks | ktoks)
            if jacc >= threshold:
                match = k
                break
        if match:
            match["alt_sources"].append({"source": item["source"], "link": item["link"]})
            match["score"] = round(match["score"] + 0.3, 2)  # Mehrfachnennung = relevanter
            merged += 1
        else:
            kept.append(item)
    if merged:
        kept.sort(key=lambda x: (x["score"], x["published"]), reverse=True)
        print(f"Clustering: {merged} Dubletten zusammengeführt")
    return kept


# ── 3. Volltext für Top-N nachladen ────────────────────────────────────────
def enrich_fulltext(items: list[dict], top_n: int) -> None:
    with httpx.Client(timeout=15, headers={"User-Agent": USER_AGENT},
                      follow_redirects=True) as client:
        for item in items[:top_n]:
            try:
                resp = client.get(item["link"])
                resp.raise_for_status()
                text = clean(resp.text)
                # grober Boilerplate-Schnitt: erst ab dem Titel-Anfang suchen
                anchor = item["title"][:30].lower()
                pos = text.lower().find(anchor)
                if pos > 0:
                    text = text[pos:]
                item["fulltext"] = text[:2500]
                print(f"Volltext geladen: {item['title'][:50]} ({len(item['fulltext'])} Z.)")
            except Exception as exc:
                print(f"WARN: Volltext für {item['link'][:50]} fehlgeschlagen: {exc}",
                      file=sys.stderr)


# ── 4. Zusammenfassen ──────────────────────────────────────────────────────
def call_claude(system: str, user_prompt: str, max_tokens: int = 2500) -> str | None:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return None
    try:
        resp = httpx.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": api_key, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": ANTHROPIC_MODEL, "max_tokens": max_tokens,
                  "system": system,
                  "messages": [{"role": "user", "content": user_prompt}]},
            timeout=120,
        )
        resp.raise_for_status()
        return resp.json()["content"][0]["text"].strip()
    except Exception as exc:
        print(f"WARN: Claude API fehlgeschlagen: {exc}", file=sys.stderr)
        return None


def summarize(items: list[dict]) -> tuple[str, str]:
    blocks = []
    for it in items:
        alt = "".join(f"\nWeitere Quelle: {a['source']} | {a['link']}"
                      for a in it["alt_sources"])
        full = f"\nVolltext-Auszug: {it['fulltext']}" if it["fulltext"] else ""
        blocks.append(
            f"[{it['category']}] {it['title']}\n"
            f"Quelle: {it['source']} | URL: {it['link']}{alt}\n"
            f"Teaser: {it['summary'][:300]}{full}\n")
    text = call_claude(SYSTEM_PROMPT, "Hier die heutigen Einträge:\n\n" + "\n".join(blocks))
    if text:
        return text, f"anthropic:{ANTHROPIC_MODEL}"

    out = ["**Top-Themen (ohne KI-Zusammenfassung):**\n"]
    current = None
    for it in sorted(items, key=lambda x: x["category"]):
        if it["category"] != current:
            current = it["category"]
            out.append(f"\n## {current}")
        out.append(f"- **{it['title']}** [Link]({it['link']})")
    return "\n".join(out), "fallback:plain"


# ── 5. Digest speichern ────────────────────────────────────────────────────
def save_digest(md: str, item_count: int, llm: str, suffix: str = "") -> Path:
    DIGEST_DIR.mkdir(exist_ok=True)
    now = datetime.now(TZ)
    path = DIGEST_DIR / f"{now.strftime('%Y-%m-%d')}{suffix}.md"
    meta = {"created": now.isoformat(), "items": item_count, "llm": llm,
            "weekly": bool(suffix)}
    path.write_text(f"<!-- {json.dumps(meta)} -->\n{md}\n", encoding="utf-8")
    print(f"Digest gespeichert: {path.name}")
    return path


# ── 6. Feedback einsammeln (👍/👎 vom Vortag) ──────────────────────────────
def collect_feedback(token: str) -> None:
    """Holt Callback-Queries per getUpdates und lernt daraus: 👎 senkt die
    Gewichte der Kategorien des bewerteten Digests, 👍 hebt sie leicht an.
    Funktioniert nur, wenn für den Bot kein Webhook registriert ist."""
    fb = load_json(STATE_DIR / "feedback.json",
                   {"up": 0, "down": 0, "offset": 0, "cat_weights": {}})
    last_cats = load_json(STATE_DIR / "last_categories.json", {})
    try:
        resp = httpx.get(f"https://api.telegram.org/bot{token}/getUpdates",
                         params={"offset": fb.get("offset", 0) + 1,
                                 "allowed_updates": '["callback_query"]',
                                 "timeout": 0},
                         timeout=30)
        resp.raise_for_status()
        updates = resp.json().get("result", [])
    except Exception as exc:
        print(f"WARN: Feedback-Abruf fehlgeschlagen: {exc}", file=sys.stderr)
        return

    weights = fb.setdefault("cat_weights", {})
    for upd in updates:
        fb["offset"] = max(fb.get("offset", 0), upd["update_id"])
        cq = upd.get("callback_query")
        if not cq:
            continue
        data = cq.get("data", "")
        if data not in ("fb:up", "fb:down"):
            continue
        direction = 1 if data == "fb:up" else -1
        fb["up" if direction > 0 else "down"] = fb.get("up" if direction > 0 else "down", 0) + 1
        # Lernen: Anteil der Kategorie am bewerteten Digest bestimmt die Schrittweite
        step = 0.06 if direction < 0 else 0.03  # 👎 wiegt schwerer als 👍
        for cat, share in last_cats.items():
            new = weights.get(cat, 0.0) + direction * step * float(share)
            weights[cat] = round(max(-0.6, min(0.6, new)), 3)
    save_json(STATE_DIR / "feedback.json", fb)
    if updates:
        print(f"Feedback eingesammelt: 👍{fb['up']} / 👎{fb['down']} · "
              f"Gewichte: {weights}")


def save_category_snapshot(items: list[dict]) -> None:
    """Merkt sich die Kategorien-Verteilung des heutigen Digests, damit das
    nächste Feedback weiß, worauf es sich bezieht."""
    if not items:
        return
    counts: dict[str, int] = {}
    for it in items:
        counts[it["category"]] = counts.get(it["category"], 0) + 1
    total = sum(counts.values())
    save_json(STATE_DIR / "last_categories.json",
              {cat: round(n / total, 3) for cat, n in counts.items()})


# ── 7. Telegram-Push (mit Feedback-Buttons) ────────────────────────────────
_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_BOLD = re.compile(r"\*\*([^*]+)\*\*")
_META = re.compile(r"^<!-- (\{.*\}) -->")


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


def send_telegram(md: str, header: str, with_feedback: bool = True) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        print("Telegram nicht konfiguriert – Push übersprungen")
        return

    body = f"<b>{html.escape(header)}</b>\n\n{md_to_tg_html(md)}"
    chunks, current = [], ""
    for line in body.splitlines(keepends=True):
        if len(current) + len(line) > 4000:
            chunks.append(current); current = ""
        current += line
    if current.strip():
        chunks.append(current)

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    for i, chunk in enumerate(chunks):
        payload = {"chat_id": chat_id, "text": chunk, "parse_mode": "HTML",
                   "disable_web_page_preview": True}
        # Buttons nur unter dem letzten Chunk
        if with_feedback and i == len(chunks) - 1:
            payload["reply_markup"] = {"inline_keyboard": [[
                {"text": "👍 Guter Digest", "callback_data": "fb:up"},
                {"text": "👎 Daneben", "callback_data": "fb:down"},
            ]]}
        resp = httpx.post(url, json=payload, timeout=30)
        if resp.status_code != 200:
            print(f"WARN: Telegram-Fehler {resp.status_code}: {resp.text[:300]}",
                  file=sys.stderr)
            return
    print("Digest per Telegram versendet")


# ── 8. Freitags-Wochenrückblick ────────────────────────────────────────────
def weekly_review() -> None:
    files = sorted(p for p in DIGEST_DIR.glob("*.md") if "weekly" not in p.stem)[-7:]
    if len(files) < 3:
        print("Wochenrückblick übersprungen (weniger als 3 Tages-Digests)")
        return
    parts = []
    for p in files:
        text = _META.sub("", p.read_text(encoding="utf-8")).strip()
        parts.append(f"=== Digest vom {p.stem} ===\n{text[:4000]}")
    text = call_claude(WEEKLY_PROMPT, "\n\n".join(parts), max_tokens=1800)
    if not text:
        print("Wochenrückblick übersprungen (kein LLM verfügbar)")
        return
    save_digest(text, len(files), f"anthropic:{ANTHROPIC_MODEL}", suffix="-weekly")
    send_telegram(text, f"📅 Wochenrückblick – KW {datetime.now(TZ).isocalendar()[1]}",
                  with_feedback=False)


# ── 9. Dashboard rendern ───────────────────────────────────────────────────
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
    fb = load_json(STATE_DIR / "feedback.json", {"up": 0, "down": 0})
    articles = []

    for path in sorted(DIGEST_DIR.glob("*.md"), reverse=True)[:16]:
        text = path.read_text(encoding="utf-8")
        meta = {"created": path.stem, "items": "?", "llm": "?", "weekly": False}
        m = _META.match(text)
        if m:
            meta.update(json.loads(m.group(1)))
            text = text[m.end():]
        try:
            created = datetime.fromisoformat(meta["created"]).strftime("%a, %d.%m.%Y %H:%M")
        except (ValueError, TypeError):
            created = path.stem
        label = "📅 " + created if meta.get("weekly") else created
        is_first = not articles
        articles.append(
            f'<details{" open" if is_first else ""}><article>'
            f'<summary>{html.escape(label)}'
            f'<span class="badge">{html.escape(str(meta["items"]))} Items · '
            f'{html.escape(str(meta["llm"]))}</span></summary>'
            f'<div class="content">{md_to_safe_html(text)}</div></article></details>')

    body = "\n".join(articles) if articles else '<div class="empty">Noch kein Digest vorhanden.</div>'
    stamp = (datetime.now(TZ).strftime("%d.%m.%Y %H:%M")
             + f" · Feedback: 👍{fb.get('up', 0)} / 👎{fb.get('down', 0)}")
    page = template.replace("<!--DIGESTS-->", body).replace("<!--UPDATED-->", stamp)
    (DOCS_DIR / "index.html").write_text(page, encoding="utf-8")
    (DOCS_DIR / ".nojekyll").write_text("", encoding="utf-8")
    print(f"Dashboard gerendert ({len(articles)} Digests)")


# ── Main ───────────────────────────────────────────────────────────────────
def main() -> int:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if token:
        collect_feedback(token)

    items = fetch_items()
    if not items:
        print("Keine Einträge im Zeitfenster – Abbruch ohne Digest")
        render_dashboard()
        return 0

    items = cluster_items(items)[:MAX_ITEMS]
    enrich_fulltext(items, FULLTEXT_TOP_N)

    md, llm = summarize(items)
    save_digest(md, len(items), llm)
    save_category_snapshot(items)
    send_telegram(md, f"☕ Dein Tech-Digest – {datetime.now(TZ).strftime('%A, %d.%m.%Y')}")

    if datetime.now(TZ).weekday() == 4:  # Freitag
        weekly_review()

    render_dashboard()
    return 0


if __name__ == "__main__":
    sys.exit(main())
