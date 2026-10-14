# Eigenes Mähroboter-Frontend für LUBA – Projektplan

Oct 8, 2026 · @Lukas

## Ziel & Rahmen

Gebaut wird eine eigenständige, mehrbenutzerfähige Steuerzentrale für Mähroboter für zuhause, herstellerunabhängig über eine Steuerschnittstelle mit Adaptern je Hersteller (der erste Adapter ist Mammotion): Status, Karte, Mähaufträge mit eigenen Einstellungen, wetterabhängige Zeitpläne, Kameras, Benachrichtigungen und ein optionaler KI-Assistent in einer selbst gehosteten Web-App. Sie besteht aus kleinen, einzeln skalierbaren Diensten, läuft auf einem Mini-PC zu Hause genauso wie in der Cloud und hängt von keinem Cloud-Anbieter ab.

- **Zielgruppe:** zuerst du und dein Haushalt; später andere Haushalte und Betriebe auf derselben Installation.
- **Konten, Rechte & Flotte:** Konten (Haushalt oder Betrieb) mit Unterbenutzern, Rollen aus Einzelrechten und Flottenmanagement; Daten sind pro Konto strikt getrennt.
- **Architektur:** Microservices mit klaren Verträgen; die Mäher-Steuerung ist eine eigenständige, wiederverwendbare Komponente.
- **Mäher-Anbindung:** PyMammotion; Verbindung wählbar: Bluetooth über eine ESP32-Bridge (Standard) oder WLAN, sobald Mammotion die WLAN-Steuerung wie angekündigt wieder freigibt.
- **Positionierung:** eigene RTK-Station und Mammotion-Netz-RTK über 4G werden beide unterstützt.
- **Oberfläche:** Web-App, auf dem Handy installierbar (PWA), später optional als Store-App.
- **Home Assistant:** eigene HACS-Integration als Client der öffentlichen API.
- **Anmeldung:** offen für weitere Verfahren; Start mit Benutzername + Passwort, OAuth (z. B. GitHub) ist eingeplant.
- **Abo & Zahlung:** als optionaler Dienst, je Installation ein- oder ausschaltbar. Für dich zu Hause: aus.
- **Betrieb:** selbst gehostet (Docker Compose) oder Cloud (Kubernetes), dieselben Images.

Grundsätze: offen selbst hostbar, ohne Cloud-Zwang, und die Steuerung ist ein Baustein, den andere Programme nutzen können.

## Funktionsumfang

Abgedeckt ist alles, was ein Haushalt mit dem Mäher macht, dazu die Steuerung aus Home Assistant, Mandanten und ein abschaltbares Abo. Mäherbefehle laufen über PyMammotion, alles andere ist eigene Logik im Backend. Die Spalte Phase verweist auf den Phasenplan weiter unten.

| Funktion | Umsetzung | Phase | Aufwand |
| --- | --- | --- | --- |
| Status: Akku, Mähstatus, RTK-Fix, WLAN/Bluetooth | Backend liest per PyMammotion, Live-Updates per WebSocket | MVP | mittel |
| Verbindungsart wählen: Bluetooth oder WLAN | Verbindungs-Adapter im Backend, Umschalter in den Einstellungen; WLAN-Adapter folgt, sobald Mammotion es freigibt | MVP | mittel |
| Karte mit Position, Zonen, gefahrener Route | Kartendaten über PyMammotion, eigene Darstellung | MVP | mittel |
| Start, Pause, Zurück zur Station, ganze Zone mähen | Befehle über PyMammotion | MVP | gering |
| Schnitthöhe und Tempo, auch während des Mähens | Befehle über PyMammotion | MVP | gering |
| Benachrichtigungen: fertig, steckt fest, Akku, offline | Web Push an die PWA, alternativ ntfy | MVP | mittel |
| Steuerung aus Home Assistant | eigene HACS-Integration über die Backend-API | Komfort | mittel |
| Zeitpläne (wöchentlich, alle x Tage) | eigene Zeitplanung im Backend | Komfort | mittel |
| Regenpause | Wetterdienst wie Open-Meteo im Backend | Komfort | gering |
| Gesundheit: Messerstunden, Akkuverlauf | eigene Historie in SQLite | Komfort | gering |
| Manuell fahren per Joystick, Messer an | direkter Befehl über PyMammotion | Komfort | mittel |
| RTK-Signalkarte | Positionen mit Fix-Qualität und Korrekturquelle (Station oder 4G-Netz-RTK) loggen, als Heatmap zeigen | Komfort | mittel |
| Diebstahlalarm: Anheben, Verlassen der Station | Sensoren + Geofence, Alarm mit Position | Komfort | mittel |
| Mehrere Benutzer, Hell/Dunkel-Designs | eigene Konten, Login per Benutzername und Passwort, weitere Verfahren als Plug-in; CSS-Themes | Komfort | mittel |
| Spot-Mähen beliebiger Formen mit erweiterbaren Mähmustern | eigene Bahnplanung + Ausführung | Spot & Kamera | hoch |
| Verpasste Flecken nachmähen | Abdeckung aus Routenlog berechnen | Spot & Kamera | hoch |
| Live-Kamera | Streaming-Weg erst klären | Spot & Kamera | hoch |
| Ai Recover und Find Dock | Kamerabild an Vision-Modell, Freigabe per Push | KI | sehr hoch |
| Konten, Unterbenutzer, Einladungen, eigene Rollen | Identity-Dienst, Mandanten-ID in jedem Token | Fundament | mittel |
| Abo, Testphase, Zahlung, Rechnungen | Billing-Dienst mit Stripe-Adapter, abschaltbar | Flotte & Abo | hoch |
| Plan-Grenzen: Anzahl Mäher, Zusatzfunktionen | Entitlement-Dienst, bei abgeschaltetem Abo alles frei | Flotte & Abo | mittel |
| Betreiber-Konsole: Haushalte, Pläne, Systemzustand | eigener Bereich der Web-App, nur für Betreiber-Admins | Flotte & Abo | mittel |
| Standorte, Gruppen, Flottenkarte, Sammelbefehle | Fleet-Dienst auf Basis der Rechte-Hierarchie | Flotte & Abo | mittel |
| Berichte und Fleet-API | Abfragen auf Telemetry, öffentliche API mit eingeschränkten Tokens | Flotte & Abo | mittel |

## Architektur

Die Plattform ist in kleine Dienste geschnitten, die nur über das Gateway und den Event-Bus miteinander sprechen. Nur das Device-Gateway redet mit Mähern; App, Home Assistant und Bridges sind gleichberechtigte Clients.

&#91;embedded content: Architektur · Clients, Gateway, 10 Dienste, Event-Bus, Datenhaltung\]

Jeder Dienst ist intern gleich aufgebaut (Domain, Anwendungsfälle, Adapter), damit man sich in jedem sofort zurechtfindet. Alles in einem Monorepo mit getrennten Paketen: ein Dienst je Ordner, gemeinsame Bibliotheken (`mowdeck-control`, `mowdeck-contracts`, `mowdeck-auth`) als eigenständige Pakete, die auch außerhalb nutzbar sind.

## Dienste im Überblick

Die Plattform besteht aus zwölf kleinen Programmen plus App, Bridge und HACS-Integration; jedes hat eine Aufgabe, eigene Daten und wird einzeln skaliert. Die Steuerung steckt zusätzlich in einer eigenen Bibliothek, damit andere Programme sie ohne den Rest nutzen können.

| Dienst | Aufgabe | Eigene Daten | Skaliert nach | Wiederverwendbar |
| --- | --- | --- | --- | --- |
| **Gateway** | einziger Eingang: TLS, Routing, Rate-Limits, Token-Prüfung | keine | Anfragen | Standard-Software (Traefik) |
| **Identity** | Konten, Benutzer, Unterbenutzer, Rollen und Rechte, Anmelde-Plug-ins, API-Tokens, Ausgabe kurzlebiger Tokens | Benutzer, Sitzungen, Identitäten | Anmeldungen | ja, für jedes Projekt mit Login |
| **Authz (OpenFGA)** | beantwortet jede Rechte-Frage anhand des Modells und der Zuweisungen | Beziehungen (wer, Rolle, Ebene) | Prüf-Anfragen | Standard-Software |
| **Fleet** | Mäher, Standorte, Gruppen, Freigaben, Verbindungsart, Sammelbefehle | Mäher, Freigaben | Anfragen | – |
| **Device-Gateway** (Steuerung) | hält die Verbindungen zu Mähern und Bridges, setzt Befehle um, meldet Zustand | nur flüchtig | Anzahl verbundener Mäher (Sharding nach Mäher-ID) | ja: Kern als Bibliothek `mowdeck-control` |
| **Telemetry** | Zustand, Positionen, RTK- und Akkuverlauf speichern und abfragen | Zeitreihen | Datenmenge | – |
| **Scheduler** | Zeitpläne, Regenpause, Wartungs-Erinnerungen | Pläne | Anzahl Pläne | – |
| **Planner** | Mähmuster als Plug-ins, Routen berechnen, prüfen, Vorschau (rechenintensiv) | keine | CPU-Last | ja, als Bibliothek |
| **Notifications** | Web Push, ntfy, E-Mail nach Benutzer-Vorlieben | Abos, Vorlieben | Nachrichten | ja |
| **Realtime** | WebSocket-Verteiler für Live-Daten an App und HA | keine | offene Verbindungen | ja |
| **Entitlements** | beantwortet „Darf Haushalt X Funktion Y?“ | Pläne, Grenzen | Anfragen (mit Cache) | – |
| **Billing** (optional) | Abo, Testphase, Zahlung über Zahlungsanbieter | Abos, Rechnungsverweise | Webhooks | – |
| Web-App (PWA) | Oberfläche für Benutzer und Betreiber | – | statisch, CDN-fähig | – |
| ESP32-Bridge | Firmware auf dem Mäher, Bluetooth ↔ WLAN | – | – | ja |
| HACS-Integration | Home Assistant als Client | – | – | – |

Später kommt ein **Vision**-Dienst für Kamera und Ai Recover dazu. Kein Dienst greift auf die Datenbank eines anderen zu; geteilt werden nur Verträge.

## Kommunikation & Verträge

Abfragen laufen synchron über das Gateway, alles Zeitkritische und Befehle laufen asynchron über einen Event-Bus. So kann jeder Dienst ausfallen oder skalieren, ohne die anderen mitzureißen.

- **Von außen:** App, HACS-Integration und Bridges sprechen nur mit dem Gateway (REST + WebSocket). Die öffentliche API ist versioniert (`/api/v1`).
- **Zwischen Diensten:** Event-Bus mit NATS JetStream. Befehle als `command.mower.<id>`, Zustand als `telemetry.mower.<id>`, fachliche Ereignisse wie `mower.registered` oder `subscription.changed`.
- **Tokens:** Identity gibt kurzlebige, signierte Tokens (JWT, 5–15 min) mit Benutzer, Haushalt, Rollen und Plan aus. Jeder Dienst prüft sie selbst über einen öffentlichen Schlüssel – keine Rückfrage bei Identity pro Anfrage.
- **Verträge:** OpenAPI für REST, AsyncAPI bzw. JSON-Schema für Events, in einem eigenen Paket `mowdeck-contracts`. Änderungen nur abwärtskompatibel oder mit neuer Version.
- **Zuverlässigkeit:** Befehle tragen eine eindeutige ID (doppelte Zustellung wird erkannt), haben ein Ablaufdatum (ein veralteter „Start“ wird verworfen) und Not-Stopp hat Vorrang.
- **Bridge-Anbindung:** Die ESP32-Bridge baut die Verbindung von sich aus auf (ausgehend, TLS, eigenes Gerätegeheimnis). Funktioniert hinter jedem Router, zu Hause wie in der Cloud.
- **Beobachtbarkeit:** OpenTelemetry für Traces und Logs, Prometheus-Metriken, Korrelations-ID durch alle Dienste.

## Konten, Benutzer & Rechte

Ein Konto (Haushalt oder Betrieb) hat einen Eigentümer und beliebig viele Unterbenutzer. Rechte werden auf einer Ebene vergeben – Konto, Standort, Gruppe oder einzelner Mäher – und gelten für alles darunter.

&#91;embedded content: Rechte-Hierarchie · Konto, Standort, Gruppe, Mäher\]

Im Beispiel darf Anna nur die Gruppe Nord und deren Mäher bedienen. Bei einem Haushalt gibt es nur einen unsichtbaren Standard-Standort, dieselbe Logik deckt also Haushalt und Flotte ab.

- **Konto:** Daten- und Abrechnungsgrenze; jeder Datensatz trägt die Konto-ID, die nur aus dem geprüften Token kommt.
- **Unterbenutzer:** vom Konto angelegt, per Einladungslink oder direkt mit Benutzername (z. B. Mitarbeiter ohne eigene E-Mail). Sie gehören dem Konto und lassen sich jederzeit sperren. Ein Mensch kann zusätzlich Mitglied in anderen Konten sein.
- **Einzelrechte:** fein geschnitten, z. B. `mower.view`, `mower.control`, `mower.drive`, `schedule.edit`, `fleet.bulk_command`, `users.manage`, `billing.manage`.
- **Rollen:** benannte Bündel von Einzelrechten. Standardrollen sind vorgegeben, jedes Konto kann eigene Rollen anlegen.
- **Prüfung:** kein Eigenbau. OpenFGA übernimmt die Rechte-Prüfung als eigener Dienst (Open Source, CNCF-Projekt, beziehungsbasierte Rechte nach Googles Zanzibar-Muster, selbst gehostet auf PostgreSQL). Das Rechte-Modell liegt als Datei im Repo und wird wie Code versioniert und getestet. Dienste fragen über die dünne Bibliothek `mowdeck-authz` mit genau einer Frage: darf *wer* *was* auf *welchem Objekt*.
- **Betreiber-Ebene:** Betreiber-Admins verwalten Konten und Pläne, sehen aber keine Mäherdaten. Support-Zugriff nur mit Freigabe des Kontos, zeitlich begrenzt und protokolliert.
- **Audit-Log:** jede Rechteänderung und jeder Steuerbefehl wird mit Wer, Was, Wann gespeichert.

| Standardrolle | Typische Ebene | Darf |
| --- | --- | --- |
| Eigentümer | Konto | alles, inkl. Abo, Konto löschen, Eigentum übertragen |
| Admin | Konto | Benutzer, Rollen, Standorte, Mäher verwalten |
| Manager | Standort oder Gruppe | Mäher einrichten, Zeitpläne, Sammelbefehle |
| Bediener | Standort, Gruppe oder Mäher | mähen, fahren, Spot-Mähen |
| Betrachter | jede | nur ansehen |
| Integration (API-Token) | frei wählbar | nur die gewählten Rechte, z. B. für Home Assistant |

## Flottenmanagement

Flotte ist kein eigenes Produkt, sondern dieselbe Plattform mit mehr Standorten, Gruppen und Mitarbeitern. Alle Funktionen bauen auf der Rechte-Hierarchie auf und laufen im Fleet-Dienst; ein Haushalt sieht sie einfach nicht, solange sein Plan sie nicht enthält.

| Funktion | Umsetzung | Wiederverwendet |
| --- | --- | --- |
| Standorte mit Adresse, Zeitzone, eigener Karte | Fleet-Dienst | Rechte-Hierarchie |
| Mäher-Gruppen, auch standortübergreifend | Fleet-Dienst | Rechte-Hierarchie |
| Karte aller Mäher, nach Standort gefiltert | Web-App, Live-Daten über Realtime | Telemetry, Realtime |
| Sammelbefehle: alle pausieren, alle zur Station | Fleet zerlegt in Einzelbefehle auf dem Event-Bus | Device-Gateway, Befehlsformat |
| Mitarbeiter mit Rollen je Standort | Identity | Rollen, Unterbenutzer |
| Zeitpläne für Gruppen | Scheduler mit Gruppe als Ziel | Scheduler |
| Berichte: Mähzeit, Fläche, Störungen je Standort | Abfragen auf Telemetry, Export als CSV | Telemetry |
| Fleet-API für eigene Systeme | öffentliche API + API-Tokens mit eingeschränkten Rechten | Gateway, Identity |
| Rechnungen mit Firmendaten und Steuernummer | Billing | Billing |

Sammelbefehle laufen nie am Rechte-System vorbei: Fleet prüft jeden einzelnen Mäher, und das Ergebnis zeigt pro Mäher, ob der Befehl angekommen ist.

## Mähmuster & Routen

Mähmuster sind Plug-ins: Jedes Muster ist ein kleines, austauschbares Modul, das aus einer Fläche und Einstellungen eine Route berechnet. Die vier Grundmuster (Streifen, Diagonale, Schachbrett, nur Rand) sind nur der Anfang; neue Muster kommen ohne Änderung an App, Planner oder Steuerung dazu.

| Muster | So fährt der Mäher | Sinnvoll für | Phase |
| --- | --- | --- | --- |
| Streifen (Zickzack) | parallele Bahnen, Winkel frei wählbar | Standard, schnell | 5 |
| Spirale | von außen nach innen oder umgekehrt | runde und kompakte Flächen | 5 |
| Schachbrett | zwei Durchgänge quer zueinander | gleichmäßiges Schnittbild | 5 |
| Diagonal | Streifen im 45°-Winkel | Optik, weniger Fahrspuren | 5 |
| Rand zuerst + Füllung | Randrunden, dann beliebiges Füllmuster | saubere Kanten | 5 |
| Konturparallel | Bahnen folgen der Flächenform nach innen | verwinkelte Beete, Kurven | 5 |
| Automatisch ausgerichtet | Streifen im Winkel mit den wenigsten Wenden | lange, schmale Flächen, Akku sparen | 5 |
| Zerlegt (Zellen) | Fläche in Teilstücke zerlegen, je eigene Richtung | komplexe Formen mit Hindernissen | 5 |
| Wechselnde Richtung | Winkel ändert sich bei jedem Mähen | keine Fahrrinnen im Rasen | 5 |
| Wellen, Kreise, Strahlen | geschwungene oder sternförmige Bahnen | Optik, Spaß | später |
| Rasenkunst | Streifenmuster, die Formen oder Schriftzüge zeigen | Optik | später |
| Nachmähen | nur Lücken aus dem letzten Lauf | Ausbesserung | 5 |
| Nur Kante | eine oder mehrere Randrunden | zwischen zwei Vollmäh-Läufen | 5 |

**So funktioniert ein Muster-Plug-in:**

1. **Eingang:** Fläche als Polygon mit Aussparungen (Beete, No-Go-Zonen), Mäherdaten (Schnittbreite, Wendekreis) und die Einstellungen des Musters.
2. **Einstellungen beschreiben sich selbst:** Jedes Muster liefert ein JSON-Schema seiner Einstellungen (Winkel, Abstand, Richtung, Runden …). Die App baut daraus das Formular automatisch – ein neues Muster braucht keine neue Oberfläche.
3. **Ausgang:** ein neutraler Mähplan aus Abschnitten (fahren mit Messer an oder aus, Geschwindigkeit, Reihenfolge), unabhängig vom Mäher-Modell.
4. **Prüfkette für alle Muster gleich:** innerhalb der Fläche mit Sicherheitsabstand, Wendekreis machbar, keine No-Go-Zonen, Messer aus beim Überfahren. Ein Plug-in kann keine unsichere Route erzeugen.
5. **Vorschau:** Planner liefert Bahnen, Dauer und Fläche, bevor gestartet wird.
6. **Ausführung:** Das Device-Gateway übersetzt den Mähplan für den Mäher – als eigene Bahnfolge oder, wenn der Mäher das Muster selbst kann, als nativer Auftrag.

- **Kombinieren:** Muster lassen sich zu einer Kette verbinden, z. B. „2 Randrunden + Diagonal + Nachmähen“, und als eigene Vorlage speichern.
- **Zeitpläne:** können zwischen Mustern rotieren, z. B. jede Woche andere Richtung.
- **Wiederverwendbar:** alle Muster stecken in der reinen Rechen-Bibliothek `mowdeck-patterns` (ohne Netzwerk, ohne Datenbank), nutzbar auch außerhalb der Plattform.
- **Neues Muster hinzufügen:** eine Klasse mit Name, Einstellungs-Schema und Berechnung, registriert über einen Python-Einstiegspunkt; Pläne können Muster als Plan-Funktion freischalten.

## Erweiterbarkeit & Wiederverwendung

Eine neue Funktion soll im besten Fall ein neues Modul oder ein neuer Dienst sein, der sich selbst anmeldet – ohne Änderungen an bestehenden Diensten. Dafür gibt es feste Erweiterungspunkte und gemeinsame Bibliotheken.

**Selbstbeschreibende Module.** Jeder Dienst bringt eine Beschreibung mit: welche Rechte er definiert, welche Funktionen (für Pläne) er anbietet, welche Events er liefert und welche Bereiche er in der Web-App belegt. Identity, Entitlements und App lesen diese Beschreibungen beim Start. Ein neuer Dienst „Berichte“ meldet so `report.view` als Recht und `reports` als Plan-Funktion an, und beides erscheint automatisch in Rollen- und Plan-Verwaltung.

| Erweiterungspunkt | Schnittstelle | Beispiele |
| --- | --- | --- |
| Mäher-Modelle und Verbindungswege | `MowerConnection` in `mowdeck-control` | LUBA über BLE-Bridge, WLAN, YUKA, später andere Hersteller |
| Anmeldeverfahren | `CredentialAuthenticator`, `RedirectAuthenticator` | Passwort, GitHub, Google, Passkey, OIDC |
| Rechte-Prüfung | `Authorizer` in `mowdeck-authz` | OpenFGA-Modell um neue Objekttypen und Beziehungen erweitern, z. B. Berichte, Kameras |
| Zahlungsanbieter | `PaymentProvider` | Stripe, Paddle, Rechnung |
| Benachrichtigungs-Kanäle | `NotificationChannel` | Web Push, ntfy, E-Mail, Telegram |
| Wetter, Karten, KI | je ein Adapter-Port | Open-Meteo, Vision-Modelle |
| Neue Fachfunktion | neuer Dienst, hört auf vorhandene Events | Berichte, Wartungsplaner |
| Neue Oberfläche | Modul in der App-Shell (Route, Menüpunkt, benötigtes Recht) | Flottenkarte, Betreiber-Konsole |
| Mähmuster | MowPattern in mowdeck-patterns (Einstellungs-Schema + Berechnung) | Streifen, Spirale, Konturparallel, Rasenkunst |

**Gemeinsame Bibliotheken**, jede einzeln versioniert und auch außerhalb der Plattform nutzbar:

- `mowdeck-control` – Steuerung von Mähern, ohne Plattform nutzbar (Skripte, andere Projekte)
- `mowdeck-contracts` – alle API- und Event-Formate
- `mowdeck-auth` – Tokens prüfen, Principal ermitteln
- `mowdeck-authz` – dünner OpenFGA-Client mit Cache und Hilfen für die Rechte-Hierarchie
- `mowdeck-service-kit` – Grundgerüst jedes Dienstes: Konfiguration, Logging, Tracing, Health, Event-Bus-Anbindung, Mandanten-Filter, Fehlerformat
- `mowdeck-ui` – gemeinsame Oberflächen-Bausteine (Karte, Mäher-Karte, Rechte-gesteuerte Anzeige)
- `mowdeck-patterns` – Mähmuster und Routenprüfung als reine Rechen-Bibliothek

**Dienst-Vorlage.** Ein neuer Dienst entsteht aus einer Vorlage mit allem Pflichtzubehör: Schichten, Tests, Dockerfile, Helm-Werte, Beispiel-Endpunkt mit Rechteprüfung. Damit dauert das Gerüst Minuten statt Tage, und alle Dienste sehen gleich aus.

**Regeln, die Umbauten vermeiden:** Verträge nur abwärtskompatibel erweitern; neue Felder optional; Events nie umdeuten, sondern neue Versionen anlegen; jede Funktion hinter einem Funktions-Schlüssel, damit sie per Plan oder Installation an- und abschaltbar ist.

## Dokumentation

Dokumentation liegt als Text im selben Repo wie der Code, wird mit jeder Änderung gepflegt und automatisch als Website veröffentlicht. Was sich aus dem Code erzeugen lässt, wird erzeugt statt geschrieben.

| Dokument | Für wen | Inhalt | Wie es aktuell bleibt |
| --- | --- | --- | --- |
| Architektur nach arc42 | Entwickler | Ziele, Kontext, Bausteine, Laufzeit-Abläufe, Querschnittsthemen | Pflicht-Update bei neuen Diensten |
| C4-Diagramme | Entwickler, Betreiber | Kontext, Container, Komponenten | als Code (Structurizr oder Mermaid), im CI gerendert |
| Entscheidungen (ADRs) | Entwickler | je Entscheidung: Lage, Optionen, Wahl, Folgen – z. B. „ADR-001: OpenFGA statt Eigenbau“ | neue ADR bei jeder Architektur-Entscheidung |
| API-Referenz | Integratoren, HACS | alle Endpunkte und Events mit Beispielen | aus OpenAPI und AsyncAPI erzeugt |
| Rechte-Modell | Entwickler, Admins | Objekttypen, Rollen, Einzelrechte | aus dem OpenFGA-Modell und den Dienst-Beschreibungen erzeugt |
| Konfigurations-Referenz | Betreiber | jede Einstellung mit Standard und Bedeutung | aus den Einstellungs-Klassen erzeugt |
| Betriebshandbuch | Betreiber | Installation (Compose, Helm), Updates, Backups, Monitoring, Störungsfälle | bei jedem Release geprüft |
| Entwickler-Leitfaden | Mitwirkende | Dienst aus Vorlage anlegen, Recht hinzufügen, Mäher-Adapter schreiben, Tests | mit der Dienst-Vorlage gepflegt |
| Benutzerhandbuch | Endnutzer | App, Spot-Mähen, Zeitpläne, Flotte, HACS | bei neuen Funktionen |
| Changelog | alle | Änderungen je Version, Breaking Changes hervorgehoben | aus Commit-Konventionen erzeugt |

- **Werkzeug:** MkDocs Material als Doku-Website, im CI gebaut; auf GitHub Pages oder selbst gehostet.
- **Pflicht:** Eine Änderung gilt erst als fertig, wenn Doku und ggf. ADR mitgeändert sind; das CI prüft Links und ob die erzeugten Teile aktuell sind.
- **Jeder Dienst** hat ein README mit Zweck, Schnittstellen, Events, Einstellungen und lokalem Start – die Dienst-Vorlage bringt es schon mit.
- **Versionen:** Semantic Versioning für Dienste, Bibliotheken und öffentliche API.

## Abo & Zahlung

Bezahlung und Berechtigung sind getrennt: Der Billing-Dienst kümmert sich nur ums Geld, der Entitlement-Dienst beantwortet, was ein Haushalt darf. Ist Billing aus, meldet Entitlements einfach „alles erlaubt“ – kein anderer Dienst muss davon wissen.

- **Schalter je Installation:** `billing.enabled = false` (Standard bei dir) – Billing-Dienst wird gar nicht gestartet, Entitlements nutzt den Plan „Unbegrenzt“. Mit `true` gelten die konfigurierten Pläne.
- **Pläne:** frei definierbar als Daten, z. B. „Home“ (1–4 Mäher) und „Fleet“ (ab 5), mit Grenzen (Anzahl Mäher, Benutzer) und Funktionen (Spot-Mähen, Kamera, KI). Testphase in Tagen einstellbar.
- **Durchsetzung:** Der aktuelle Plan steht im Token; Dienste prüfen Funktionen lokal, harte Grenzen (z. B. weiterer Mäher) fragt Fleet bei Entitlements nach.
- **Zahlungsanbieter als Adapter:** Start mit Stripe (Checkout und Kundenportal). Weitere Anbieter wie Paddle, Mollie oder „manuell per Rechnung“ hinter derselben Schnittstelle.
- **Sicherheit:** keine Kartendaten im System (Zahlungsseite des Anbieters), Webhooks mit Signaturprüfung, jede Nachricht nur einmal verarbeitet.
- **Ablauf bei Zahlungsproblemen:** Schonfrist, dann nur noch Ansehen und Not-Stopp – nie ein Mäher, der wegen eines Abos mitten im Garten stehen bleibt.
- **Rechtliches, sobald andere zahlen:** Impressum, AGB, Datenschutzerklärung, Auftragsverarbeitung, Umsatzsteuer (bei EU-Kunden One-Stop-Shop). Das ist Teil der Phase „Flotte & Abo“, nicht vorher.

## Betriebsarten

Dieselben Container-Images laufen zu Hause und in der Cloud; der Unterschied liegt nur in Konfiguration und Anzahl der Instanzen. Es werden ausschließlich offene Bausteine genutzt, die überall laufen – keine Dienste, die es nur bei einem Cloud-Anbieter gibt.

|  | Selbst gehostet (dein Start) | Cloud |
| --- | --- | --- |
| Start | ein `docker compose up` | Helm-Chart auf Kubernetes |
| Instanzen | je Dienst eine | je Dienst nach Last, automatisch skaliert |
| Datenbank | eine PostgreSQL mit einem Schema je Dienst | PostgreSQL je Dienst oder verwalteter Cluster |
| Event-Bus | NATS als einzelner Container | NATS-Cluster |
| Billing | aus | an oder aus |
| Registrierung | aus | offen oder nur Einladung |
| Zugriff von außen | Tailscale oder Reverse-Proxy | Gateway mit öffentlicher Domain |
| Hardware | Mini-PC, NAS oder Raspberry Pi 5 mit 4 GB | beliebig |

Speicher für Zeitreihen und Dateien ist ebenfalls austauschbar (PostgreSQL bzw. lokales Volume oder S3-kompatibel wie MinIO). Der Umzug von zu Hause in die Cloud ist ein Datenbank-Export, kein Umbau.

Damit der Betrieb in jeder Cloud ohne Umbau funktioniert, hält sich jeder Dienst an feste Regeln (angelehnt an die Twelve-Factor-App):

- Konfiguration und Geheimnisse nur über Umgebungsvariablen oder Secret-Dateien, nie im Image.
- Dienste sind zustandslos; Zustand liegt in PostgreSQL, NATS oder Objektspeicher. Dadurch beliebig viele Instanzen.
- Logs strukturiert auf stdout, Metriken und Traces über OpenTelemetry.
- Health-, Readiness- und Liveness-Endpunkte; sauberes Herunterfahren ohne verlorene Befehle.
- Datenbank-Migrationen laufen als eigener Schritt vor dem Start, abwärtskompatibel für rollierende Updates.
- Images für amd64 und arm64, ohne Root, schreibgeschütztes Dateisystem.
- Nur Standard-Schnittstellen (PostgreSQL, S3, OIDC, NATS) – damit laufen dieselben Images auf Hetzner, AWS, Azure, Google Cloud oder zu Hause.

## Tech-Stack

Empfehlung: Python für die Dienste (eine Sprache wie PyMammotion und HA-Integrationen), offene Standard-Bausteine für Infrastruktur, alles in einem Monorepo mit getrennten Paketen.

| Schicht | Wahl | Warum |
| --- | --- | --- |
| Dienste | Python 3.12, FastAPI, jeder Dienst mit Domain-/Anwendungs-/Adapter-Schichten | klare Grenzen, gut testbar |
| Steuerungs-Kern | Bibliothek `mowdeck-control` auf PyMammotion, Verbindungs-Adapter BLE-Bridge und WLAN | in Device-Gateway, Skripten oder anderen Projekten nutzbar |
| Bluetooth-Bridge | ESP32 (z. B. XIAO ESP32-S3) auf dem Mäher, per USB versorgt | Bluetooth aus nächster Nähe, WLAN zum Device-Gateway |
| Gateway | Traefik | Routing, TLS, Rate-Limits, läuft in Compose und Kubernetes |
| Event-Bus | NATS JetStream | leicht, schnell, ein Binary – zu Hause wie im Cluster |
| Datenbank | PostgreSQL, ein Schema bzw. eine Datenbank je Dienst; Migrationen mit Alembic | Datenhoheit pro Dienst |
| Zeitreihen | PostgreSQL mit TimescaleDB-Erweiterung (optional) | Positionen, RTK, Akku |
| Authentifizierung | Plug-in-Schnittstelle: Passwort (Argon2id), API-Tokens, OAuth 2/OIDC (GitHub), später Passkey | neues Verfahren = neue Klasse |
| Dienst-Tokens | JWT mit EdDSA-Signatur, öffentliche Schlüssel per JWKS | Dienste prüfen ohne Rückfrage |
| Zahlung | Stripe-Adapter im Billing-Dienst, abschaltbar | keine Kartendaten im System |
| Frontend | SvelteKit oder React + Vite, als PWA; Karte mit Leaflet + Geoman | eine Oberfläche für Handy und PC |
| Store-App (später) | Capacitor | verpackt dieselbe PWA |
| Bahnplanung | Shapely | Streifen, Spiralen, Schachbrett |
| Verträge | OpenAPI, AsyncAPI, JSON-Schema im Paket `mowdeck-contracts` | Dienste unabhängig weiterentwickeln |
| Beobachtbarkeit | OpenTelemetry, Prometheus, Grafana (optional) | Fehler über Dienstgrenzen finden |
| Betrieb | Docker Compose (zu Hause), Helm-Chart (Cloud), Images per CI | gleiche Images überall |
| HA-Integration | Custom Integration für HACS, eigenes Repo | Config Flow mit URL + API-Token |
| KI (später) | Vision-Modell über austauschbaren Adapter | Ai Recover |
| Rechte | OpenFGA, selbst gehostet; Modell als Code im Repo, Client-Bibliothek mowdeck-authz | bewährt, offen, horizontal skalierbar – kein Eigenbau bei Sicherheit |

## Phasenplan

Nach etwa 8–12 Wochen ersetzt die eigene App die Mammotion-App bei dir; Flotte und Abo kommen erst danach, wenn das Fundament steht.

&#91;embedded content: Phasenplan · 6 Phasen, 5 Freigaben\]

Die Rauten sind Freigaben: Erst wenn das Kriterium darunter erfüllt ist, startet die nächste Phase. Das Fundament baut die Dienste von Anfang an mandantenfähig (Konto-ID und Rechte-Hierarchie überall), Flottenfunktionen, Abo und Cloud-Betrieb folgen in Phase 4. In Phase 5 kommen Spot-Mähen, Kamera und KI.

## Risiken, Sicherheit, offene Fragen

Das größte Risiko ist das inoffizielle Protokoll: Ein Firmware-Update von Mammotion kann Funktionen jederzeit brechen.

- **Protokolländerungen:** Abhängigkeit von PyMammotion-Updates. Gegenmittel: Version pinnen, Firmware-Updates des Mähers erst nach Community-Rückmeldung einspielen.
- **Doppelzugriff:** Solange die bisherige Mammotion-Integration in HA läuft, steuern zwei Dienste den Mäher. Im Spike ein zweites, geteiltes Mammotion-Konto nutzen; nach dem MVP die alte Integration entfernen.
- **Sicherheit beim Fahren:** Bei Joystick und Spot-Mähen gilt: Totmannschalter (Befehl verfällt nach z. B. 1 s ohne Signal), Messer beim Transfer aus, Sofort-Stopp-Taste immer sichtbar, Sensor-Stopps nie umgehen.
- **Eigene Konten und Zugriff:** Der Identity-Dienst übernimmt Login und Rechte. Authentifizierung ist offen gebaut: Jedes Verfahren ist ein Plug-in hinter einer gemeinsamen Schnittstelle. Start mit Benutzername und Passwort (Argon2) und API-Tokens pro Client (eins für HA); Passkeys oder OIDC lassen sich später ergänzen. Dazu Rate-Limit beim Login, kurze Sitzungen, Rollen pro Benutzer, Zugriff von außen nur über Tailscale oder HTTPS-Proxy.
- **Pflegeaufwand:** Viele kleine Programme heißt viele Versionen. Gegenmittel: Monorepo, gemeinsame Verträge, eine CI für alles, öffentliche API früh versioniert.
- **Nutzungsbedingungen:** für den Eigenbedarf unkritisch; vor einer Weitergabe oder Vermarktung prüfen.
- **WLAN-Steuerung noch nicht verfügbar:** Mammotion hat sie angekündigt, der Zeitpunkt ist offen. Deshalb ist Bluetooth der Standard und WLAN ein zweiter Adapter hinter derselben Schnittstelle, der sich später ohne Umbau einhängen lässt.
- **Komplexität von Microservices:** Für einen einzelnen Haushalt sind zehn Dienste mehr Aufwand als ein Programm. Gegenmittel: alle Dienste nach demselben Muster, Start mit einer Compose-Datei, und die Schnittstellen so gebaut, dass anfangs auch mehrere Dienste in einem Prozess laufen könnten.
- **Datenlecks zwischen Haushalten:** Das Hauptrisiko jeder Mehrbenutzer-Plattform. Gegenmittel: Haushalts-ID nur aus dem Token, Filter in jeder Abfrage, eigene Tests pro Dienst, die gezielt fremde Daten anfragen.
- **Verteilte Fehler:** Ein Befehl läuft durch Gateway, Bus und Device-Gateway. Gegenmittel: Befehls-IDs, Ablaufzeiten, Not-Stopp mit Vorrang, Traces über alle Dienste.
- **Zahlung und Recht:** Sobald andere zahlen, kommen Steuern, AGB und Datenschutz dazu. Gegenmittel: erst in Phase 4, Zahlungsanbieter mit Steuer-Abwicklung wählen.

Offene Fragen für den Spike:

- [ ] Liefert PyMammotion die Kartengeometrie (Zonen, Wege, Station) in nutzbarer Form?
- [ ] Unterstützt PyMammotion manuelles Fahren und Mähaufträge mit eigener Begrenzung?
- [ ] Wie kommt der Kamerastream vom Mäher, und ist er lokal abgreifbar?
- [ ] Wie hoch ist die Befehlslatenz über die Bluetooth-Bridge, reicht sie für den Joystick?
- [ ] Welche Entitäten und Dienste soll die HACS-Integration anbieten?
- [ ] Unterstützt PyMammotion Mäher mit 4G und Netz-RTK vollständig, inklusive Fix-Status und Verbindung über Mobilfunk?
- [ ] Liefert der Mäher die Korrekturquelle (Station oder Netz-RTK) mit, damit die App sie anzeigen kann?
- [ ] Welche Bridge-Firmware: ESPHome-Bluetooth-Proxy oder eine eigene, schlanke ESP32-Firmware?
- [ ] Zahlungsanbieter für EU-Kunden: Stripe mit Steuer-Modul oder ein Anbieter, der als Verkäufer auftritt (z. B. Paddle)?
- [ ] Wie viele Mäher-Verbindungen schafft eine Device-Gateway-Instanz? Daraus folgt die Sharding-Größe.
- [ ] Kann der LUBA beliebige Bahnfolgen abfahren, oder nur Zonen mit seinen eigenen Mustern? Davon hängt ab, welche Muster als eigene Route und welche als nativer Auftrag laufen.

## Spätere Erweiterungen & eigene Ideen

Mandanten, Abo und Cloud-Betrieb sind jetzt Teil des Plans; offen bleiben Erweiterungen, die auf diesem Fundament aufsetzen.

- **Weitere Mäher-Modelle:** neuer Verbindungs-Adapter in `mowdeck-control`, sonst keine Änderung.
- **Weitere Clients:** Siri-Kurzbefehle, MQTT-Brücke, Node-RED – alle über dieselbe öffentliche API.
- **Fleet-Funktionen für Betriebe:** Standorte, Karte aller Mäher, „alle zur Station“, Fleet-API.
- **Weitere Anmeldeverfahren:** Passkeys, Google, Apple, beliebiger OIDC-Anbieter.
- **Mehrere Regionen:** dank zustandsloser Dienste und NATS-Clustern später möglich.

Deine Ideen:

- [ ]
