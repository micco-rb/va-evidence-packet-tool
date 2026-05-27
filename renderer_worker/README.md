# Renderer Worker

Runs a local Playwright/Chromium PDF service so the Replit app can generate
true browser-print-quality article PDFs without fighting container limits.

## How it works

```
Replit Flask UI
  → POST /render {"url": "..."} → this worker
  → Chromium opens the page, prints to PDF
  → returns PDF bytes
  → Replit merges into evidence packet
```

If the worker is unreachable, Replit falls back to WeasyPrint automatically.

---

## Quick start (3 steps)

### macOS / Linux

```bash
cd renderer_worker
chmod +x start.sh
./start.sh
```

### Windows

Double-click `start.bat`  
or run in a terminal:

```cmd
cd renderer_worker
start.bat
```

The worker starts on **http://localhost:7777** and stays running.

---

## Tell Replit where to find it

1. Open your Replit project
2. Go to **Secrets** (lock icon in the sidebar)
3. Add a secret:
   - **Key:** `RENDERER_URL`
   - **Value:** `http://YOUR_LOCAL_IP:7777`

> Use your machine's **local network IP** (e.g. `192.168.1.42`), not
> `localhost`, because Replit runs in the cloud and can't reach your
> `localhost` directly.  
> Find your IP: `ipconfig` (Windows) or `ifconfig` / `ip a` (Mac/Linux).

If you're running on the same machine as Replit Desktop or a tunneling tool
(e.g. ngrok), `localhost` may work — or use an ngrok tunnel URL instead.

---

## Using ngrok (recommended for remote access)

```bash
# In a separate terminal, after the worker is running:
ngrok http 7777
```

Copy the `https://xxxx.ngrok.io` URL and use it as `RENDERER_URL` in Replit Secrets.

---

## Verify it's working

```bash
curl http://localhost:7777/health
# → {"ok": true, "service": "renderer-worker", "version": "1.0"}
```
