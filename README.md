# AutoAnswer

A Chrome extension that watches a region of the current tab and lets an AI
answer what it sees — **live**, sending a new frame only when the region
changes.

Pick a rectangle on the active tab, watch a live preview of it in a side panel,
and an AI vision model answers whatever question or problem appears there.

Supported AIs (each needs your own API key): **Claude**, **OpenAI (GPT)**,
**Google (Gemini)**.

## Install (unpacked)

1. Generate the icons once (needs [Pillow](https://pypi.org/project/Pillow/) —
   `py -m pip install pillow`):

   ```powershell
   py extension/make_icons.py
   ```

2. Open `chrome://extensions`, enable **Developer mode** (top-right).
3. Click **Load unpacked** and select the `extension/` folder.
4. Click the AutoAnswer toolbar icon to open the **side panel**.

## Use

1. Open a normal website tab (Chrome blocks capturing `chrome://` and Web Store
   pages).
2. Pick an **AI**, set the **Model** if you want a non-default one, paste your
   **API key**, and click **Save key** (**Get key →** opens the provider's key
   page).
3. Optionally edit **Instructions for the AI**.
4. Click **Select region** and drag a box over the area to watch (Esc or click
   outside to cancel).
5. The **Live preview** updates about once a second.
6. Toggle **Live: OFF → ON**. While on, the extension sends a frame to the AI
   whenever the region's contents change, and shows the answer. Toggle off to
   stop.

## Notes

- Keys are stored per-provider in `chrome.storage.local` and only leave the
  browser in the request to the provider you chose.
- `chrome.tabs.captureVisibleTab` is rate-limited, so the capture/preview loop
  runs ~once per second. Tune `LOOP_MS`, `CHANGE_THRESHOLD`, and
  `ERROR_COOLDOWN_MS` at the top of `extension/sidepanel.js`.
- Each detected change is one paid API call on your account.

See [`extension/README.md`](extension/README.md) for the file-by-file layout.
```
