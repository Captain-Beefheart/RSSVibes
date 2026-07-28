#!/usr/bin/env python3
"""
Local RSS dashboard server (Netvibes / Dashdork style).

Runs entirely on the Python standard library:
  - serves the dashboard UI from ./web
  - proxies + parses RSS/Atom/RDF feeds server-side (browsers can't, due to CORS)
  - discovers a site's feed from its homepage URL
  - persists the whole dashboard as ./data/state.json

Bind is 127.0.0.1 only: this is a single-user local app.
"""

import gzip
import html
import io
import json
import os
import re
import socket
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import zipfile
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape, quoteattr

FROZEN = getattr(sys, "frozen", False)   # running as a PyInstaller .exe / AppImage?


def _bundle_dir():
    """Directory holding bundled assets (web/). PyInstaller extracts to _MEIPASS."""
    if FROZEN:
        return getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(sys.executable)))
    return os.path.dirname(os.path.abspath(__file__))


def _data_dir():
    """Writable location for state.json. Next to the source when running as a
    script; a per-user data dir when packaged (the bundle is read-only/temporary)."""
    if not FROZEN:
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return os.path.join(base, "RSSVibes")


BASE_DIR = _bundle_dir()
WEB_DIR = os.path.join(BASE_DIR, "web")
DATA_DIR = _data_dir()
STATE_FILE = os.path.join(DATA_DIR, "state.json")

HOST = "127.0.0.1"
PORT = int(os.environ.get("PORT", "8787"))

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) LocalDashboard/1.0 "
    "(+https://localhost) Python-urllib"
)
FETCH_TIMEOUT = 12  # seconds
CACHE_TTL = 90      # seconds; be polite to feed servers

# ---------------------------------------------------------------------------
# Small in-memory cache so rapid refreshes don't hammer feed servers.
# ---------------------------------------------------------------------------
_cache = {}
_cache_lock = threading.Lock()


def cache_get(key, ttl=CACHE_TTL):
    with _cache_lock:
        hit = _cache.get(key)
        if hit and (time.time() - hit[0]) < ttl:
            return hit[1]
    return None


def cache_put(key, value):
    with _cache_lock:
        _cache[key] = (time.time(), value)


# ---------------------------------------------------------------------------
# HTTP fetching
# ---------------------------------------------------------------------------
_ssl_ctx = ssl.create_default_context()


def http_get(url, timeout=FETCH_TIMEOUT, max_bytes=5_000_000):
    """Fetch a URL, returning (final_url, content_type, bytes)."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("Only http(s) URLs are allowed")

    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/rss+xml, application/atom+xml, application/xml,"
                      " text/xml, text/html;q=0.9, */*;q=0.8",
            "Accept-Encoding": "gzip, deflate",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout, context=_ssl_ctx) as resp:
        final_url = resp.geturl()
        ctype = resp.headers.get("Content-Type", "")
        raw = resp.read(max_bytes + 1)
        if len(raw) > max_bytes:
            raw = raw[:max_bytes]
        encoding = (resp.headers.get("Content-Encoding") or "").lower()
        if "gzip" in encoding:
            try:
                raw = gzip.decompress(raw)
            except OSError:
                pass
        elif "deflate" in encoding:
            import zlib
            try:
                raw = zlib.decompress(raw)
            except zlib.error:
                try:
                    raw = zlib.decompress(raw, -zlib.MAX_WBITS)
                except zlib.error:
                    pass
    return final_url, ctype, raw


def decode_bytes(raw, ctype=""):
    """Best-effort decode of feed bytes to str."""
    # Honour an explicit charset in the Content-Type header.
    m = re.search(r"charset=([\w-]+)", ctype or "", re.I)
    if m:
        try:
            return raw.decode(m.group(1), errors="replace")
        except LookupError:
            pass
    # Honour an XML declaration encoding.
    head = raw[:200]
    m = re.search(br'encoding=["\']([\w-]+)["\']', head, re.I)
    if m:
        try:
            return raw.decode(m.group(1).decode("ascii", "ignore"), errors="replace")
        except (LookupError, UnicodeDecodeError):
            pass
    for enc in ("utf-8", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Feed parsing (RSS 2.0, RSS 1.0/RDF, Atom) via ElementTree.
# ---------------------------------------------------------------------------
def _local(tag):
    """Strip an XML namespace, returning the local tag name (lowercased)."""
    if tag is None:
        return ""
    if "}" in tag:
        tag = tag.split("}", 1)[1]
    return tag.lower()


def _kids(el):
    """Map local child tag name -> list of child elements."""
    out = {}
    for c in el:
        out.setdefault(_local(c.tag), []).append(c)
    return out


def _text(el):
    return (el.text or "").strip() if el is not None else ""


def _first(kids, *names):
    for n in names:
        if kids.get(n):
            return kids[n][0]
    return None


def _first_text(kids, *names):
    el = _first(kids, *names)
    return _text(el) if el is not None else ""


class _StripHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)

    def text(self):
        return re.sub(r"\s+", " ", "".join(self.parts)).strip()


def strip_html(html):
    if not html:
        return ""
    try:
        p = _StripHTML()
        p.feed(html)
        return p.text()
    except Exception:
        return re.sub(r"<[^>]+>", " ", html)


def first_image(html):
    if not html:
        return ""
    m = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', html, re.I)
    return m.group(1) if m else ""


def parse_date(value):
    """Return (iso_string, epoch_seconds) or ('', 0)."""
    if not value:
        return "", 0
    value = value.strip()
    # RFC 822 (RSS pubDate)
    try:
        dt = parsedate_to_datetime(value)
        if dt is not None:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.isoformat(), dt.timestamp()
    except (TypeError, ValueError):
        pass
    # ISO 8601 (Atom)
    try:
        v = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(v)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.isoformat(), dt.timestamp()
    except ValueError:
        pass
    return value, 0


def _atom_link(entry_kids):
    """Pick the best href from Atom <link> elements."""
    links = entry_kids.get("link", [])
    best = ""
    for ln in links:
        rel = (ln.get("rel") or "alternate").lower()
        href = ln.get("href") or ""
        if not href:
            continue
        if rel == "alternate":
            return href
        if not best:
            best = href
    return best


def _thumb(item_kids, content_html):
    # media:thumbnail / media:content
    for name in ("thumbnail", "content"):
        for el in item_kids.get(name, []):
            url = el.get("url") or ""
            if url and (name == "thumbnail" or (el.get("medium") == "image")):
                return url
    # enclosure image
    for el in item_kids.get("enclosure", []):
        if (el.get("type") or "").startswith("image") and el.get("url"):
            return el.get("url")
    # first <img> in the content
    return first_image(content_html)


def parse_feed(text):
    """Parse feed XML text into a normalized dict."""
    text = text.lstrip("﻿ \r\n\t")
    root = ET.fromstring(text)
    rtag = _local(root.tag)

    feed = {"title": "", "link": "", "items": []}

    if rtag == "feed":  # Atom
        rk = _kids(root)
        feed["title"] = _first_text(rk, "title")
        feed["link"] = _atom_link(rk)
        entries = rk.get("entry", [])
        for e in entries:
            ek = _kids(e)
            content_el = _first(ek, "content", "summary")
            content = _text(content_el)
            feed["items"].append({
                "title": _first_text(ek, "title") or "(untitled)",
                "link": _atom_link(ek),
                "summary": content,
                "author": _atom_author(ek),
                **_date_fields(_first_text(ek, "updated", "published", "issued")),
                "id": _first_text(ek, "id") or _atom_link(ek),
                "thumb": _thumb(ek, content),
            })
        return feed

    # RSS 2.0 (<rss><channel>) or RDF/RSS 1.0 (<rdf:RDF>)
    rk = _kids(root)
    channel = _first(rk, "channel")
    if channel is not None:
        ck = _kids(channel)
        feed["title"] = _first_text(ck, "title")
        feed["link"] = _first_text(ck, "link")
        items = ck.get("item", [])
    else:
        items = []
    # RSS 1.0 keeps <item> as siblings of <channel> under <rdf:RDF>.
    if not items and rk.get("item"):
        items = rk["item"]

    for it in items:
        ik = _kids(it)
        content = _first_text(ik, "encoded") or _first_text(ik, "description", "summary")
        link = _first_text(ik, "link")
        if not link:
            guid_el = _first(ik, "guid")
            if guid_el is not None and (guid_el.get("isPermaLink") != "false"):
                link = _text(guid_el)
        feed["items"].append({
            "title": _first_text(ik, "title") or "(untitled)",
            "link": link,
            "summary": content,
            "author": _first_text(ik, "creator", "author"),
            **_date_fields(_first_text(ik, "pubdate", "date", "published", "updated")),
            "id": _first_text(ik, "guid") or link or _first_text(ik, "title"),
            "thumb": _thumb(ik, content),
        })
    return feed


def _atom_author(ek):
    a = _first(ek, "author")
    if a is not None:
        return _first_text(_kids(a), "name")
    return ""


def _date_fields(raw):
    iso, epoch = parse_date(raw)
    return {"date": iso, "ts": epoch}


# ---------------------------------------------------------------------------
# Feed discovery: given a homepage URL, find its RSS/Atom link.
# ---------------------------------------------------------------------------
class _FeedLinkFinder(HTMLParser):
    def __init__(self):
        super().__init__()
        self.feeds = []

    def handle_starttag(self, tag, attrs):
        if tag != "link":
            return
        a = dict(attrs)
        rel = (a.get("rel") or "").lower()
        typ = (a.get("type") or "").lower()
        if "alternate" in rel and ("rss" in typ or "atom" in typ or "xml" in typ):
            href = a.get("href")
            if href:
                self.feeds.append({"href": href, "title": a.get("title") or ""})


def discover_feed(url):
    final_url, ctype, raw = http_get(url)
    text = decode_bytes(raw, ctype)
    # Already a feed?
    stripped = text.lstrip("﻿ \r\n\t")[:400].lower()
    if stripped.startswith("<?xml") or "<rss" in stripped or "<feed" in stripped or "<rdf" in stripped:
        return [final_url]
    finder = _FeedLinkFinder()
    try:
        finder.feed(text)
    except Exception:
        pass
    resolved = [urllib.parse.urljoin(final_url, f["href"]) for f in finder.feeds]
    # Common fallbacks if the page declared nothing.
    if not resolved:
        for guess in ("/feed", "/rss", "/rss.xml", "/atom.xml", "/index.xml", "/feed.xml"):
            resolved.append(urllib.parse.urljoin(final_url, guess))
    # de-dup, keep order
    seen, out = set(), []
    for u in resolved:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


# ---------------------------------------------------------------------------
# Weather (Open-Meteo — free, no API key, includes geocoding).
# ---------------------------------------------------------------------------
WMO = {
    0: ("Clear sky", "☀️"),
    1: ("Mainly clear", "🌤️"), 2: ("Partly cloudy", "⛅"), 3: ("Overcast", "☁️"),
    45: ("Fog", "🌫️"), 48: ("Rime fog", "🌫️"),
    51: ("Light drizzle", "🌦️"), 53: ("Drizzle", "🌦️"), 55: ("Heavy drizzle", "🌦️"),
    56: ("Freezing drizzle", "🌧️"), 57: ("Freezing drizzle", "🌧️"),
    61: ("Light rain", "🌦️"), 63: ("Rain", "🌧️"), 65: ("Heavy rain", "🌧️"),
    66: ("Freezing rain", "🌧️"), 67: ("Freezing rain", "🌧️"),
    71: ("Light snow", "🌨️"), 73: ("Snow", "🌨️"), 75: ("Heavy snow", "❄️"), 77: ("Snow grains", "🌨️"),
    80: ("Light showers", "🌦️"), 81: ("Showers", "🌧️"), 82: ("Violent showers", "⛈️"),
    85: ("Snow showers", "🌨️"), 86: ("Snow showers", "🌨️"),
    95: ("Thunderstorm", "⛈️"), 96: ("Thunderstorm, hail", "⛈️"), 99: ("Thunderstorm, hail", "⛈️"),
}


def _wmo(code):
    try:
        return WMO.get(int(code), ("—", "🌡️"))
    except (TypeError, ValueError):
        return ("—", "🌡️")


def fetch_weather(location="", lat="", lon="", units="metric"):
    place = location
    if not (lat and lon):
        if not location:
            raise ValueError("Enter a location.")
        geo_url = "https://geocoding-api.open-meteo.com/v1/search?" + urllib.parse.urlencode(
            {"name": location, "count": 1, "language": "en", "format": "json"})
        _, ctype, raw = http_get(geo_url, timeout=10)
        results = (json.loads(decode_bytes(raw, ctype)) or {}).get("results") or []
        if not results:
            raise ValueError("Location not found: %s" % location)
        g = results[0]
        lat, lon = g["latitude"], g["longitude"]
        parts = [g.get("name")]
        if g.get("admin1") and g.get("admin1") != g.get("name"):
            parts.append(g.get("admin1"))
        if g.get("country"):
            parts.append(g.get("country"))
        place = ", ".join(p for p in parts if p)

    params = {
        "latitude": lat, "longitude": lon,
        "current": "temperature_2m,relative_humidity_2m,apparent_temperature,weather_code,wind_speed_10m",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min",
        "timezone": "auto", "forecast_days": 4,
    }
    if units == "imperial":
        params["temperature_unit"] = "fahrenheit"
        params["wind_speed_unit"] = "mph"
    _, ctype, raw = http_get("https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode(params), timeout=10)
    data = json.loads(decode_bytes(raw, ctype))

    cur = data.get("current") or {}
    cunits = data.get("current_units") or {}
    desc, icon = _wmo(cur.get("weather_code"))

    d = data.get("daily") or {}
    dates = d.get("time") or []
    codes = d.get("weather_code") or []
    tmax = d.get("temperature_2m_max") or []
    tmin = d.get("temperature_2m_min") or []
    daily = []
    for i, dt in enumerate(dates):
        ddesc, dicon = _wmo(codes[i] if i < len(codes) else None)
        daily.append({"date": dt, "desc": ddesc, "icon": dicon,
                      "tmax": tmax[i] if i < len(tmax) else None,
                      "tmin": tmin[i] if i < len(tmin) else None})

    return {
        "location": place, "lat": lat, "lon": lon,
        "current": {"temp": cur.get("temperature_2m"), "feels": cur.get("apparent_temperature"),
                    "desc": desc, "icon": icon,
                    "humidity": cur.get("relative_humidity_2m"), "wind": cur.get("wind_speed_10m")},
        "daily": daily,
        "units": {"temp": cunits.get("temperature_2m") or ("°F" if units == "imperial" else "°C"),
                  "wind": cunits.get("wind_speed_10m") or ("mph" if units == "imperial" else "km/h")},
    }


# ---------------------------------------------------------------------------
# OPML import (Netvibes exports + any standard OPML from other readers).
#
# Netvibes shape: <body> holds one <outline> per tab (with cols/layout attrs);
# feed outlines nested inside carry xmlUrl/htmlUrl/type + col/row positions.
# Non-feed widgets (type="ExtendedVibes", searches, etc.) have no xmlUrl and
# are skipped. Plain OPML from Feedly/Inoreader/etc. uses the same <outline>
# grammar, so this importer handles those too.
# ---------------------------------------------------------------------------
def _opml_int(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def _opml_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _cols_from_layout(layout):
    if not layout:
        return 0
    m = re.match(r"\s*(\d+)", layout)
    return int(m.group(1)) if m else 0


def _clean(text):
    # Netvibes double-encodes some titles (e.g. "&amp;amp;", "Home&amp;#x2F;Gardening"),
    # so after the XML parser's single decode a second unescape restores the real text.
    # A no-op for well-formed OPML from other readers.
    return html.unescape((text or "").strip())


def _feed_from_outline(el):
    return {
        "title": _clean(el.get("title") or el.get("text")),
        "url": el.get("xmlUrl") or el.get("xmlurl"),
        "htmlUrl": el.get("htmlUrl") or el.get("htmlurl") or "",
        "col": _opml_int(el.get("col")),
        "row": _opml_float(el.get("row")),
    }


def parse_opml(text):
    text = text.lstrip("﻿ \r\n\t")
    root = ET.fromstring(text)

    body = next((el for el in root.iter() if _local(el.tag) == "body"), None)
    if body is None:
        raise ValueError("Not an OPML document (no <body> element).")
    head_title = next((_text(el) for el in root.iter() if _local(el.tag) == "title"), "")

    skipped = [0]

    def collect_feeds(el):
        feeds = []
        for child in el:
            if _local(child.tag) != "outline":
                continue
            if child.get("xmlUrl") or child.get("xmlurl"):
                feeds.append(_feed_from_outline(child))
            elif len(child):                 # container (e.g. ExtendedVibes) -> recurse
                feeds.extend(collect_feeds(child))
            else:                            # leaf widget with no feed URL -> skipped
                skipped[0] += 1
        return feeds

    pages, orphans = [], []
    for top in body:
        if _local(top.tag) != "outline":
            continue
        if top.get("xmlUrl") or top.get("xmlurl"):   # a feed at the top level
            orphans.append(_feed_from_outline(top))
            continue
        name = _clean(top.get("title") or top.get("text")) or "Imported"
        cols = _opml_int(top.get("cols")) or _cols_from_layout(top.get("layout"))
        feeds = collect_feeds(top)
        feeds.sort(key=lambda f: (f["col"] or 1, f["row"]))
        if feeds:
            pages.append({"name": name, "columns": cols, "feeds": feeds})

    if orphans:
        orphans.sort(key=lambda f: (f["col"] or 1, f["row"]))
        pages.insert(0, {"name": head_title or "Imported", "columns": 0, "feeds": orphans})

    feed_count = sum(len(p["feeds"]) for p in pages)
    return {"title": head_title, "pages": pages, "feedCount": feed_count, "skipped": skipped[0]}


def opml_from_upload(raw, content_type=""):
    """Accept a raw .opml/.xml body or a .zip (Netvibes wraps its OPML in a zip)."""
    if raw[:2] == b"PK":  # zip magic
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            names = [n for n in zf.namelist() if n.lower().endswith((".opml", ".xml"))]
            if not names:
                raise ValueError("No .opml or .xml file found inside the zip.")
            text = decode_bytes(zf.read(names[0]))
    else:
        text = decode_bytes(raw, content_type)
    return parse_opml(text)


def build_opml(state):
    """Serialize RSSVibes state to Netvibes-compatible OPML.

    OPML is a feed-subscription format, so only feed widgets are exported (one
    <outline> per feed, grouped by page, positioned by col/row). Notes/clock/
    bookmark widgets have no OPML equivalent and are omitted. The output parses
    cleanly back through parse_opml(), so RSSVibes round-trips its own exports.
    """
    settings = state.get("settings") or {}
    try:
        default_cols = max(1, int(settings.get("columns", 3) or 3))
    except (TypeError, ValueError):
        default_cols = 3
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    out = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<opml version="1.0">',
        ' <head>',
        '  <title>%s</title>' % escape(str(settings.get("brand") or "RSSVibes")),
        '  <type>Private</type>',
        '  <creationDate>%s</creationDate>' % stamp,
        ' </head>',
        ' <body>',
    ]

    for tab in state.get("tabs") or []:
        feeds = [w for w in (tab.get("widgets") or [])
                 if w.get("type") == "feed" and w.get("url")]
        if not feeds:
            continue  # OPML is feed-only; skip pages with no feeds
        try:
            tcols = max(1, min(5, int(tab.get("columns") or default_cols)))
        except (TypeError, ValueError):
            tcols = default_cols
        out.append('  <outline title=%s cols="%d" layout="%d-0">'
                   % (quoteattr(str(tab.get("name") or "Imported")), tcols, tcols))
        rows = {}
        for w in feeds:
            try:
                col = int(w.get("col", 0)) + 1        # OPML columns are 1-based
            except (TypeError, ValueError):
                col = 1
            col = max(1, col)
            rows[col] = rows.get(col, 0) + 1          # preserve in-column order via row
            attrs = [
                'title=%s' % quoteattr(str(w.get("title") or "")),
                'xmlUrl=%s' % quoteattr(str(w.get("url"))),
            ]
            if w.get("htmlUrl"):
                attrs.append('htmlUrl=%s' % quoteattr(str(w.get("htmlUrl"))))
            attrs.append('type="rss"')
            attrs.append('row="%d"' % (rows[col] * 100))
            attrs.append('col="%d"' % col)
            out.append('   <outline %s/>' % " ".join(attrs))
        out.append('  </outline>')

    out.append(' </body>')
    out.append('</opml>')
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------
_state_lock = threading.Lock()


def load_state():
    with _state_lock:
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, "r", encoding="utf-8") as fh:
                    return json.load(fh)
            except (OSError, json.JSONDecodeError):
                pass
    return None


def save_state(obj):
    with _state_lock:
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, STATE_FILE)


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".png": "image/png",
}


class Handler(BaseHTTPRequestHandler):
    server_version = "LocalDashboard/1.0"
    protocol_version = "HTTP/1.1"

    def handle(self):
        # Browsers freely drop keep-alive sockets; that's normal, not an error.
        try:
            super().handle()
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
            pass

    def log_message(self, fmt, *args):
        sys.stderr.write("  %s - %s\n" % (self.address_string(), fmt % args))

    # -- helpers -----------------------------------------------------------
    def _send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, body, ctype, status=200, cache=True):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if not cache:
            self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _read_body(self):
        length = int(self.headers.get("Content-Length", "0") or "0")
        return self.rfile.read(length) if length else b""

    # -- routing -----------------------------------------------------------
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        if path == "/api/feed":
            return self.api_feed(qs)
        if path == "/api/discover":
            return self.api_discover(qs)
        if path == "/api/weather":
            return self.api_weather(qs)
        if path == "/api/state":
            return self.api_state_get()
        return self.serve_static(path)

    def do_HEAD(self):
        self.do_GET()

    def do_PUT(self):
        if urllib.parse.urlparse(self.path).path == "/api/state":
            return self.api_state_put()
        self.send_error(404)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/import":
            return self.api_import()
        if path == "/api/export":
            return self.api_export()
        if path == "/api/quit":
            return self.api_quit()
        if path == "/api/state":
            return self.api_state_put()
        self.send_error(404)

    def api_quit(self):
        """Stop the server (from the in-app 'Stop server' button)."""
        self._read_body()  # drain any body (e.g. a sendBeacon save fired first)
        self._send_json({"ok": True, "stopping": True})
        # shutdown() must run off the serve_forever thread; this handler is on a
        # worker thread, so a short-lived helper thread does the job after the
        # response has flushed.
        srv = self.server

        def _stop():
            time.sleep(0.3)
            srv.shutdown()
        threading.Thread(target=_stop, daemon=True).start()

    # -- API ---------------------------------------------------------------
    def api_feed(self, qs):
        url = (qs.get("url") or [""])[0].strip()
        if not url:
            return self._send_json({"error": "missing url"}, 400)
        cached = cache_get(url)
        if cached is not None:
            return self._send_json(cached)
        try:
            final_url, ctype, raw = http_get(url)
            text = decode_bytes(raw, ctype)
            feed = parse_feed(text)
            feed["feedUrl"] = final_url
            feed["fetched"] = datetime.now(timezone.utc).isoformat()
            cache_put(url, feed)
            return self._send_json(feed)
        except ET.ParseError as e:
            return self._send_json({"error": "Not a valid feed: %s" % e}, 502)
        except urllib.error.HTTPError as e:
            return self._send_json({"error": "HTTP %s from feed" % e.code}, 502)
        except (urllib.error.URLError, socket.timeout, ValueError) as e:
            return self._send_json({"error": str(getattr(e, "reason", e))}, 502)
        except Exception as e:  # last-resort guard so one bad feed can't 500 loudly
            return self._send_json({"error": "%s: %s" % (type(e).__name__, e)}, 502)

    def api_discover(self, qs):
        url = (qs.get("url") or [""])[0].strip()
        if not url:
            return self._send_json({"error": "missing url"}, 400)
        if not re.match(r"^https?://", url, re.I):
            url = "https://" + url
        try:
            candidates = discover_feed(url)
        except Exception as e:
            return self._send_json({"error": str(e)}, 502)
        # Validate candidates by trying to parse each until one works.
        for cand in candidates[:6]:
            try:
                fin, ctype, raw = http_get(cand)
                feed = parse_feed(decode_bytes(raw, ctype))
                if feed.get("items") is not None:
                    feed["feedUrl"] = fin
                    return self._send_json({"found": fin, "title": feed.get("title", ""),
                                            "count": len(feed["items"])})
            except Exception:
                continue
        return self._send_json({"error": "No feed found at that URL"}, 404)

    def api_weather(self, qs):
        location = (qs.get("location") or [""])[0].strip()
        units = (qs.get("units") or ["metric"])[0]
        lat = (qs.get("lat") or [""])[0].strip()
        lon = (qs.get("lon") or [""])[0].strip()
        if not location and not (lat and lon):
            return self._send_json({"error": "Enter a location."}, 400)
        key = "wx:%s:%s:%s:%s" % (location.lower(), lat, lon, units)
        cached = cache_get(key, 600)  # weather changes slowly; cache 10 min
        if cached is not None:
            return self._send_json(cached)
        try:
            result = fetch_weather(location, lat, lon, units)
            cache_put(key, result)
            return self._send_json(result)
        except ValueError as e:
            return self._send_json({"error": str(e)}, 400)
        except urllib.error.HTTPError as e:
            return self._send_json({"error": "Weather service returned HTTP %s" % e.code}, 502)
        except (urllib.error.URLError, socket.timeout) as e:
            return self._send_json({"error": str(getattr(e, "reason", e))}, 502)
        except Exception as e:
            return self._send_json({"error": "%s: %s" % (type(e).__name__, e)}, 502)

    def api_state_get(self):
        state = load_state()
        if state is None:
            return self._send_json({"empty": True})
        return self._send_json(state)

    def api_state_put(self):
        try:
            obj = json.loads(self._read_body() or b"{}")
        except json.JSONDecodeError as e:
            return self._send_json({"error": "bad json: %s" % e}, 400)
        save_state(obj)
        return self._send_json({"ok": True})

    def api_import(self):
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length > 25_000_000:
            return self._send_json({"error": "File too large (max 25 MB)."}, 413)
        raw = self._read_body()
        if not raw:
            return self._send_json({"error": "No file received."}, 400)
        try:
            result = opml_from_upload(raw, self.headers.get("Content-Type", ""))
            return self._send_json(result)
        except zipfile.BadZipFile:
            return self._send_json({"error": "That zip file appears to be corrupt."}, 400)
        except ET.ParseError as e:
            return self._send_json({"error": "Invalid OPML/XML: %s" % e}, 400)
        except ValueError as e:
            return self._send_json({"error": str(e)}, 400)
        except Exception as e:
            return self._send_json({"error": "%s: %s" % (type(e).__name__, e)}, 500)

    def api_export(self):
        try:
            state = json.loads(self._read_body() or b"{}")
        except json.JSONDecodeError as e:
            return self._send_json({"error": "bad json: %s" % e}, 400)
        try:
            opml = build_opml(state)
        except Exception as e:
            return self._send_json({"error": "%s: %s" % (type(e).__name__, e)}, 500)
        body = opml.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/x-opml; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Disposition", 'attachment; filename="rssvibes-subscriptions.opml"')
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    # -- static ------------------------------------------------------------
    def serve_static(self, path):
        if path == "/":
            path = "/index.html"
        # Prevent path traversal.
        rel = os.path.normpath(path.lstrip("/")).replace("\\", "/")
        if rel.startswith("..") or os.path.isabs(rel):
            return self.send_error(403)
        full = os.path.join(WEB_DIR, rel)
        if not os.path.isfile(full):
            return self.send_error(404)
        ext = os.path.splitext(full)[1].lower()
        ctype = CONTENT_TYPES.get(ext, "application/octet-stream")
        try:
            with open(full, "rb") as fh:
                body = fh.read()
        except OSError:
            return self.send_error(404)
        # HTML/JS/CSS shouldn't be cached during development.
        self._send_bytes(body, ctype, cache=(ext in (".png", ".ico", ".svg")))


def open_browser(url):
    threading.Thread(target=lambda: (time.sleep(0.8), webbrowser.open(url)), daemon=True).start()


def main():
    # A PyInstaller --windowed (.exe) build has no console: sys.stdout/stderr are
    # None. Prints and the request logger (sys.stderr.write) would then raise and
    # crash every request handler mid-response, so route them to a harmless sink.
    _sink = open(os.devnull, "w")
    if sys.stdout is None:
        sys.stdout = _sink
    if sys.stderr is None:
        sys.stderr = _sink

    os.makedirs(DATA_DIR, exist_ok=True)
    url = "http://%s:%d/" % (HOST, PORT)

    try:
        httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError:
        # Port already in use — RSSVibes is probably already running.
        # Just open the browser to the existing instance and exit.
        print("RSSVibes already running; opening %s" % url)
        webbrowser.open(url)
        return

    print("=" * 60)
    print("  RSSVibes running at:")
    print("    " + url)
    print("  Use the ⏻ Stop button in the app, or press Ctrl+C here.")
    print("=" * 60)

    # A packaged app has no console, so open the browser automatically.
    # (Set RSSVIBES_OPEN=1 to force this when running as a script.)
    if FROZEN or os.environ.get("RSSVIBES_OPEN"):
        open_browser(url)

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        httpd.shutdown()
    httpd.server_close()


if __name__ == "__main__":
    main()
