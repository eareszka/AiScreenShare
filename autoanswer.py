"""
AutoAnswer - live screen-region capture that asks Claude to answer what it sees.

Workflow:
  1. Click "Select Region" and drag a rectangle over the part of your screen
     you want to watch (a quiz question, a problem, anything).
  2. A live OBS-style preview of that region updates continuously.
  3. Toggle "Live Answer: ON" (or press F9). While on, the app watches the
     region and, whenever its contents change, sends that frame to Claude's
     vision API and shows the answer. Toggle it off to stop.

Needs an Anthropic API key: set the ANTHROPIC_API_KEY environment variable,
or paste it into the key field and click "Save key" (stored in config.json
next to this script).
"""

import base64
import io
import json
import os
import shutil
import subprocess
import tempfile
import threading
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

MODEL = "claude-sonnet-4-6"  # good vision + fast; swap to "claude-opus-4-8" for max quality
OLLAMA_URL = "http://localhost:11434/api/generate"   # local Llama (Ollama) endpoint
PREVIEW_MAX_W = 520          # max width of the live preview, in px
PREVIEW_FPS = 10
LIVE_CHECK_MS = 1500         # how often, in live mode, to look at the region for changes
CHANGE_THRESHOLD = 8         # mean per-pixel difference (0-255) that counts as "changed"
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

PROMPT = (
    "The image is a screenshot of something on the user's screen. Read it and "
    "directly answer the question or solve the problem it contains. If it is "
    "multiple choice, give the correct option and a one-line reason. Be concise."
)


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


def load_api_key():
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if key:
        return key
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f).get("api_key", "").strip()
    except Exception:
        return ""


def save_api_key(key):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump({"api_key": key.strip()}, f)


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
        self.last_sig = None           # signature of the last frame we sent to Claude

        # --- API key row
        top = tk.Frame(self)
        top.pack(fill="x", padx=10, pady=8)
        tk.Label(top, text="API key:").pack(side="left")
        self.key_var = tk.StringVar(value=load_api_key())
        self.key_entry = tk.Entry(top, textvariable=self.key_var, show="*")
        self.key_entry.pack(side="left", fill="x", expand=True, padx=6)
        tk.Button(top, text="Save key", command=self._save_key).pack(side="left")
        tk.Button(top, text="Get key →", command=self._open_console).pack(side="left", padx=(6, 0))

        # --- AI provider row
        prov = tk.Frame(self)
        prov.pack(fill="x", padx=10, pady=(0, 4))
        tk.Label(prov, text="AI:").pack(side="left")
        self.provider_var = tk.StringVar(value="Claude (logged in)")
        tk.OptionMenu(
            prov, self.provider_var,
            "Claude (logged in)", "Claude (API key)", "Llama (Ollama)",
        ).pack(side="left", padx=6)
        tk.Label(prov, text="Ollama model:").pack(side="left")
        self.ollama_var = tk.StringVar(value="llama3.2-vision")
        tk.Entry(prov, textvariable=self.ollama_var, width=18).pack(side="left", padx=6)

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
        tk.Label(self, text="Claude's answer:").pack(anchor="w", padx=10)
        self.answer = scrolledtext.ScrolledText(self, height=12, wrap="word")
        self.answer.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        self.bind("<F9>", lambda e: self._toggle_live())
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._update_preview()
        self._live_loop()

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
            if self.provider_var.get() == "Claude (API key)" and not self.key_var.get().strip():
                messagebox.showwarning("AutoAnswer", "Enter an Anthropic API key first.")
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
        # Watches the region while live mode is on; sends a frame to Claude only
        # when the content has changed and no request is already in flight.
        if self.live and self.region and not self.busy:
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
        self.busy = True
        self._set_answer("Answering...")
        provider = self.provider_var.get()
        if provider == "Claude (API key)":
            target, arg = self._ask_claude, self.key_var.get().strip()
        elif provider == "Claude (logged in)":
            target, arg = self._ask_claude_cli, None
        else:
            target, arg = self._ask_ollama, self.ollama_var.get().strip()
        threading.Thread(target=target, args=(arg, b64), daemon=True).start()

    def _ask_claude(self, key, b64):
        try:
            client = anthropic.Anthropic(api_key=key)
            resp = client.messages.create(
                model=MODEL,
                max_tokens=1024,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "image", "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": b64,
                        }},
                        {"type": "text", "text": PROMPT},
                    ],
                }],
            )
            text = "".join(b.text for b in resp.content if b.type == "text")
        except Exception as ex:
            text = f"Error: {ex}"
        self.after(0, lambda: self._finish(text))

    def _ask_ollama(self, model, b64):
        try:
            payload = json.dumps({
                "model": model,
                "prompt": PROMPT,
                "images": [b64],
                "stream": False,
            }).encode()
            req = urllib.request.Request(
                OLLAMA_URL, data=payload, headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=180) as r:
                text = json.loads(r.read()).get("response", "").strip()
            text = text or "(empty response — is this a vision model? try: ollama pull llama3.2-vision)"
        except Exception as ex:
            text = (
                f"Error: {ex}\n\nIs Ollama running and is '{model}' a vision model?\n"
                "Pull one with:  ollama pull llama3.2-vision"
            )
        self.after(0, lambda: self._finish(text))

    def _ask_claude_cli(self, _arg, b64):
        # Uses the logged-in Claude Code CLI (no API key): write the frame to a
        # temp PNG and let `claude -p` read it via its Read tool.
        path = None
        try:
            fd, path = tempfile.mkstemp(suffix=".png")
            with os.fdopen(fd, "wb") as f:
                f.write(base64.b64decode(b64))
            prompt = (
                f"Read the image file at {path} and directly answer the question "
                "or solve the problem it contains. If multiple choice, give the "
                "correct option and a one-line reason. Be concise."
            )
            proc = subprocess.run(
                [find_claude(), "-p", prompt, "--allowedTools", "Read"],
                capture_output=True, text=True,
                stdin=subprocess.DEVNULL, timeout=180,
            )
            text = (proc.stdout or "").strip() or (proc.stderr or "").strip() or "(no response)"
        except FileNotFoundError:
            text = "Error: Claude Code CLI not found. Install it or use a different AI."
        except Exception as ex:
            text = f"Error: {ex}"
        finally:
            if path and os.path.exists(path):
                try:
                    os.remove(path)
                except OSError:
                    pass
        self.after(0, lambda: self._finish(text))

    def _finish(self, text):
        self.busy = False
        self._set_answer(text)

    def _set_answer(self, text):
        self.answer.delete("1.0", "end")
        self.answer.insert("1.0", text)

    def _save_key(self):
        save_api_key(self.key_var.get())
        self.status.config(text="API key saved", fg="green")

    def _open_console(self):
        # Anthropic's API authenticates with a key (no account login for apps).
        # This just opens the page where you create/copy one; paste it once + Save.
        webbrowser.open("https://console.anthropic.com/settings/keys")

    def _on_close(self):
        try:
            self.sct.close()
        except Exception:
            pass
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
