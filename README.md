# RSSVibes

A personal, **Netvibes- and DashDork-inspired** RSS reader / start page that runs
entirely on your own machine. Tabbed pages, a drag-and-drop grid of widgets, RSS/Atom
feed portlets, notes, a clock and bookmarks — all saved locally to `data/state.json`.
No account, no cloud, no external services (except the feeds you choose to follow).

![Made with Python stdlib](https://img.shields.io/badge/backend-Python%20stdlib-3776ab)
![No dependencies](https://img.shields.io/badge/dependencies-none-2fa84f)
![License: MIT](https://img.shields.io/badge/license-MIT-blue)

## Run it

Double-click **`start.bat`**, or from a terminal:

```bash
"C:\msys64\mingw64\bin\python.exe" server.py
```

Then open <http://127.0.0.1:8787/>. `start.bat` opens it for you.

Requires only Python 3 (uses the standard library — no `pip install`). It's wired to the
Python that ships with your MSYS2 install; any Python 3.8+ works if you'd rather use another.

## Why a local server (and not just an HTML file)

Browsers block cross-origin requests, so a pure static page can't fetch most RSS feeds.
`server.py` fetches and parses feeds **server-side** and hands the browser clean JSON,
which is what makes the feeds actually load. It also:

- discovers a site's feed when you paste its homepage URL (`/api/discover`)
- caches feed responses for ~90s to be polite to publishers
- stores your whole dashboard in `data/state.json` (export/import from ⚙ Settings)

## Using it

| Action | How |
| --- | --- |
| Add a feed | **＋ Feed** — paste a feed URL *or* a site homepage |
| Add a widget | **＋ Widget** — Feed, Notes, Clock or Bookmarks |
| Rearrange | Drag a widget by its **header** between columns |
| New page | **＋** next to the tabs (double-click a tab to rename) |
| Theme / accent / columns | **⚙** in the top-right |
| Read an article | Click an item → reading pane slides in |
| Import subscriptions | **⚙ → Import Netvibes / OPML** — a Netvibes `.zip`/`.opml` export or any OPML file |

## Importing from Netvibes (or any OPML)

Migrating from Netvibes? Export your subscriptions (Netvibes gives you a `.zip`
containing an OPML file), then open **⚙ Settings → Import Netvibes / OPML** and pick the
file. RSSVibes reads the zip directly — no need to unpack it — and recreates each Netvibes
**tab as a page**, preserving the column and row layout of every feed. Netvibes-specific
widgets that aren't RSS feeds (saved searches, etc.) are skipped, and imported feeds are
**added** as new pages so your existing dashboard is left intact.

Plain OPML exports from Feedly, Inoreader, The Old Reader and similar readers work too.

## Files

```
server.py        local server: static files + feed proxy + state storage
web/index.html   app shell
web/styles.css   theme (light/dark + accent colors)
web/app.js       dashboard logic (state, drag-drop, feeds, reader, modals)
data/state.json  your saved dashboard (created on first run)
```

## Notes

- The server binds to `127.0.0.1` only — it is not reachable from other machines.
- Change the port with an env var: `PORT=9000 python server.py`.
- Bookmark favicons load from Google's public favicon service; everything else is local.
