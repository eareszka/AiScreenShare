# AutoAnswer

Live screen-region capture that asks Claude to answer what it sees.

Select a rectangle anywhere on your screen, watch a live OBS-style preview of it,
then send the current frame to Claude's vision API and get an answer back.

## Setup

```powershell
py -m pip install -r requirements.txt
```

Provide an Anthropic API key one of two ways:

- Set an environment variable: `setx ANTHROPIC_API_KEY "sk-ant-..."` (reopen the terminal), or
- Run the app, paste the key into the **API key** field, and click **Save key**
  (stored in `config.json`, which is gitignored).

## Run

```powershell
py autoanswer.py
```

1. **Select Region** – drag a rectangle over the area to watch (Esc cancels).
2. The live preview updates continuously.
3. **Live Answer: ON/OFF** (or press **F9**) – while ON, the app watches the region
   and sends a frame to Claude **only when the contents change**, then shows the
   answer. Toggle OFF to stop.

## Choosing the AI

The **AI** dropdown picks the backend:

- **Claude (logged in)** – routes through the Claude Code CLI using your existing
  Claude Code login. **No API key needed.** Requires Claude Code installed and signed
  in. Slower per answer (it spins up an agent each call).
- **Claude (API key)** – Anthropic's cloud vision API. Needs an API key (above).
  Fastest, best quality.
- **Llama (Ollama)** – a local vision model, free and private. Requires a *vision*
  model pulled into Ollama (text-only models like `llama3.1` can't read the screen):

  ```powershell
  ollama pull llama3.2-vision    # or: ollama pull llava
  ```

  Set the model name in the **Ollama model** field. No API key needed for this path.

## Notes

- Claude model is set by `MODEL` in `autoanswer.py` (default `claude-sonnet-4-6`; change
  to `claude-opus-4-8` for maximum quality).
- Live mode calls the API once per detected change — each call costs money on your
  Anthropic account. Tune `LIVE_CHECK_MS` (how often it looks) and `CHANGE_THRESHOLD`
  (how much change triggers a new answer) at the top of `autoanswer.py`.
- Works across multiple monitors; region coordinates are physical pixels.
```
