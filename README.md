# P-StreamRec

[![License: Non-Commercial](https://img.shields.io/badge/License-Non--Commercial-red.svg)](LICENSE)
[![Docker](https://img.shields.io/badge/Docker-Ready-blue.svg)](https://www.docker.com/)
[![Open Source](https://img.shields.io/badge/Open%20Source-Yes-green.svg)](https://github.com/raccommode/P-StreamRec)

**Automatic Chaturbate & m3u8 stream recorder with a modern web interface.**

## Features

- **24/7 automatic recording** — monitors models and records when they go live
- **Auto MP4 conversion** — converts TS to compressed MP4 in background (50-70% smaller)
- **Discover** — browse live Chaturbate models with gender, tag, and search filters
- **Following** — sync and view your followed models from your Chaturbate account
- **Recordings** — manage all recordings with built-in video player
- **Live Watch** — watch streams directly in the browser with HLS player
- **Chaturbate auth** — login for better stream quality and followed models sync
- **FlareSolverr** — automatic Cloudflare bypass via dedicated container
- **Settings** — manage account, FlareSolverr status, tag blacklist
- **Password protection** — optional login to secure the interface
- **GitOps updates** — update the app directly from the UI
- **Docker ready** — one command to get started

## Screenshots

| Discover | Following | Recordings |
|----------|-----------|------------|
| ![Discover](discover.png) | ![Following](following.png) | ![Recordings](recordings.png) |

## Quick Start

### UmbrelOS (one-click install)

P-StreamRec ships as an Umbrel Community App Store. On your Umbrel:

1. Open the **App Store**, click the store menu, then **Community App Stores → Add**
2. Paste the repo URL: `https://github.com/raccommode/P-StreamRec`
3. Install **P-StreamRec** from the store

FlareSolverr is bundled inside the app — no extra setup required. Recordings are stored in `${APP_DATA_DIR}/data` on your Umbrel.

### Docker Compose (recommended, includes FlareSolverr)

```yaml
version: "3.8"
services:
  flaresolverr:
    image: ghcr.io/flaresolverr/flaresolverr:latest
    environment:
      - LOG_LEVEL=info
    ports:
      - "8191:8191"
    restart: unless-stopped

  p-streamrec:
    image: ghcr.io/raccommode/p-streamrec:latest
    depends_on:
      - flaresolverr
    environment:
      - CB_RESOLVER_ENABLED=true
      - FLARESOLVERR_URL=http://flaresolverr:8191
    ports:
      - "8080:8080"
    volumes:
      - ./data:/data
    restart: unless-stopped
```

### Docker Run (simple)

```bash
docker run -d --name p-streamrec \
  -p 8080:8080 -v ./data:/data \
  -e CB_RESOLVER_ENABLED=true \
  ghcr.io/raccommode/p-streamrec:latest
```

**Access:** `http://localhost:8080`

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `OUTPUT_DIR` | `/data` | Recordings folder |
| `PORT` | `8080` | Web interface port |
| `FFMPEG_PATH` | `ffmpeg` | Path to FFmpeg |
| `CB_RESOLVER_ENABLED` | `true` | Enable Chaturbate support |
| `CB_REQUEST_DELAY` | `1.0` | Delay between Chaturbate requests (seconds) |
| `PASSWORD` | — | Password to protect the interface (optional) |
| `AUTO_RECORD_USERS` | — | Comma-separated usernames to auto-record |
| `CHATURBATE_USERNAME` | — | Chaturbate login (optional, enables Following + better quality) |
| `CHATURBATE_PASSWORD` | — | Chaturbate password (optional) |
| `FLARESOLVERR_URL` | — | FlareSolverr URL (e.g. `http://flaresolverr:8191`) |
| `TZ` | `UTC` | Timezone (e.g. `America/Toronto`) |

## Usage

1. **Add a model** — click **+**, enter a Chaturbate username or m3u8 URL
2. **Auto-record** — the system checks every 2 minutes and records when live
3. **Auto-convert** — when the stream ends, TS is converted to MP4 automatically
4. **Watch live** — click a model card to open the live player
5. **Browse replays** — go to the Recordings page to watch or delete recordings

### Recording format

- Original: `/data/records/<username>/YYYYMMDD_HHMMSS_ID.ts` (MPEG-TS, lossless)
- Converted: `/data/records/<username>/YYYYMMDD_HHMMSS_ID.mp4` (H.264, auto-generated)

### Storage estimates

| Format | Size per hour |
|--------|---------------|
| TS (original) | ~2–4 GB |
| MP4 (converted) | ~600 MB–1.2 GB |

## Plugins

P-StreamRec ships with a plugin system that lets you add new streaming sources without touching the core. Each source (Chaturbate, and any third-party ones) is an independent plugin with its own manifest and Python module.

### How it works

- **Chaturbate is a plugin** — it's bundled with the app under `plugins/chaturbate/` and auto-installed on first launch. You can disable or uninstall it like any other plugin.
- **Installed plugins** live in `${OUTPUT_DIR}/plugins/<id>/`. Each one is sandboxed to its own folder and data namespace.
- **Go to Settings → Plugins** to see what's installed, enable/disable, or uninstall.

### Installing extra plugins (advanced)

At the bottom of the Plugins tab, click **Advanced plugin options** to reveal:

- **Plugin Catalog** — browse plugins from the official repository and any custom repositories you've added. Click **Install** next to a plugin, then restart the app when prompted.
- **Plugin Repositories** — add a third-party index URL (HTTPS only) to make its plugins appear in the catalog.

> **Security warning:** non-verified plugins execute arbitrary Python code with full access to your server. Only install plugins from authors you trust. The UI explicitly asks you to acknowledge this risk for non-official plugins.

### Writing your own plugin

A plugin is a Python package implementing the `SourcePlugin` protocol — see [app/core/plugin_base.py](app/core/plugin_base.py) for the contract (`resolve()`, `check_status()`, `validate_target()`, manifest schema). A plugin folder contains:

```
my-plugin/
├── manifest.json    # id, name, version, api_version, source_type, capabilities
├── __init__.py      # must expose `plugin` (an instance of your class)
└── plugin.py        # your implementation
```

To publish, host an `index.json` catalog (see [plugins/index.json](plugins/index.json) for the schema) pointing to a `.tar.gz` archive of your plugin, and share the index URL with users.

## Development

```bash
git clone https://github.com/raccommode/P-StreamRec.git
cd P-StreamRec
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

**Stack:** FastAPI, SQLite (aiosqlite), HLS.js, FFmpeg, Docker

## License

**Non-Commercial Open Source License** — See [LICENSE](LICENSE)

Free to use, modify, and distribute — **no commercial use** — share modifications under same license — attribution required
