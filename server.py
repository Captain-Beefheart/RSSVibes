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
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from xml.etree import ElementTree as ET

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(BASE_DIR, "web")
DATA_DIR = os.path.join(BASE_DIR, "data")
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


def cache_get(key):
    with _cache_lock:
        hit = _cache.get(key)
        if hit and (time.time() - hit[0]) < CACHE_TTL:
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
        if path == "/api/state":
            return self.api_state_get()
        return self.serve_static(path)

    def do_HEAD(self):
        self.do_GET()

    def do_PUT(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/state":
            return self.api_state_put()
        self.send_error(404)

    def do_POST(self):
        self.do_PUT()

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


def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    url = "http://%s:%d/" % (HOST, PORT)
    print("=" * 60)
    print("  Local RSS Dashboard running at:")
    print("    " + url)
    print("  Press Ctrl+C to stop.")
    print("=" * 60)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        httpd.shutdown()


if __name__ == "__main__":
    main()
