// AI provider table + the call paths. Each call takes a base64 PNG (no data:
// prefix), the model id, the API key, and the prompt, and returns answer text.
// Throws an Error with a friendly .message (and .status for HTTP errors) on
// failure so the UI can show it and trigger a cooldown.

const PROVIDERS = {
  "Claude (API key)": {
    kind: "anthropic",
    defaultModel: "claude-sonnet-4-6",
    keyUrl: "https://console.anthropic.com/settings/keys",
  },
  "OpenAI (GPT)": {
    kind: "openai",
    defaultModel: "gpt-4o",
    endpoint: "https://api.openai.com/v1/chat/completions",
    keyUrl: "https://platform.openai.com/api-keys",
  },
  "Google (Gemini)": {
    kind: "gemini",
    defaultModel: "gemini-2.0-flash",
    keyUrl: "https://aistudio.google.com/app/apikey",
  },
};

const DEFAULT_PROVIDER = "Claude (API key)";

// Read an error body and turn an HTTP failure into a clear message. 429 gets a
// special note since live mode backs off on it.
async function httpError(resp) {
  let body = "";
  try {
    body = (await resp.text()).slice(0, 600).trim();
  } catch (_) {
    /* ignore */
  }
  const err = new Error();
  err.status = resp.status;
  if (resp.status === 429) {
    err.message =
      "Rate limited (HTTP 429). The provider is throttling you or your " +
      "account is out of quota/credit. Live mode will pause briefly, then " +
      "retry. Slow the changes, switch AI, or check your plan/billing." +
      (body ? "\n\n" + body : "");
  } else {
    err.message = `HTTP ${resp.status} ${resp.statusText}` + (body ? "\n\n" + body : "");
  }
  return err;
}

async function callAnthropic(b64, model, key, prompt) {
  const resp = await fetch("https://api.anthropic.com/v1/messages", {
    method: "POST",
    headers: {
      "content-type": "application/json",
      "x-api-key": key,
      "anthropic-version": "2023-06-01",
      // lets the request run from a browser context
      "anthropic-dangerous-direct-browser-access": "true",
    },
    body: JSON.stringify({
      model,
      max_tokens: 1024,
      messages: [
        {
          role: "user",
          content: [
            {
              type: "image",
              source: { type: "base64", media_type: "image/png", data: b64 },
            },
            { type: "text", text: prompt },
          ],
        },
      ],
    }),
  });
  if (!resp.ok) throw await httpError(resp);
  const data = await resp.json();
  return (data.content || [])
    .filter((b) => b.type === "text")
    .map((b) => b.text)
    .join("")
    .trim();
}

async function callOpenAI(endpoint, b64, model, key, prompt) {
  const resp = await fetch(endpoint, {
    method: "POST",
    headers: {
      "content-type": "application/json",
      Authorization: `Bearer ${key}`,
    },
    body: JSON.stringify({
      model,
      max_tokens: 1024,
      messages: [
        {
          role: "user",
          content: [
            { type: "text", text: prompt },
            { type: "image_url", image_url: { url: `data:image/png;base64,${b64}` } },
          ],
        },
      ],
    }),
  });
  if (!resp.ok) throw await httpError(resp);
  const data = await resp.json();
  return (data.choices?.[0]?.message?.content || "").trim();
}

async function callGemini(b64, model, key, prompt) {
  const url =
    `https://generativelanguage.googleapis.com/v1beta/models/${model}:generateContent?key=${key}`;
  const resp = await fetch(url, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      contents: [
        {
          parts: [
            { text: prompt },
            { inline_data: { mime_type: "image/png", data: b64 } },
          ],
        },
      ],
    }),
  });
  if (!resp.ok) throw await httpError(resp);
  const data = await resp.json();
  const parts = data.candidates?.[0]?.content?.parts || [];
  return parts.map((p) => p.text || "").join("").trim();
}

// Dispatch to the right path for the chosen provider.
async function askAI(name, b64, model, key, prompt) {
  const cfg = PROVIDERS[name];
  switch (cfg.kind) {
    case "anthropic":
      return callAnthropic(b64, model, key, prompt);
    case "openai":
      return callOpenAI(cfg.endpoint, b64, model, key, prompt);
    case "gemini":
      return callGemini(b64, model, key, prompt);
    default:
      throw new Error(`Unknown provider kind '${cfg.kind}'`);
  }
}
