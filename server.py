#!/usr/bin/env python3
"""Feriendorf Flight Agent – lokaler Server. Nur Python-Standardbibliothek.

Start:  python3 server.py   ->  http://localhost:8787
Der API-Key (Ignav) steht in .env:  IGNAV_API_KEY=...
"""
import base64
import hmac
import http.cookiejar
import json
import re
import threading
import time
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
API = "https://ignav.com/api"
DEFAULT_ORIGIN = "FRA"  # nur Vorbelegung; Abflughafen ist in der Suche frei wählbar
PORT = int(os.environ.get("PORT", "8787"))
HOST = os.environ.get("HOST", "127.0.0.1")  # für Hosting: HOST=0.0.0.0

# Einstellungen für die Veröffentlichung (siehe README.md); alle optional:
#   ACCESS_CODE        Zugangscode (Passwortabfrage des Browsers) für die ganze Seite
#   PUBLIC_MODE=1      blendet Ignav-Konto/Verbrauch aus, die Seite ist dann für fremde Nutzer gedacht
#   DAILY_API_LIMIT    maximale Ignav-Abfragen pro 24 h über alle Nutzer (Schutz des Kontingents)
#   SEARCHES_PER_HOUR  maximale Suchen je Besucher (IP) und Stunde
#   TRUST_PROXY=1      Besucher-IP aus X-Forwarded-For lesen (hinter Hosting-Proxy)
def public_mode():
    return env_var("PUBLIC_MODE") == "1"


def int_env(name, default=0):
    try:
        return int(env_var(name) or default)
    except ValueError:
        return default


_rate = {}

STAR_ALLIANCE = ["A3", "AC", "CA", "AI", "NZ", "NH", "OZ", "OS", "AV", "SN", "CM", "OU", "MS",
                 "ET", "BR", "LO", "LH", "SK", "ZH", "SQ", "SA", "LX", "TP", "TG", "TK", "UA"]


def env_var(name):
    """Wert aus der Umgebung oder aus .env neben server.py."""
    val = os.environ.get(name, "").strip()
    if val:
        return val
    path = os.path.join(HERE, ".env")
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line.startswith(name + "="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def load_key():
    return env_var("IGNAV_API_KEY")


def clean_url(url):
    """Affiliate-Umleitung entfernen (direkt zur Airline) und abgelaufene Google-Klick-IDs streichen."""
    if url.startswith("https://prf.hn/") and "destination:" in url:
        url = urllib.parse.unquote(url.split("destination:", 1)[1])
    if "?" in url:
        base, query = url.split("?", 1)
        parts = [x for x in query.split("&") if not x.startswith(("gclid=", "gclsrc="))]
        url = base + ("?" + "&".join(parts) if parts else "")
    return url


# ---- Optionale Anzeige des Ignav-Dashboards (Verbrauch) -------------------------------------------
# Nur aktiv, wenn IGNAV_EMAIL und IGNAV_PASSWORD in .env stehen. Das Passwort wird nur für den Login bei
# Ignav genutzt, die Sitzung bleibt im Arbeitsspeicher. Ohne Zugangsdaten läuft der Agent normal weiter.
ACCOUNT_BASE = os.environ.get("IGNAV_ACCOUNT_BASE", "https://ignav.com")
_acct = {"opener": None, "cache": None, "ts": 0.0}
_acct_lock = threading.Lock()


def _acct_login(email, pw):
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    req = urllib.request.Request(ACCOUNT_BASE + "/api/auth/login", method="POST",
                                 data=json.dumps({"email": email, "password": pw}).encode(),
                                 headers={"Content-Type": "application/json", "Accept": "application/json"})
    op.open(req, timeout=20).read()
    return op


def _acct_get(op, path):
    req = urllib.request.Request(ACCOUNT_BASE + path, headers={"Accept": "application/json"})
    with op.open(req, timeout=20) as r:
        return json.loads(r.read().decode())


def dashboard_info():
    """Verbrauch aus dem Ignav-Dashboard. Quelle der Anmeldung: Sitzung aus dem Login-Fenster der App
    (nur im Arbeitsspeicher) oder, optional, IGNAV_EMAIL/IGNAV_PASSWORD aus .env."""
    if public_mode():
        return {"configured": False, "public": True}
    email, pw = env_var("IGNAV_EMAIL"), env_var("IGNAV_PASSWORD")
    has_env = bool(email and pw)
    with _acct_lock:
        if not has_env and _acct["opener"] is None:
            return {"configured": False}
        if _acct["cache"] and time.time() - _acct["ts"] < 3:
            return _acct["cache"]
        try:
            for attempt in (0, 1):
                if _acct["opener"] is None:
                    _acct["opener"] = _acct_login(email, pw)
                try:
                    me = _acct_get(_acct["opener"], "/api/account/me")
                    an = _acct_get(_acct["opener"], "/api/account/analytics?range=24h")
                    break
                except urllib.error.HTTPError as e:
                    if e.code == 401:  # Sitzung abgelaufen
                        _acct["opener"] = None
                        if has_env and attempt == 0:
                            continue
                        return {"configured": False, "expired": True}
                    raise
            res = {"configured": True, "ok": True, "requests24h": an["totals"]["total_requests"],
                   "cost24h": an["totals"].get("estimated_cost_usd"), "freeRemaining": me.get("free_remaining"),
                   "source": "env" if has_env else "login"}
        except urllib.error.HTTPError as e:
            _acct["opener"] = None
            res = {"configured": True, "ok": False, "error": "Login bei Ignav fehlgeschlagen (%s)" % e.code}
        except Exception:  # noqa: BLE001
            _acct["opener"] = None
            res = {"configured": True, "ok": False, "error": "Ignav-Dashboard nicht erreichbar"}
        _acct["cache"], _acct["ts"] = res, time.time()
        return res


def dashboard_login(email, pw):
    op = _acct_login(email, pw)  # wirft HTTPError bei falschen Zugangsdaten; das Passwort wird nicht gespeichert
    with _acct_lock:
        _acct["opener"], _acct["cache"], _acct["ts"] = op, None, 0.0
    return dashboard_info()


def dashboard_logout():
    with _acct_lock:
        _acct["opener"], _acct["cache"], _acct["ts"] = None, None, 0.0


USAGE_FILE = os.path.join(HERE, "usage.json")
_lock = threading.Lock()
_cache = {}
TTL = {"/fares/round-trip": 1800, "/fares/one-way": 1800, "/fares/booking-links": 1800, "/airports": 7 * 86400}  # Sekunden


def _usage():
    try:
        return json.load(open(USAGE_FILE, encoding="utf-8"))
    except Exception:
        return {"total": 0, "by": {}}


def _count(path):
    with _lock:
        u = _usage()
        u["total"] = u.get("total", 0) + 1
        u["by"][path] = u["by"].get(path, 0) + 1
        now = time.time()
        u["log"] = [t for t in u.get("log", []) if now - t < 7 * 86400] + [round(now, 1)]
        try:
            json.dump(u, open(USAGE_FILE, "w", encoding="utf-8"))
        except OSError:
            pass


def usage_summary():
    u = _usage()
    now = time.time()
    log = u.get("log", [])
    return {"total": u.get("total", 0), "last24h": sum(1 for t in log if now - t < 86400),
            "logged": len(log), "since": min(log) if log else None, "by": u.get("by", {})}


def call_api(method, path, body=None, query=None, info=None):
    """API-Aufruf mit Zwischenspeicher: identische Anfragen kosten innerhalb der TTL keine API-Abfrage."""
    ck = (method, path, json.dumps(body, sort_keys=True), json.dumps(query, sort_keys=True))
    ttl = TTL.get(path, 0)
    with _lock:
        hit = _cache.get(ck)
    if hit and time.time() - hit[0] < ttl:
        if info is not None:
            info["cached"] = True
        return hit[1]
    key = load_key()
    if not key:
        raise RuntimeError("Kein API-Key: bitte IGNAV_API_KEY in .env eintragen.")
    limit = int_env("DAILY_API_LIMIT")
    if limit and usage_summary()["last24h"] >= limit:
        raise RuntimeError("Tageslimit der Seite erreicht, bitte später erneut versuchen.")
    url = API + path + ("?" + urllib.parse.urlencode(query) if query else "")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "X-Api-Key": key, "Content-Type": "application/json", "Accept": "application/json"})
    try:
        _count(path)
        with urllib.request.urlopen(req, timeout=60) as r:
            res = json.loads(r.read().decode())
        # Leere Treffer nie zwischenspeichern: die API liefert mitunter fälschlich 0 Angebote, ein erneuter Klick soll es neu versuchen
        empty = path in ("/fares/round-trip", "/fares/one-way") and not res.get("itineraries")
        if ttl and not empty:
            with _lock:
                _cache[ck] = (time.time(), res)
        return res
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read().decode()).get("error", {}).get("message", "")
        except Exception:
            msg = ""
        raise RuntimeError("API-Fehler %s: %s" % (e.code, msg or e.reason))


AIRPORT_FILE = os.path.join(HERE, "airports_cache.json")
try:
    AIRPORTS = json.load(open(AIRPORT_FILE, encoding="utf-8"))
except Exception:
    AIRPORTS = {}


def airport_info(codes):
    out, changed = {}, False
    for c in codes:
        c = c.upper()
        if c not in AIRPORTS:
            try:
                for a in call_api("GET", "/airports", query={"q": c, "limit": 5}):
                    if a.get("code") == c:
                        AIRPORTS[c] = a
                        changed = True
                        break
            except Exception:  # noqa: BLE001
                continue
        if c in AIRPORTS:
            out[c] = AIRPORTS[c]
    if changed:
        try:
            json.dump(AIRPORTS, open(AIRPORT_FILE, "w", encoding="utf-8"), ensure_ascii=False)
        except OSError:
            pass
    return out


def build_combos(dep, flex, nmin, nmax, max_calls, skip=-1):
    """Hinflugtag +- flex Tage, je Reisedauer nmin..nmax Nächte. Ist das Budget kleiner, wird gleichmäßig ausgedünnt."""
    center = date.fromisoformat(dep)
    today = date.today()
    combos = []
    for off in range(-flex, flex + 1):
        d = center + timedelta(days=off)
        if d <= today or abs(off) <= skip:  # skip: bereits gesuchter Bereich
            continue
        for n in range(nmin, nmax + 1):
            combos.append((d, d + timedelta(days=n), n))
    if len(combos) > max_calls > 0:
        step = len(combos) / max_calls
        combos = [combos[int(i * step)] for i in range(max_calls)]
    return combos


def diversify(its, per_combo=2, cap=14):
    """Günstigste Angebote behalten, aber je Airline-Kombination höchstens `per_combo`, damit z. B. Lufthansa
    nicht von sechs gleich teuren Angeboten einer anderen Airline verdrängt wird."""
    out, count = [], {}
    for it in sorted(its, key=lambda i: i["price"]["amount"]):
        codes = set()
        for leg in (it.get("outbound"), it.get("inbound")):
            for sg in (leg or {}).get("segments", []):
                codes.add(sg.get("marketing_carrier_code"))
        k = tuple(sorted(c or "" for c in codes))
        if count.get(k, 0) >= per_combo:
            continue
        count[k] = count.get(k, 0) + 1
        out.append(it)
        if len(out) >= cap:
            break
    return out


def search_one(p, dep, ret, phase):
    body = {"origin": p["origin"], "destination": p["dest"], "departure_date": dep.isoformat(),
            "adults": p["adults"], "cabin_class": p["cabin"],
            "market": p["market"], "allow_self_transfer": False}
    if not p["oneway"]:
        body["return_date"] = ret.isoformat()
    if p["maxStops"] is not None:
        body["max_stops"] = p["maxStops"]
    if p["bags"]:
        body["min_checked_bags"] = p["bags"]
    if phase == "star":
        body["airlines_include"] = STAR_ALLIANCE
    else:
        body["airlines_exclude"] = STAR_ALLIANCE
    info = {}
    res = call_api("POST", "/fares/one-way" if p["oneway"] else "/fares/round-trip", body, info=info)
    return diversify(res.get("itineraries", [])), bool(info.get("cached"))


class Handler(BaseHTTPRequestHandler):
    def client_ip(self):
        if env_var("TRUST_PROXY") == "1":
            fwd = self.headers.get("X-Forwarded-For", "")
            if fwd:
                return fwd.split(",")[0].strip()
        return self.client_address[0]

    def gate(self):
        """Zugangscode per HTTP-Basic-Auth (Benutzername egal). Ohne ACCESS_CODE ist die Seite offen."""
        code = env_var("ACCESS_CODE")
        if not code:
            return True
        try:
            given = base64.b64decode(self.headers.get("Authorization", "")[6:]).decode().split(":", 1)[1]
        except Exception:  # noqa: BLE001
            given = ""
        if hmac.compare_digest(given.encode(), code.encode()):
            return True
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Feriendorf Flight Agent"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def do_POST(self):
        if not self.gate():
            return
        u = urllib.parse.urlparse(self.path)
        try:
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n) or b"{}")
            own = self.headers.get("Origin", "").endswith(("localhost:%d" % PORT, "127.0.0.1:%d" % PORT))
            if not own:  # nur Aufrufe aus der eigenen Seite zulassen
                return self.send_json({"error": "forbidden"}, 403)
            if u.path.startswith("/api/dashboard") and public_mode():
                return self.send_json({"error": "forbidden"}, 403)
            if u.path == "/api/dashboard/login":
                try:
                    self.send_json(dashboard_login(str(body.get("email", "")).strip(), str(body.get("password", ""))))
                except urllib.error.HTTPError:
                    self.send_json({"configured": False, "error": "E-Mail oder Passwort falsch"}, 401)
            elif u.path == "/api/dashboard/logout":
                dashboard_logout()
                self.send_json({"configured": False})
            else:
                self.send_json({"error": "not found"}, 404)
        except Exception as e:  # noqa: BLE001
            self.send_json({"error": "Anmeldung nicht möglich"}, 502)

    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

    def send_json(self, obj, code=200):
        raw = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path == "/healthz":  # für Hosting-Dienste, ohne Zugangscode, ohne Inhalt
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")
            return
        if not self.gate():
            return
        u = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        try:
            if u.path in ("/", "/index.html"):
                raw = open(os.path.join(HERE, "index.html"), "rb").read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            elif u.path == "/api/status":
                self.send_json({"key": bool(load_key()), "origin": DEFAULT_ORIGIN})
            elif u.path == "/api/airports":
                self.send_json(call_api("GET", "/airports", query={"q": q.get("q", ""), "limit": 8}))
            elif u.path == "/api/dashboard":
                _acct["ts"] = 0.0 if q.get("fresh") else _acct["ts"]
                self.send_json(dashboard_info())
            elif u.path == "/api/usage":
                self.send_json(usage_summary())
            elif u.path == "/api/airportinfo":
                codes = [c for c in q.get("codes", "").split(",") if c.isalpha() and len(c) == 3][:40]
                self.send_json(airport_info(codes))
            elif u.path == "/api/links":
                self.links(q)
            elif u.path == "/api/scan":
                self.scan(q)
            else:
                self.send_json({"error": "not found"}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:  # noqa: BLE001
            self.send_json({"error": str(e)}, 502)

    def links(self, q):
        res = call_api("POST", "/fares/booking-links", {"ignav_id": q["ignav_id"]})
        out = []
        for opt in res.get("booking_options", []):
            for l in opt.get("links", []):
                if l.get("provider_type") == "airline":  # nur Airline-Seiten
                    l["url"] = clean_url(l["url"])
                    out.append(l)
        self.send_json({"links": out})

    def scan(self, q):
        p = {"origin": q.get("origin", DEFAULT_ORIGIN).upper(), "oneway": q.get("trip") == "oneway",
             "dest": q["dest"].upper(), "adults": int(q.get("adults", 1)),
             "cabin": q.get("cabin", "economy"), "market": q.get("market", "DE"),
             "maxStops": int(q["maxStops"]) if q.get("maxStops", "") != "" else None,
             "bags": int(q.get("bags", 0)), "star": q.get("star") == "1"}
        if not (re.fullmatch(r"[A-Z]{3}", p["origin"]) and re.fullmatch(r"[A-Z]{3}", p["dest"])):
            return self.send_json({"error": "Ungültiger Flughafencode"}, 400)
        combos = build_combos(q["dep"], max(0, min(3, int(q.get("flex", 0)))), int(q["min"]), int(q["max"]),
                               int(q.get("maxCalls", 20)), int(q.get("skip", -1)))
        per_hour = int_env("SEARCHES_PER_HOUR")
        if per_hour:
            now, ip = time.time(), self.client_ip()
            hist = [t for t in _rate.get(ip, []) if now - t < 3600]
            if len(hist) >= per_hour:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for ev, d in (("start", {"total": 0}), ("warn", {"message": "Suchlimit erreicht (%d pro Stunde), bitte später erneut versuchen." % per_hour, "dep": "", "ret": "", "done": 0}), ("done", {"done": 0, "api_calls": 0})):
                    self.wfile.write(("event: %s\ndata: %s\n\n" % (ev, json.dumps(d))).encode())
                return
            _rate[ip] = hist + [now]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        def emit(event, data):
            self.wfile.write(("event: %s\ndata: %s\n\n" % (event, json.dumps(data))).encode())
            self.wfile.flush()

        # Phase 1: Star Alliance, danach Phase 2: alle anderen Airlines (entfällt bei "nur Star Alliance")
        phases = ["star"] if p["star"] else ["star", "other"]
        used_before = _usage().get("total", 0)
        emit("start", {"total": len(combos) * len(phases), "phases": phases})
        done = 0
        stop = False
        for phase in phases:
            if stop:
                break
            with ThreadPoolExecutor(max_workers=4) as pool:
                futs = {pool.submit(search_one, p, d, r, phase): (d, r, n) for d, r, n in combos}
                try:
                    for f in as_completed(futs):
                        d, r, n = futs[f]
                        done += 1
                        try:
                            emit("result", {"dep": d.isoformat(), "ret": "" if p["oneway"] else r.isoformat(), "nights": n,
                                            "itineraries": f.result()[0], "cached": f.result()[1],
                                            "done": done, "phase": phase})
                        except (BrokenPipeError, ConnectionResetError):
                            raise
                        except Exception as e:  # noqa: BLE001
                            emit("warn", {"dep": d.isoformat(), "ret": "" if p["oneway"] else r.isoformat(), "message": str(e), "done": done})
                            if "API-Key" in str(e) or " 401" in str(e) or " 402" in str(e):
                                stop = True
                                for g in futs:
                                    g.cancel()
                                break
                except (BrokenPipeError, ConnectionResetError):
                    for g in futs:
                        g.cancel()
                    return
        emit("done", {"done": done, "api_calls": _usage().get("total", 0) - used_before})


if __name__ == "__main__":
    print("Feriendorf Flight Agent läuft auf http://%s:%d  (Key gesetzt: %s, Zugangscode: %s, öffentlicher Modus: %s)"
          % ("localhost" if HOST == "127.0.0.1" else HOST, PORT, bool(load_key()), bool(env_var("ACCESS_CODE")), public_mode()))
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
