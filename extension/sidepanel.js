// SnipAI side panel: capture the visible tab, crop a user-selected region, and
// send that crop to the chosen AI. The user decides when: "Analyze now" answers
// once on demand, while "Live" answers automatically whenever the region changes.
// PROVIDERS / askAI live in providers.js (loaded first).

const PROMPT =
  "The image is a screenshot of something on the user's screen. Read it and " +
  "directly answer the question or solve the problem it contains. If it is " +
  "multiple choice, give the correct option and a one-line reason. Be concise.";

// Appended to every prompt to control answer formatting. The panel renders LaTeX
// math (via MathJax) but shows everything else as plain text, so: use LaTeX only
// for actual math, and never use Markdown headings/bold (those don't render here).
const FORMAT =
  " Formatting: if the screenshot is a math problem, write every mathematical " +
  "expression in LaTeX, wrapped in $ ... $ for inline math and $$ ... $$ for " +
  "displayed equations (e.g. $$\\frac{d}{dx}(e^x\\cos x) = e^x(\\cos x - \\sin x)$$). " +
  "If it is not math, reply in plain text with no LaTeX. Either way, do NOT use " +
  "Markdown headings (#) or bold (**) — write any words as plain prose.";

const LOOP_MS = 1200; // how often we capture/check (also bounds captureVisibleTab calls)
const CHANGE_THRESHOLD = 8; // mean per-pixel grayscale diff (0-255) that counts as "changed"
const ERROR_COOLDOWN_MS = 20000; // after an error (e.g. 429), pause live sends this long
const INSTRUCTIONS_IDLE_MS = 900; // pause after typing instructions before we re-answer

// ---- DOM
const $ = (id) => document.getElementById(id);
const els = {
  provider: $("provider"),
  model: $("model"),
  key: $("key"),
  saveKey: $("saveKey"),
  getKey: $("getKey"),
  instructions: $("instructions"),
  selectRegion: $("selectRegion"),
  analyze: $("analyze"),
  live: $("live"),
  status: $("status"),
  selectWrap: $("selectWrap"),
  selectCanvas: $("selectCanvas"),
  previewWrap: $("previewWrap"),
  preview: $("preview"),
  previewPlaceholder: $("previewPlaceholder"),
  answer: $("answer"),
};

// ---- state
let region = null; // {x, y, w, h} in captured-image (natural) pixels
let live = false;
let busy = false;
let selecting = false;
let lastSig = null;
let cooldownUntil = 0;
let instrTimer = null; // debounce timer for "done typing instructions"
let lastRegenInstr = null; // instructions text we last auto-answered, to skip no-op repeats
let pendingRegen = false; // a regenerate was requested while busy; run it when free
let store = { keys: {}, models: {}, provider: null, instructions: PROMPT };

// ---- helpers
const now = () => Date.now();

function setStatus(text, cls = "") {
  els.status.textContent = text;
  els.status.className = "status" + (cls ? " " + cls : "");
}

// Matches LaTeX math delimiters: $...$, $$...$$, \(...\), \[...\]
const MATH_RE = /\$|\\\(|\\\[/;

function setAnswer(text) {
  const clean = stripMarkdown(text);
  // Plain text first: it's the safe fallback and what non-math answers should show.
  els.answer.textContent = clean;
  // If there's LaTeX and MathJax is ready, typeset it in place (math -> SVG).
  if (clean && MATH_RE.test(clean) && window.MathJax && MathJax.typesetPromise) {
    try {
      MathJax.typesetClear?.([els.answer]);
      MathJax.typesetPromise([els.answer]).catch(() => {
        /* malformed LaTeX — leave the plain text as-is */
      });
    } catch (_) {
      /* renderer not ready — plain text stays */
    }
  }
}

// Strip Markdown the panel can't render (headings, bold, inline-code ticks) while
// leaving LaTeX math untouched so MathJax can typeset it.
function stripMarkdown(text) {
  if (!text) return text;
  return text
    .replace(/^#{1,6}\s*/gm, "")
    .replace(/\*\*/g, "")
    .replace(/`/g, "")
    .trim();
}

function captureTab() {
  // Screenshot of the visible area of the active tab in the current window.
  return chrome.tabs.captureVisibleTab({ format: "png" });
}

// Capture the tab and return a canvas cropped to the current region.
async function captureCrop() {
  const dataUrl = await captureTab();
  const img = await loadImage(dataUrl);
  return cropCanvas(img, region);
}

function loadImage(dataUrl) {
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.onload = () => resolve(img);
    img.onerror = () => reject(new Error("could not decode capture"));
    img.src = dataUrl;
  });
}

function cropCanvas(img, r) {
  const x = Math.max(0, Math.min(r.x, img.naturalWidth - 1));
  const y = Math.max(0, Math.min(r.y, img.naturalHeight - 1));
  const w = Math.max(1, Math.min(r.w, img.naturalWidth - x));
  const h = Math.max(1, Math.min(r.h, img.naturalHeight - y));
  const c = document.createElement("canvas");
  c.width = w;
  c.height = h;
  c.getContext("2d").drawImage(img, x, y, w, h, 0, 0, w, h);
  return c;
}

// Tiny 32x32 grayscale fingerprint for cheap change detection.
function signature(canvas) {
  const c = document.createElement("canvas");
  c.width = 32;
  c.height = 32;
  const cx = c.getContext("2d");
  cx.drawImage(canvas, 0, 0, 32, 32);
  const d = cx.getImageData(0, 0, 32, 32).data;
  const g = new Uint8Array(32 * 32);
  for (let i = 0; i < g.length; i++) {
    const p = i * 4;
    g[i] = (d[p] * 0.299 + d[p + 1] * 0.587 + d[p + 2] * 0.114) | 0;
  }
  return g;
}

function diff(a, b) {
  if (!a || !b || a.length !== b.length) return 255;
  let s = 0;
  for (let i = 0; i < a.length; i++) s += Math.abs(a[i] - b[i]);
  return s / a.length;
}

function showPreview(canvas) {
  els.preview.src = canvas.toDataURL("image/png");
  els.preview.style.display = "block";
  els.previewPlaceholder.style.display = "none";
}

// ---- storage
async function loadStore() {
  const st = await chrome.storage.local.get(["keys", "models", "provider", "instructions"]);
  store.keys = st.keys || {};
  store.models = st.models || {};
  store.provider = st.provider || DEFAULT_PROVIDER;
  store.instructions = typeof st.instructions === "string" ? st.instructions : PROMPT;
}

function saveStore() {
  chrome.storage.local.set({
    keys: store.keys,
    models: store.models,
    provider: store.provider,
    instructions: store.instructions,
  });
}

// ---- provider UI
function currentProvider() {
  return els.provider.value;
}

function buildProviderMenu() {
  for (const name of Object.keys(PROVIDERS)) {
    const opt = document.createElement("option");
    opt.value = name;
    opt.textContent = name;
    els.provider.appendChild(opt);
  }
  els.provider.value = store.provider;
}

function onProviderChanged() {
  const name = currentProvider();
  store.provider = name;
  els.model.value = store.models[name] || PROVIDERS[name].defaultModel;
  els.key.value = store.keys[name] || "";
  els.getKey.disabled = !PROVIDERS[name].keyUrl;
  saveStore();
}

// ---- region selection (drag a box on a screenshot shown in the panel)
let dragStart = null;
let selectImg = null;

function canvasPoint(e) {
  const rect = els.selectCanvas.getBoundingClientRect();
  return {
    x: (e.clientX - rect.left) * (els.selectCanvas.width / rect.width),
    y: (e.clientY - rect.top) * (els.selectCanvas.height / rect.height),
  };
}

function redrawSelect(box) {
  const cx = els.selectCanvas.getContext("2d");
  cx.drawImage(selectImg, 0, 0);
  if (box) {
    cx.strokeStyle = "#00e0ff";
    cx.lineWidth = Math.max(2, els.selectCanvas.width / 300);
    cx.strokeRect(box.x, box.y, box.w, box.h);
  }
}

async function startSelect() {
  if (selecting) return cancelSelect();
  let dataUrl;
  try {
    dataUrl = await captureTab();
  } catch (e) {
    setStatus("Can't capture this page (try a normal website tab).", "err");
    return;
  }
  selectImg = await loadImage(dataUrl);
  els.selectCanvas.width = selectImg.naturalWidth;
  els.selectCanvas.height = selectImg.naturalHeight;
  redrawSelect(null);
  els.selectWrap.classList.remove("hidden");
  selecting = true;
  setStatus("Drag a box over the area to watch.");
}

function cancelSelect() {
  selecting = false;
  dragStart = null;
  els.selectWrap.classList.add("hidden");
}

els.selectCanvas.addEventListener("mousedown", (e) => {
  dragStart = canvasPoint(e);
});
els.selectCanvas.addEventListener("mousemove", (e) => {
  if (!dragStart) return;
  const p = canvasPoint(e);
  redrawSelect({ x: dragStart.x, y: dragStart.y, w: p.x - dragStart.x, h: p.y - dragStart.y });
});
els.selectCanvas.addEventListener("mouseup", (e) => {
  if (!dragStart) return;
  const p = canvasPoint(e);
  const x = Math.round(Math.min(dragStart.x, p.x));
  const y = Math.round(Math.min(dragStart.y, p.y));
  const w = Math.round(Math.abs(p.x - dragStart.x));
  const h = Math.round(Math.abs(p.y - dragStart.y));
  dragStart = null;
  if (w > 5 && h > 5) {
    region = { x, y, w, h };
    lastSig = null; // force an answer on the next live frame
    setStatus(`Region: ${w}×${h}`, "ok");
    cancelSelect();
  } else {
    setStatus("Box too small — drag a larger area.", "err");
  }
});

// cancel selection by clicking outside the canvas or pressing Esc
document.addEventListener("mousedown", (e) => {
  if (selecting && !els.selectWrap.contains(e.target) && e.target !== els.selectRegion) {
    cancelSelect();
    setStatus("Selection canceled.");
  }
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && selecting) {
    cancelSelect();
    setStatus("Selection canceled.");
  }
});

// ---- live mode
function setLive(on) {
  live = on;
  els.live.textContent = on ? "Live: ON" : "Live: OFF";
  els.live.classList.toggle("live-on", on);
  els.live.classList.toggle("live-off", !on);
}

function toggleLive() {
  if (!live) {
    if (!region) {
      setStatus("Select a region first.", "err");
      return;
    }
    if (!els.key.value.trim()) {
      setStatus(`Enter an API key for ${currentProvider()} first.`, "err");
      return;
    }
    lastSig = null;
    setLive(true);
    setStatus("Live: watching for changes…", "ok");
  } else {
    setLive(false);
    setStatus("Live: off.");
  }
}

async function send(crop) {
  busy = true;
  setAnswer("Answering…");
  const name = currentProvider();
  const model = els.model.value.trim() || PROVIDERS[name].defaultModel;
  const key = els.key.value.trim();
  const prompt = (els.instructions.value.trim() || PROMPT) + FORMAT;
  const b64 = crop.toDataURL("image/png").split(",")[1];
  try {
    const text = await askAI(name, b64, model, key, prompt);
    setAnswer(text || "(empty response)");
  } catch (err) {
    setAnswer("Error: " + (err.message || err));
    cooldownUntil = now() + ERROR_COOLDOWN_MS; // back off after any failure
  } finally {
    busy = false;
    if (pendingRegen) {
      // instructions changed while we were answering — honor the latest text now
      pendingRegen = false;
      regenerateForInstructions();
    }
  }
}

// Answer the current region once, on demand. This is the manual counterpart to
// Live mode: the user clicks "Analyze now" to choose exactly when the AI looks.
async function analyzeNow() {
  if (!region) {
    setStatus("Select a region first.", "err");
    return;
  }
  if (!els.key.value.trim()) {
    setStatus(`Enter an API key for ${currentProvider()} first.`, "err");
    return;
  }
  if (busy) {
    setStatus("Already answering — hold on…");
    return;
  }
  let crop;
  try {
    crop = await captureCrop();
  } catch (e) {
    setStatus("Capture failed (restricted page?).", "err");
    return;
  }
  showPreview(crop);
  lastSig = signature(crop); // keep Live mode from immediately re-sending the same frame
  setStatus("Analyzing…", "ok");
  send(crop);
}

// Capture the current region and answer with the latest instructions. Used when
// the user finishes editing the Instructions box, independent of Live mode.
async function regenerateForInstructions() {
  if (!region) {
    setStatus("Select a region first to answer new instructions.");
    return;
  }
  if (!els.key.value.trim()) {
    setStatus(`Enter an API key for ${currentProvider()} first.`, "err");
    return;
  }
  if (busy) {
    pendingRegen = true; // a request is in flight; rerun with the new text after it
    return;
  }
  let crop;
  try {
    crop = await captureCrop();
  } catch (e) {
    setStatus("Capture failed (restricted page?).", "err");
    return;
  }
  lastSig = signature(crop); // keep Live mode from immediately re-sending the same frame
  setStatus("New instructions — answering…", "ok");
  send(crop);
}

// ---- main loop: capture, update preview, and (if live) detect change + send
async function tick() {
  if (region && !selecting) {
    try {
      const crop = await captureCrop();
      showPreview(crop);
      if (live && !busy && now() >= cooldownUntil) {
        const sig = signature(crop);
        if (diff(sig, lastSig) >= CHANGE_THRESHOLD) {
          lastSig = sig;
          send(crop); // fire and forget; busy guards re-entry
        }
      }
    } catch (e) {
      if (live) setStatus("Capture failed (restricted page?).", "err");
    }
  }
  setTimeout(tick, LOOP_MS);
}

// ---- wire up + init
els.provider.addEventListener("change", onProviderChanged);
els.model.addEventListener("change", () => {
  store.models[currentProvider()] = els.model.value.trim();
  saveStore();
});
els.instructions.addEventListener("input", () => {
  store.instructions = els.instructions.value;
  // Debounce: wait until typing pauses, then save and (if the text actually
  // changed) generate a fresh answer for the current region.
  if (instrTimer) clearTimeout(instrTimer);
  instrTimer = setTimeout(() => {
    saveStore();
    const text = els.instructions.value.trim();
    if (text && text !== lastRegenInstr) {
      lastRegenInstr = text;
      regenerateForInstructions();
    }
  }, INSTRUCTIONS_IDLE_MS);
});
els.saveKey.addEventListener("click", () => {
  const name = currentProvider();
  store.keys[name] = els.key.value.trim();
  saveStore();
  setStatus(`API key saved for ${name}.`, "ok");
});
els.getKey.addEventListener("click", () => {
  const url = PROVIDERS[currentProvider()].keyUrl;
  if (url) chrome.tabs.create({ url });
});
els.selectRegion.addEventListener("click", startSelect);
els.analyze.addEventListener("click", analyzeNow);
els.live.addEventListener("click", toggleLive);

(async function init() {
  await loadStore();
  buildProviderMenu();
  els.instructions.value = store.instructions;
  lastRegenInstr = store.instructions.trim(); // don't auto-answer the restored text
  onProviderChanged();
  setLive(false);
  tick();
})();
