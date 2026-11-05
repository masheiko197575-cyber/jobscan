# jobscan

Täglicher Vakanzen-Scan für die Jobsuche-Tabelle (Projektmanagement, Koordination, Projektentwicklung, Einkauf — Photovoltaik und Batteriespeicher, Deutschland).

Läuft als GitHub Action jeden Morgen (07:35 Berlin) und schreibt `data/today.json` in den Branch `data` (jeden Tag ein frischer Commit, damit die Historie nicht wächst): https://raw.githubusercontent.com/masheiko197575-cyber/jobscan/data/data/today.json

Inhalt:

- `candidates` — Jobbörse der Bundesagentur (offizielle Jobsuche-API), Titel passt zur Rolle, mit Beschreibung
- `other` — Jobbörse, Titel unklar (nur Titel/Firma/Ort)
- `watch` — beobachtete Arbeitgeber (Jobbörse nach Name, Personio-, Recruitee-, SmartRecruiters-, Workday-Feeds)
- `boards` — eejobs.de und greenjobs.de (Atom) sowie optional python-jobspy (Indeed, LinkedIn-Gastsuche)
- `stats`, `errors`

Quellen und Filter stehen in `config.json`. Keine Zugangsdaten, keine personenbezogenen Daten.

Datenquelle Jobbörse: öffentliche Jobsuche-API der Bundesagentur für Arbeit (Suche `/pc/v6/jobs`, Details `/pc/v4/jobdetails`; dokumentiert unter github.com/bundesAPI/jobsuche-api). Ergebnis: `data/today.json` (+ `data/seen.json`, `data/details.json`, `data/run-log.json`).
