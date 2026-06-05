# SnipAI — Chrome extension

Snip a region of the current tab, watch a live preview of it in a side panel,
and let an AI answer what it sees. **You decide when the AI looks:** click
**Analyze now** for a single on-demand answer, or turn **Live** on to answer
automatically — sending a new frame **only when the region changes**.

This is a from-scratch rebuild of the desktop app's idea as a Manifest V3
Chrome extension. Because a browser extension is sandboxed, it differs from the
desktop app in a few ways:

- It captures the **active browser tab**, not arbitrary apps/windows.
- The "logged-in Claude CLI" and "Ollama" providers are gone (no local
  processes from a browser). Supported AIs: **Claude (API key)**, **OpenAI
  (GPT)**, **Google (Gemini)**.
- API keys are stored in `chrome.storage.local`, per provider.

## Install (developer / unpacked)

1. Generate the icons once (needs Pillow, which the desktop app already uses):

   ```powershell
   py extension/make_icons.py
   ```

2. Open `chrome://extensions` in Chrome.
3. Turn on **Developer mode** (top-right).
4. Click **Load unpacked** and select this `extension/` folder.
5. Click the SnipAI toolbar icon to open the **side panel**.

## Use

1. Open a normal website tab (the panel can't capture `chrome://` or the Web
   Store pages).
2. Pick an **AI**, fill in the **Model** if you want a non-default one, paste
   your **API key**, and click **Save key**. Use **Get key →** to open the
   provider's key page.
3. Optionally edit **Instructions for the AI**. When you stop typing, the
   extension automatically re-answers the current region with the new
   instructions (no need to toggle Live).
4. Click **Select region**, then drag a box over the area to watch on the
   screenshot shown in the panel (Esc or click outside to cancel).
5. The **Live preview** updates about once a second.
6. Decide when the AI analyzes the region:
   - **Analyze now** — answers the current region once, right when you click.
     This is the manual mode: nothing is sent to the AI until you ask.
   - **Live: OFF → ON** — the extension watches the region and sends a frame to
     the AI whenever the contents change, then shows the answer. Click again to
     stop.

## Notes & tuning

- `chrome.tabs.captureVisibleTab` is rate-limited, so the capture/preview loop
  runs about once per second (`LOOP_MS` in `sidepanel.js`).
- Change sensitivity (for Live mode) is `CHANGE_THRESHOLD`; after an error (e.g.
  HTTP 429 rate limit) live sends pause for `ERROR_COOLDOWN_MS`. The pause after
  typing instructions before auto-answering is `INSTRUCTIONS_IDLE_MS`. All are at
  the top of `sidepanel.js`.
- Each **Analyze now** click, and each detected change in Live mode, is one paid
  API call on your account.
- Keys never leave your browser except in the request to the provider you
  chose. The Anthropic call sets `anthropic-dangerous-direct-browser-access` so
  it works from a browser context.

## Files

| File | Role |
| --- | --- |
| `manifest.json` | MV3 manifest; registers the side panel + service worker |
| `background.js` | Opens the side panel when the toolbar icon is clicked |
| `sidepanel.html/.css` | The panel UI |
| `sidepanel.js` | Capture, region select, on-demand + live answering |
| `providers.js` | Provider table + Claude/OpenAI/Gemini API calls |
| `make_icons.py` | Generates `icons/icon{16,48,128}.png` |
