# Feriendorf Flight Agent

Flugsuche mit Monats-/Flex-Suche, Star-Alliance-Filter und Buchung direkt bei der Airline.
Daten: Ignav-API (https://ignav.com). Nur Python 3 nötig, keine Pakete.

## Lokal starten
1. `cp .env.example .env` und `IGNAV_API_KEY` eintragen.
2. `python3 server.py` → http://localhost:8787 (auch am Handy im selben WLAN: `HOST=0.0.0.0 python3 server.py`, dann `http://<Mac-IP>:8787`).

## Mit anderen teilen
**Wichtig:** Alle Besucher verbrauchen *deinen* Ignav-Key (1000 Gratis-Abfragen, danach ca. 2 $ je 1000). Schütze die Seite daher:

| Variable | Wirkung |
|---|---|
| `ACCESS_CODE=geheim` | Browser fragt nach einem Passwort (Benutzername beliebig). Nur wer den Code kennt, kommt rein. |
| `PUBLIC_MODE=1` | Blendet Ignav-Konto/Verbrauch aus (sonst könnte ein Besucher dein Konto sehen/verwenden). |
| `DAILY_API_LIMIT=300` | Max. Ignav-Abfragen pro 24 h für alle zusammen. Danach meldet die Seite „Tageslimit erreicht". |
| `SEARCHES_PER_HOUR=10` | Max. Suchen je Besucher (IP) pro Stunde. |
| `TRUST_PROXY=1` | Nötig hinter Hosting-Diensten, damit die echte Besucher-IP verwendet wird. |

### Variante A: schnell, vom eigenen Mac (Link nur solange der Mac läuft)
```
ACCESS_CODE=meincode PUBLIC_MODE=1 DAILY_API_LIMIT=200 SEARCHES_PER_HOUR=10 python3 server.py
cloudflared tunnel --url http://localhost:8787     # liefert eine öffentliche https-Adresse
```
(`brew install cloudflared`; alternativ ngrok.) Die Adresse + den Code an Freunde schicken.

### Variante B: dauerhaft gehostet (z. B. Render, Fly.io, Railway)
Dieses Verzeichnis enthält ein `Dockerfile`. Beim Hosting-Dienst ein neues „Web Service" aus dem Repository/Ordner anlegen und als Umgebungsvariablen setzen:
`IGNAV_API_KEY`, `ACCESS_CODE`, `DAILY_API_LIMIT`, `SEARCHES_PER_HOUR` (`PUBLIC_MODE` und `TRUST_PROXY` sind im Dockerfile schon gesetzt).
Der Dienst vergibt eine https-Adresse; PORT liest die App automatisch.
Hinweis: Auf kostenlosen Tarifen wird der Speicher oft zurückgesetzt, dann beginnen Zwischenspeicher und Zähler bei 0.

## Hinweise
- Prüfe die Nutzungsbedingungen von Ignav, bevor du den Dienst auf deinem Key öffentlich anbietest.
- Der Server speichert keine Passwörter. Er protokolliert wie jeder Webserver die Adresse der Besucher in der Konsole.
- Preise sind Live-Preise der API und können sich ändern. Maßgeblich ist die Airline-Seite.
