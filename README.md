# jobscan

Täglicher Vakanzen-Scan für die Jobsuche-Tabelle (Projektmanagement, Koordination, Projektentwicklung, Einkauf — Photovoltaik und Batteriespeicher, Deutschland).

Läuft als GitHub Action jeden Morgen und schreibt `data/today.json` in den Branch `data` (jeden Tag ein frischer Commit, damit die Historie nicht wächst): https://raw.githubusercontent.com/masheiko197575-cyber/jobscan/data/data/today.json

Inhalt:

- `candidates` — Jobbörse der Bundesagentur (offizielle Jobsuche-API), Titel passt zur Rolle, mit Beschreibung
- `other` — Jobbörse, Titel unklar (nur Titel/Firma/Ort)
- `watch` — beobachtete Arbeitgeber: Jobbörse nach Name, Personio-, Recruitee-, SmartRecruiters-, Workday- und Ashby-Feeds, JOIN-Firmenseiten (alle Seiten über `?page=N`; die frühere Liste `/api/public/companies/<ID>/jobs` gibt es seit 11/2026 nicht mehr) und Karriereseiten mit Sitemap + JSON-LD (EnBW, Netze BW). `pv_only` in `config.json`: bei Arbeitgebern ohne PV/BESS-Schwerpunkt nur Stellen mit PV/Speicher/Netzanschluss im Titel oder Text
- `boards` — eejobs.de und greenjobs.de (Atom), LinkedIn-Gastsuche und Indeed über python-jobspy (Indeed: enge Begriffe und breite Branchenwörter), XING (öffentliche Suche: letzte 24 Stunden und letzte Woche), Google for Jobs (SerpApi); `first_seen` = wann der Scanner die Stelle zum ersten Mal sah
- `alerts` — Warnungen (Quelle ausgefallen, unvollständig oder deutlich unter ihrem üblichen Wert; Vergleich mit `data/history.json`), `blind` — beobachtete Firmen ohne eine einzige Stelle in der Jobbörse seit 30 Tagen, `filters` — Titel-Regeln
- `stats`, `errors`

Daneben im Branch `data`: `dropped.json` (alle nach Titel verworfenen Stellen des Laufs mit dem auslösenden Wort), `disputed.json` (die strittigen der letzten 14 Tage), `history.json` (Zähler je Quelle, 21 Tage).

`tools/tokens.py` (Token-Verbrauch der Morgenaufgabe nach Teilen) und `tools/mailmatch.py` (Stellen aus E-Mail-Alerts: Titel-Filter, Abgleich mit Tabelle und Scanner-Datei) lädt die Morgenaufgabe direkt von hier.

Quellen und Filter stehen in `config.json`. Keine Zugangsdaten, keine personenbezogenen Daten.

Datenquelle Jobbörse: öffentliche Jobsuche-API der Bundesagentur für Arbeit (Suche `/pc/v6/jobs`, Details `/pc/v4/jobdetails`; dokumentiert unter github.com/bundesAPI/jobsuche-api). Ergebnis: `data/today.json` (+ `data/seen.json`, `data/details.json`, `data/run-log.json`).

Zeitplan: Scan gegen 10:00 Berlin (damit die am Morgen veröffentlichten Stellen drin sind), die Morgenaufgabe der Tabelle läuft um 10:53. Hauptauslöser ist eine Claude-Aufgabe um 09:57 Berlin: `workflow_dispatch` mit `only_if_missing=true` (scannt nur, wenn es heute ab 09:30 Berlin noch keinen vollständigen Scan gab). GitHub startet geplante Läufe nur nach Möglichkeit (oft Stunden zu spät oder gar nicht) — die beiden Zeitfenster 08:53 und 09:19 UTC sind Reserve; findet die Morgenaufgabe trotzdem keinen heutigen Scan, startet sie ihn selbst. Manuell: `gh workflow run daily.yml` (ohne Eingabe — scannt immer).
