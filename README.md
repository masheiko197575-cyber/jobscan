# jobscan

Täglicher Vakanzen-Scan für die Jobsuche-Tabelle (Projektmanagement, Koordination, Projektentwicklung, Einkauf — Photovoltaik und Batteriespeicher, Deutschland).

Läuft als GitHub Action jeden Morgen und schreibt `data/today.json` in den Branch `data` (jeden Tag ein frischer Commit, damit die Historie nicht wächst): https://raw.githubusercontent.com/masheiko197575-cyber/jobscan/data/data/today.json

Inhalt:

- `candidates` — Jobbörse der Bundesagentur (offizielle Jobsuche-API), Titel passt zur Rolle, mit Beschreibung
- `other` — Jobbörse, Titel unklar (nur Titel/Firma/Ort)
- `watch` — beobachtete Arbeitgeber (Jobbörse nach Name, Personio-, Recruitee-, SmartRecruiters-, Workday-, JOIN- und Ashby-Feeds; JOIN über die öffentliche Liste `/api/public/companies/<ID>/jobs`, alle Seiten; Personio ohne XML-Liste über `/search.json`)
- `boards` — eejobs.de und greenjobs.de (Atom) sowie optional python-jobspy (Indeed, LinkedIn-Gastsuche)
- `stats`, `errors`

Quellen und Filter stehen in `config.json`. Keine Zugangsdaten, keine personenbezogenen Daten.

Datenquelle Jobbörse: öffentliche Jobsuche-API der Bundesagentur für Arbeit (Suche `/pc/v6/jobs`, Details `/pc/v4/jobdetails`; dokumentiert unter github.com/bundesAPI/jobsuche-api). Ergebnis: `data/today.json` (+ `data/seen.json`, `data/details.json`, `data/run-log.json`).

Zeitplan: Scan gegen 10:00 Berlin (damit die am Morgen veröffentlichten Stellen drin sind), die Morgenaufgabe der Tabelle läuft um 10:53. Hauptauslöser ist eine Claude-Aufgabe um 09:57 Berlin: `workflow_dispatch` mit `only_if_missing=true` (scannt nur, wenn es heute ab 09:30 Berlin noch keinen vollständigen Scan gab). GitHub startet geplante Läufe nur nach Möglichkeit (oft Stunden zu spät oder gar nicht) — die beiden Zeitfenster 08:53 und 09:19 UTC sind Reserve; findet die Morgenaufgabe trotzdem keinen heutigen Scan, startet sie ihn selbst. Manuell: `gh workflow run daily.yml` (ohne Eingabe — scannt immer).
