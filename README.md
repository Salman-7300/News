# KI-Digest ☕ (serverlos via GitHub Actions)

Täglicher deutscher Tech-Digest ohne eigenen Server: GitHub Actions holt morgens
12 kuratierte Quellen (HN, heise, Golem, Simon Willison, Hugging Face, GitHub
Trending, Lobsters, Godot, BleepingComputer, Reddit), rankt nach deinen Interessen,
fasst per Claude API zusammen und liefert:

- **Telegram-Push** aufs Handy
- **Web-Dashboard** auf GitHub Pages (Archiv der letzten 14 Tage)

Kosten: 0 € für Actions/Pages, wenige Cent/Monat für die Claude API
(Haiku, 1 Aufruf/Tag). Ganz ohne API-Key läuft es auch – dann als einfache Linkliste.

---

## Einrichtung (einmalig, ~10 Minuten)

### 1. Repository anlegen
Neues Repo auf GitHub erstellen (z.B. `ki-digest`). **Public**, damit GitHub Pages
kostenlos ist – das Dashboard zeigt nur öffentliche News-Links, nichts Privates.
Dann diesen Ordner hochpushen:

```bash
cd ki-digest-actions
git init && git add -A && git commit -m "init"
git branch -M main
git remote add origin git@github.com:DEIN-USER/ki-digest.git
git push -u origin main
```

### 2. Secrets eintragen
Repo → **Settings → Secrets and variables → Actions → New repository secret**:

| Secret | Woher |
|---|---|
| `ANTHROPIC_API_KEY` | console.anthropic.com → API Keys (5 € Guthaben reicht ewig) |
| `TELEGRAM_BOT_TOKEN` | Telegram: [@BotFather](https://t.me/BotFather) → `/newbot` |
| `TELEGRAM_CHAT_ID` | Dem neuen Bot **zuerst eine Nachricht schicken**, dann `https://api.telegram.org/bot<TOKEN>/getUpdates` im Browser öffnen → Wert von `chat.id` |

### 3. GitHub Pages aktivieren
Repo → **Settings → Pages** → Source: **Deploy from a branch** →
Branch: `main`, Ordner: `/docs` → Save.
Dashboard-URL: `https://DEIN-USER.github.io/ki-digest/`

### 4. Ersten Lauf starten
Repo → **Actions** → „Täglicher KI-Digest" → **Run workflow**.
Nach ~1 Minute: Telegram-Nachricht da, Dashboard aktualisiert
(Pages braucht beim allerersten Mal ein paar Minuten extra).

Ab jetzt läuft es automatisch jeden Morgen. ✅

---

## Anpassen

- **Quellen & Keywords:** `feeds.yaml` direkt auf GitHub editieren – Feeds
  hinzufügen/entfernen, `interests` steuern das Ranking. Kein Code-Change nötig.
- **Uhrzeit:** in `.github/workflows/digest.yml` – Achtung, Cron läuft in **UTC**
  (04:30 UTC = 06:30 Sommerzeit; im Winter auf `30 5 * * *` stellen für 06:30).
- **Look:** `template.html` (Dashboard-Design).

## Hinweise

- **Reddit-Feeds** liefern von GitHub-Runnern oft 403 (Datacenter-IPs geblockt).
  Der Digest läuft dann einfach ohne sie weiter – bei Dauerzustand die drei
  Reddit-Blöcke aus `feeds.yaml` löschen.
- GitHub pausiert Cron-Workflows in Repos **ohne Aktivität nach ~60 Tagen** –
  da der Workflow täglich selbst committet, passiert das hier nicht.
- Digest-Archiv liegt als Markdown in `digests/` – durchsuchbar, versioniert, deins.

---

## Neu in v2

- **Dubletten-Clustering:** Dieselbe Story aus heise + Golem + HN wird zu einem
  Eintrag mit allen Quellen-Links gemergt (Titel-Ähnlichkeit, Jaccard ≥ 0,45).
  Mehrfach gemeldete Storys steigen im Ranking.
- **Volltext-Anreicherung:** Für die Top-3-Einträge wird die Artikel-Seite
  geladen (2500 Zeichen) – die Zusammenfassungen werden deutlich konkreter.
- **Feedback-Buttons:** 👍/👎 unter jedem Telegram-Digest. Die Reaktionen werden
  beim nächsten Lauf eingesammelt (`state/feedback.json`) und im Dashboard
  angezeigt. **Wichtig:** Für den Bot darf kein Webhook gesetzt sein (Standard).
- **Freitags-Wochenrückblick:** Jeden Freitag kommt zusätzlich ein Digest über
  die letzten 7 Tage – „was war diese Woche wirklich wichtig".
- **Fehler-Alarm:** Fehlgeschlagene Läufe melden sich per Telegram mit Log-Link.

---

## Neu in v3

- **Selbstlernende Interessen:** 👎 senkt die Gewichte der Kategorien des
  bewerteten Digests (anteilig), 👍 hebt sie leicht – nach ~2 Wochen ist der
  Digest auf deinen Geschmack kalibriert (`state/feedback.json → cat_weights`).
- **Archiv-Suche im Dashboard:** Suchfeld filtert alle Digests live nach
  Stichwort (z.B. „ollama", „cve") und öffnet die Treffer.

**Hinweis:** Digest und Deal-Sniper nutzen beide `getUpdates`. Wenn du für beide
denselben Bot verwendest, „klauen" sie sich gegenseitig die Updates. Lösung:
zwei getrennte Bots (je einer bei @BotFather), oder nur bei einem die
Chat-Interaktion (Feedback/Commands) aktiv lassen.
