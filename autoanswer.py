"""
AutoAnswer - live screen-region capture that asks an AI to answer what it sees.

Workflow:
  1. Click "Select Region" and drag a rectangle over the part of your screen
     you want to watch (a quiz question, a problem, anything).
  2. A live OBS-style preview of that region updates continuously.
  3. Pick an AI from the dropdown, optionally edit the model and instructions.
  4. Toggle "Live Answer: ON" (or press F9). While on, the app watches the
     region and, whenever its contents change, sends that frame to the chosen
     model's vision API and shows the answer. Toggle it off to stop.

Supported AIs (see the PROVIDERS table below):
  - Claude (logged in) ... uses the local Claude Code CLI, no API key needed
  - Claude (API key) ..... Anthropic API        (ANTHROPIC_API_KEY)
  - OpenAI (GPT) ......... OpenAI API            (OPENAI_API_KEY)
  - Google (Gemini) ...... Gemini API            (GEMINI_API_KEY / GOOGLE_API_KEY)
  - xAI (Grok) ........... xAI API               (XAI_API_KEY)
  - Llama (Ollama) ....... local Ollama server, no API key needed

For the API-key providers, set the matching environment variable, or paste a
key into the key field and click "Save key" (keys are stored per provider in
config.json next to this script). The dropdown loads the right key/model when
you switch providers.
"""

import base64
import io
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
import webbrowser
import tkinter as tk
from tkinter import scrolledtext, messagebox

import mss
from PIL import Image, ImageTk
import anthropic

# --- Make Tk report real (physical) pixels so the grabbed region lines up
# with what you selected, even on high-DPI / scaled displays. Windows only.
try:
    import ctypes
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # per-monitor DPI aware
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

OLLAMA_URL = "http://localhost:11434/api/generate"   # local Llama (Ollama) endpoint
PREVIEW_MAX_W = 520          # max width of the live preview, in px
PREVIEW_FPS = 10
LIVE_CHECK_MS = 1500         # how often, in live mode, to look at the region for changes
CHANGE_THRESHOLD = 8         # mean per-pixel difference (0-255) that counts as "changed"
ERROR_COOLDOWN_S = 20        # after an error (e.g. rate limit), pause live sends this long
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

PROMPT = (
    "The image is a screenshot of something on the user's screen. Read it and "
    "directly answer the question or solve the problem it contains. If it is "
    "multiple choice, give the correct option and a one-line reason. Be concise."
)

# --- Available AI providers. "kind" selects which call path is used; the rest
# is per-provider config. Add a new vision model by adding an entry here.
#   needs_key  : whether an API key field applies
#   default_model : model id pre-filled when this provider is picked
#   env_keys   : environment variables checked for a key (first non-empty wins)
#   endpoint   : HTTP endpoint (openai-compatible providers)
#   key_url    : where to create/copy an API key (opened by "Get key")
PROVIDERS = {
    "Claude (logged in)": {
        "kind": "claude_cli", "needs_key": False, "default_model": "",
    },
    "Claude (API key)": {
        "kind": "anthropic", "needs_key": True, "default_model": "claude-sonnet-4-6",
        "env_keys": ["ANTHROPIC_API_KEY"],
        "key_url": "https://console.anthropic.com/settings/keys",
    },
    "OpenAI (GPT)": {
        "kind": "openai", "needs_key": True, "default_model": "gpt-4o",
        "endpoint": "https://api.openai.com/v1/chat/completions",
        "env_keys": ["OPENAI_API_KEY"],
        "key_url": "https://platform.openai.com/api-keys",
    },
    "Google (Gemini)": {
        "kind": "gemini", "needs_key": True, "default_model": "gemini-2.0-flash",
        "env_keys": ["GEMINI_API_KEY", "GOOGLE_API_KEY"],
        "key_url": "https://aistudio.google.com/app/apikey",
    },
    "xAI (Grok)": {
        "kind": "openai", "needs_key": True, "default_model": "grok-2-vision-1212",
        "endpoint": "https://api.x.ai/v1/chat/completions",
        "env_keys": ["XAI_API_KEY"],
        "key_url": "https://console.x.ai/",
    },
    "Llama (Ollama)": {
        "kind": "ollama", "needs_key": False, "default_model": "llama3.2-vision",
    },
}
DEFAULT_PROVIDER = "Claude (logged in)"


def find_claude():
    """Locate the Claude Code CLI executable."""
    exe = shutil.which("claude.exe") or shutil.which("claude")
    if exe and exe.lower().endswith(".exe"):
        return exe
    guess = os.path.join(
        os.environ.get("APPDATA", ""), "npm", "node_modules",
        "@anthropic-ai", "claude-code", "bin", "claude.exe",
    )
    return guess if os.path.exists(guess) else (exe or "claude")


def load_keys():
    """Return the saved {provider_name: api_key} map from config.json.

    Migrates the old single-key format ({"api_key": "..."}) into the new
    per-provider map so existing configs keep working.
    """
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {}
    keys = dict(data.get("keys", {}))
    legacy = data.get("api_key", "").strip()
    if legacy and "Claude (API key)" not in keys:
        keys["Claude (API key)"] = legacy
    return keys


def save_keys(keys):
    clean = {name: k.strip() for name, k in keys.items() if k and k.strip()}
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump({"keys": clean}, f)


def env_key_for(name):
    """Return an API key from the environment for the given provider, if set."""
    for env in PROVIDERS.get(name, {}).get("env_keys", []):
        v = os.environ.get(env, "").strip()
        if v:
            return v
    return ""


class RegionSelector(tk.Toplevel):
    """Fullscreen translucent overlay; drag to pick a rectangle on any monitor."""

    def __init__(self, master, on_done):
        super().__init__(master)
        self.on_done = on_done
        with mss.MSS() as sct:
            self.vmon = sct.monitors[0]  # bounding box of the whole virtual desktop
        self.geometry(
            f"{self.vmon['width']}x{self.vmon['height']}"
            f"+{self.vmon['left']}+{self.vmon['top']}"
        )
        self.overrideredirect(True)
        self.attributes("-alpha", 0.30)
        self.attributes("-topmost", True)
        self.configure(cursor="cross", bg="black")

        self.canvas = tk.Canvas(self, bg="black", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.create_text(
            self.vmon["width"] // 2, 30,
            text="Drag to select an area.  Esc to cancel.",
            fill="white", font=("Segoe UI", 16),
        )
        self.start = None
        self.rect = None
        self.canvas.bind("<ButtonPress-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._drag)
        self.canvas.bind("<ButtonRelease-1>", self._release)
        self.bind("<Escape>", lambda e: self.destroy())
        self.focus_force()

    def _press(self, e):
        self.start = (e.x, e.y)
        self.rect = self.canvas.create_rectangle(
            e.x, e.y, e.x, e.y, outline="#00e0ff", width=2
        )

    def _drag(self, e):
        if self.rect:
            self.canvas.coords(self.rect, self.start[0], self.start[1], e.x, e.y)

    def _release(self, e):
        if not self.start:
            self.destroy()
            return
        x0, y0 = self.start
        x1, y1 = e.x, e.y
        left = self.vmon["left"] + min(x0, x1)
        top = self.vmon["top"] + min(y0, y1)
        w, h = abs(x1 - x0), abs(y1 - y0)
        self.destroy()
        if w > 5 and h > 5:
            self.on_done({"left": left, "top": top, "width": w, "height": h})


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("AutoAnswer")
        self.geometry("600x700")
        self.region = None
        self.sct = mss.MSS()           # used only on the main thread
        self.photo = None              # keep a ref so Tk doesn't GC the image
        self.busy = False              # an API call is currently in flight
        self.live = False              # live-answer mode on/off
        self.last_sig = None           # signature of the last frame we sent to the AI
        self.cooldown_until = 0.0      # monotonic time until which live sends are paused
        self.saved_keys = load_keys()  # {provider_name: api_key} from config.json

        # --- AI provider row
        prov = tk.Frame(self)
        prov.pack(fill="x", padx=10, pady=8)
        tk.Label(prov, text="AI:").pack(side="left")
        self.provider_var = tk.StringVar(value=DEFAULT_PROVIDER)
        tk.OptionMenu(
            prov, self.provider_var, *PROVIDERS.keys(),
            command=self._provider_changed,
        ).pack(side="left", padx=6)
        tk.Label(prov, text="Model:").pack(side="left")
        self.model_var = tk.StringVar(value=PROVIDERS[DEFAULT_PROVIDER]["default_model"])
        self.model_entry = tk.Entry(prov, textvariable=self.model_var, width=20)
        self.model_entry.pack(side="left", padx=6)

        # --- API key row (per provider)
        top = tk.Frame(self)
        top.pack(fill="x", padx=10, pady=(0, 4))
        tk.Label(top, text="API key:").pack(side="left")
        self.key_var = tk.StringVar()
        self.key_entry = tk.Entry(top, textvariable=self.key_var, show="*")
        self.key_entry.pack(side="left", fill="x", expand=True, padx=6)
        self.save_key_btn = tk.Button(top, text="Save key", command=self._save_key)
        self.save_key_btn.pack(side="left")
        self.get_key_btn = tk.Button(top, text="Get key →", command=self._open_console)
        self.get_key_btn.pack(side="left", padx=(6, 0))

        # --- custom instructions for the AI (applied to every captured frame)
        instr = tk.Frame(self)
        instr.pack(fill="x", padx=10, pady=(4, 0))
        tk.Label(instr, text="Instructions for the AI:").pack(anchor="w")
        self.prompt_text = tk.Text(instr, height=3, wrap="word")
        self.prompt_text.pack(fill="x")
        self.prompt_text.insert("1.0", PROMPT)

        # --- controls
        ctrl = tk.Frame(self)
        ctrl.pack(fill="x", padx=10)
        tk.Button(ctrl, text="Select Region", command=self._select_region).pack(side="left")
        self.live_btn = tk.Button(
            ctrl, text="Live Answer: OFF  (F9)", width=18, command=self._toggle_live
        )
        self.live_btn.pack(side="left", padx=6)
        self.status = tk.Label(ctrl, text="No region selected", fg="gray")
        self.status.pack(side="left", padx=6)

        # --- live preview
        self.preview = tk.Label(self, bg="#111", text="Live preview", fg="#666")
        self.preview.pack(padx=10, pady=8)

        # --- answer box
        tk.Label(self, text="AI answer:").pack(anchor="w", padx=10)
        self.answer = scrolledtext.ScrolledText(self, height=12, wrap="word")
        self.answer.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        self.bind("<F9>", lambda e: self._toggle_live())
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._provider_changed(DEFAULT_PROVIDER)  # set key/model fields for the default
        self._update_preview()
        self._live_loop()

    # ---- provider selection
    def _provider_changed(self, name):
        """React to the AI dropdown: load that provider's key and default model,
        and enable/disable the key and model fields as appropriate."""
        cfg = PROVIDERS[name]
        self.model_var.set(cfg["default_model"])
        self.key_var.set(env_key_for(name) or self.saved_keys.get(name, ""))
        needs_key = cfg["needs_key"]
        key_state = "normal" if needs_key else "disabled"
        self.key_entry.config(state=key_state)
        self.save_key_btn.config(state=key_state)
        self.get_key_btn.config(state="normal" if cfg.get("key_url") else "disabled")
        # the logged-in Claude CLI picks its own model; everything else uses the field
        self.model_entry.config(state="disabled" if cfg["kind"] == "claude_cli" else "normal")

    # ---- region selection
    def _select_region(self):
        self.withdraw()  # hide main window so it isn't in the way
        self.after(150, lambda: RegionSelector(self, self._region_chosen))

    def _region_chosen(self, region):
        self.region = region
        self.deiconify()
        self.status.config(
            text=f"Region: {region['width']}x{region['height']}", fg="green"
        )

    def _grab_region_image(self):
        shot = self.sct.grab(self.region)
        return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")

    # ---- live preview loop (main thread)
    def _update_preview(self):
        if self.region:
            try:
                img = self._grab_region_image()
                scale = min(1.0, PREVIEW_MAX_W / img.width)
                if scale < 1.0:
                    img = img.resize(
                        (int(img.width * scale), int(img.height * scale))
                    )
                self.photo = ImageTk.PhotoImage(img)
                self.preview.config(image=self.photo, text="")
            except Exception:
                pass
        self.after(int(1000 / PREVIEW_FPS), self._update_preview)

    # ---- live answer mode
    def _toggle_live(self):
        if not self.live:
            if not self.region:
                messagebox.showinfo("AutoAnswer", "Select a region first.")
                return
            name = self.provider_var.get()
            if PROVIDERS[name]["needs_key"] and not self.key_var.get().strip():
                messagebox.showwarning("AutoAnswer", f"Enter an API key for {name} first.")
                return
            self.live = True
            self.last_sig = None  # force an answer on the first frame
            self.live_btn.config(text="Live Answer: ON  (F9)", bg="#2e7d32", fg="white")
        else:
            self.live = False
            self.live_btn.config(text="Live Answer: OFF  (F9)", bg="SystemButtonFace", fg="black")

    @staticmethod
    def _signature(img):
        """Tiny grayscale fingerprint of a frame, for cheap change detection."""
        return img.convert("L").resize((32, 32)).tobytes()

    @staticmethod
    def _diff(a, b):
        if a is None or b is None or len(a) != len(b):
            return 255
        return sum(abs(x - y) for x, y in zip(a, b)) / len(a)

    def _live_loop(self):
        # Watches the region while live mode is on; sends a frame to the AI only
        # when the content has changed, no request is already in flight, and we're
        # not in a post-error cooldown (which backs off after e.g. a rate limit).
        if self.live and self.region and not self.busy and time.monotonic() >= self.cooldown_until:
            try:
                img = self._grab_region_image()
                sig = self._signature(img)
                if self._diff(sig, self.last_sig) >= CHANGE_THRESHOLD:
                    self.last_sig = sig
                    self._send(img)
            except Exception:
                pass
        self.after(LIVE_CHECK_MS, self._live_loop)

    def _send(self, img):
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        b64 = base64.b64encode(buf.getvalue()).decode()
        prompt = self.prompt_text.get("1.0", "end").strip() or PROMPT
        name = self.provider_var.get()
        cfg = PROVIDERS[name]
        model = self.model_var.get().strip() or cfg["default_model"]
        key = self.key_var.get().strip()
        self.busy = True
        self._set_answer("Answering...")
        threading.Thread(
            target=self._worker, args=(cfg, model, key, b64, prompt), daemon=True
        ).start()

    def _worker(self, cfg, model, key, b64, prompt):
        """Runs off the main thread: calls the selected provider, returns text."""
        kind = cfg["kind"]
        is_error = False
        try:
            if kind == "anthropic":
                text = self._call_anthropic(key, model, b64, prompt)
            elif kind == "openai":
                text = self._call_openai(cfg["endpoint"], key, model, b64, prompt)
            elif kind == "gemini":
                text = self._call_gemini(key, model, b64, prompt)
            elif kind == "ollama":
                text = self._call_ollama(model, b64, prompt)
            elif kind == "claude_cli":
                text = self._call_claude_cli(b64, prompt)
            else:
                text = f"Error: unknown provider kind '{kind}'"
        except Exception as ex:
            text = f"Error: {self._friendly_error(ex)}"
            is_error = True
        self.after(0, lambda: self._finish(text, is_error))

    @staticmethod
    def _friendly_error(ex):
        """Turn a provider/HTTP exception into a clear, actionable message."""
        # urllib raises HTTPError for non-2xx; the Anthropic SDK exposes .status_code.
        code = getattr(ex, "code", None) or getattr(ex, "status_code", None)
        body = ""
        if isinstance(ex, urllib.error.HTTPError):
            try:
                body = ex.read().decode(errors="replace").strip()[:600]
            except Exception:
                pass
        if code == 429:
            retry = ""
            hdrs = getattr(ex, "headers", None)
            if hdrs and hdrs.get("Retry-After"):
                retry = f" Provider says retry after {hdrs.get('Retry-After')}s."
            msg = (
                "Rate limited (HTTP 429). The provider is throttling you or your "
                "account is out of quota/credit." + retry +
                f"\n\nLive mode will pause for {ERROR_COOLDOWN_S}s before trying again. "
                "Slow the changes, switch AI, or check your plan/billing."
            )
            return msg + (f"\n\n{body}" if body else "")
        if isinstance(ex, urllib.error.HTTPError):
            return f"HTTP {ex.code} {ex.reason}" + (f"\n\n{body}" if body else "")
        return str(ex)

    @staticmethod
    def _http_json(url, payload, headers, timeout=180):
        data = json.dumps(payload).encode()
        req = urllib.request.Request(url, data=data, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())

    def _call_anthropic(self, key, model, b64, prompt):
        client = anthropic.Anthropic(api_key=key)
        resp = client.messages.create(
            model=model,
            max_tokens=1024,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": b64,
                    }},
                    {"type": "text", "text": prompt},
                ],
            }],
        )
        return "".join(b.text for b in resp.content if b.type == "text")

    def _call_openai(self, endpoint, key, model, b64, prompt):
        # OpenAI Chat Completions format; also used by OpenAI-compatible APIs (xAI).
        data = self._http_json(endpoint, {
            "model": model,
            "max_tokens": 1024,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {
                        "url": f"data:image/png;base64,{b64}"
                    }},
                ],
            }],
        }, {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
        })
        return data["choices"][0]["message"]["content"].strip()

    def _call_gemini(self, key, model, b64, prompt):
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{model}:generateContent?key={key}"
        )
        data = self._http_json(url, {
            "contents": [{
                "parts": [
                    {"text": prompt},
                    {"inline_data": {"mime_type": "image/png", "data": b64}},
                ],
            }],
        }, {"Content-Type": "application/json"})
        parts = data["candidates"][0]["content"]["parts"]
        return "".join(p.get("text", "") for p in parts).strip()

    def _call_ollama(self, model, b64, prompt):
        try:
            data = self._http_json(OLLAMA_URL, {
                "model": model, "prompt": prompt, "images": [b64], "stream": False,
            }, {"Content-Type": "application/json"})
            text = data.get("response", "").strip()
            return text or "(empty response — is this a vision model? try: ollama pull llama3.2-vision)"
        except Exception as ex:
            return (
                f"Error: {ex}\n\nIs Ollama running and is '{model}' a vision model?\n"
                "Pull one with:  ollama pull llama3.2-vision"
            )

    def _call_claude_cli(self, b64, prompt):
        # Uses the logged-in Claude Code CLI (no API key): write the frame to a
        # temp PNG and let `claude -p` read it via its Read tool.
        path = None
        try:
            fd, path = tempfile.mkstemp(suffix=".png")
            with os.fdopen(fd, "wb") as f:
                f.write(base64.b64decode(b64))
            cli_prompt = f"Read the image file at {path}. {prompt}"
            proc = subprocess.run(
                [find_claude(), "-p", cli_prompt, "--allowedTools", "Read"],
                capture_output=True, text=True,
                stdin=subprocess.DEVNULL, timeout=180,
            )
            return (proc.stdout or "").strip() or (proc.stderr or "").strip() or "(no response)"
        except FileNotFoundError:
            return "Error: Claude Code CLI not found. Install it or use a different AI."
        finally:
            if path and os.path.exists(path):
                try:
                    os.remove(path)
                except OSError:
                    pass

    def _finish(self, text, is_error=False):
        self.busy = False
        if is_error:
            # Back off so live mode doesn't immediately re-fire and worsen a rate limit.
            self.cooldown_until = time.monotonic() + ERROR_COOLDOWN_S
        self._set_answer(text)

    def _set_answer(self, text):
        self.answer.delete("1.0", "end")
        self.answer.insert("1.0", text)

    def _save_key(self):
        name = self.provider_var.get()
        self.saved_keys[name] = self.key_var.get().strip()
        save_keys(self.saved_keys)
        self.status.config(text=f"API key saved for {name}", fg="green")

    def _open_console(self):
        # Open the page where you create/copy an API key for the current provider.
        url = PROVIDERS[self.provider_var.get()].get("key_url")
        if url:
            webbrowser.open(url)

    def _on_close(self):
        try:
            self.sct.close()
        except Exception:
            pass
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
