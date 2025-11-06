# Gemini LiveKit Voice & Video Agent

This project deploys a LiveKit worker that runs a Gemini Realtime multi‑modal agent.  
It listens for room jobs, subscribes to the participant’s audio/video feeds, and replies in real time with speech synthesized responses.

The agent automatically joins whenever a participant connects, stays resident when the room empties, and immediately resumes when the user returns. Video frames are always consumed when available, while the user can still ask the agent to pause or resume video processing.

---

## Prerequisites

1. **Environment variables**  
   Create a `.env` file and provide:
   - `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET`, `VOICE_AGENT_ROOM`
   - `GEMINI_API_KEY` with access to Gemini Realtime
   - Optional overrides such as  `GEMINI_MODEL`, `GEMINI_TTS_VOICE`, `GEMINI_TEMPERATURE`, `VOICE_AGENT_NAME`
   - To let Gemini use Google Search, set `GEMINI_ENABLE_SEARCH=true` (or override per job metadata with `enable_search`)

2. **Python environment**
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install --upgrade pip
   pip install -r requirements.txt
   python -m pip install "livekit-agents[images]"   # для відео-енкодингу
   ```

3. **Prompt management**  
   - Edit `prompt.md` to adjust Hanna’s base persona in Ukrainian.  
   - To override the prompt dynamically, set `VOICE_AGENT_PROMPT_FILE=/path/to/custom.md` or provide `VOICE_AGENT_INSTRUCTIONS` directly in env.

4. **Autostart (optional)**  
   If you want to run the worker without passing CLI arguments, add `VOICE_AGENT_ROOM=<room-name>` to your `.env`.  
   When set, `python main.py` behaves like `python main.py connect --room <room-name>` using the credentials from `.env`.

---

## Running the Worker

```bash
source .venv/bin/activate
python main.py
```

Працюй з кореня репозиторію, де розміщені `prompt.md` та `.env`. Якщо `.venv` ще не створено, виконай кроки з розділу «Python environment» перед запуском.

The worker:
- waits for LiveKit jobs, connects to the room, and wires audio/video to the Gemini Realtime model;
- refuses to become the first host unless `VOICE_AGENT_WAIT_FOR_OCCUPANT=false`.
- automatically terminates a few seconds after the last remote participant leaves (tune via `VOICE_AGENT_ROOM_EMPTY_SHUTDOWN_DELAY`, disable with `VOICE_AGENT_TERMINATE_ON_EMPTY=false`). Якщо `VOICE_AGENT_CLOSE_ROOM_ON_EMPTY=true` (за замовчуванням), кімната закривається через LiveKit API.

For development or production deployments using `start`, `dev`, or LiveKit Cloud agent dispatch, ensure the same environment variables are supplied.

---

## Video Behaviour

- **Immediate video consumption**: when a participant’s camera is active, the agent streams video to Gemini without additional prompts.
- **User controls**: the agent exposes tool calls `disable_video_feed` and `enable_video_feed`. A user instruction like “turn off video” pauses frame streaming; “turn video back on” resumes it.
- **Adaptive frame rate**: video sampling employs `VoiceActivityVideoSampler`. Tune the cadence via:
  - `VOICE_AGENT_VIDEO_FPS_SPEAKING` (default `1.0` fps)
  - `VOICE_AGENT_VIDEO_FPS_SILENT` (default `0.3` fps)
- **Automatic greeting**: whenever a participant joins, Hanna immediately introduces herself and offers assistance without mentioning personal limitations.

Because video encoding uses Pillow, failure to install `livekit-agents[images]` (or at least `Pillow>=10`) will trigger runtime errors.

---

## Continuous Availability

- `RoomInputOptions(close_on_disconnect=False)` keeps the media pipeline alive while participants are present.
- Custom participant event hooks reconnect RoomIO to the next arriving participant, restoring audio/video automatically.
- `user_away_timeout=None` disables idle timeouts inside `AgentSession`, so the agent won’t shut down mid-conversation.
- Automatic shutdown on an empty room prevents idle resource usage; set `VOICE_AGENT_TERMINATE_ON_EMPTY=false` (or metadata `terminate_on_empty: false`) if you prefer the previous “always on” behaviour. Для повного очищення контексту можна окремо вимкнути `VOICE_AGENT_CLOSE_ROOM_ON_EMPTY`.

---

## Additional Configuration

- `VOICE_AGENT_INSTRUCTIONS` – customise the system prompt (language, tone, persona).
- By default the assistant introduces herself as **Hanna**, a polite Ukrainian-speaking helper who offers practical guidance. She never mentions physical abilities unprompted but will answer health-related questions delicately if the user explicitly asks.
- `GEMINI_MODEL`, `GEMINI_TTS_VOICE`, `GEMINI_TEMPERATURE` – override model, voice, and creativity.
- `GEMINI_ENABLE_SEARCH` – enable the experimental Gemini Google Search tool. Supported overrides: job metadata can pass `enable_search: true` to toggle it per room/session.
- `browse_web_page` – тул headless-браузера на базі Playwright. Використовує Chromium у режимі без вікна, повертає текст сторінки. Налаштування: `VOICE_AGENT_BROWSER_HOME`, `VOICE_AGENT_BROWSER_TIMEOUT_MS`, `VOICE_AGENT_BROWSER_MAX_CHARS`, `VOICE_AGENT_BROWSER_USER_AGENT`, `VOICE_AGENT_BROWSER_LOCALE`, `VOICE_AGENT_BROWSER_TIMEZONE`, `VOICE_AGENT_BROWSER_WAIT_UNTIL`, `VOICE_AGENT_BROWSER_CHROMIUM_ARGS`, `VOICE_AGENT_BROWSER_VIEWPORT_WIDTH`, `VOICE_AGENT_BROWSER_VIEWPORT_HEIGHT`, `VOICE_AGENT_BROWSER_EXTRA_WAIT_MS` (стандартно 2000 мс), `VOICE_AGENT_BROWSER_IDLE_SECONDS`.
- `current_time_utc_plus3` – тул для оголошення поточного часу у Києві/UTC+3. За потреби можна змінити часовий пояс `VOICE_AGENT_TIMEZONE` або задати зсув `VOICE_AGENT_TIME_OFFSET_HOURS`.
- `fetch_rss_news` – вбудований тул агента для читання RSS-стрічок. Укажіть повний URL (та опційно `limit`) у запиті, і Ганна перерахує останні публікації. Якщо URL не задано, можна задати `VOICE_AGENT_RSS_FEED` (та `VOICE_AGENT_RSS_LIMIT`) у середовищі; `VOICE_AGENT_RSS_ALLOW_OVERRIDE=true` дозволяє користувачу змінювати URL і ліміт у запиті, а `VOICE_AGENT_RSS_USER_AGENT` задає HTTP User-Agent.
- `VOICE_AGENT_TERMINATE_ON_EMPTY` – завершує воркер, коли в кімнаті нікого не лишилося (true за замовчуванням). `VOICE_AGENT_CLOSE_ROOM_ON_EMPTY` – одразу викликає `DeleteRoom` у LiveKit після виходу всіх. `VOICE_AGENT_ROOM_EMPTY_SHUTDOWN_DELAY` – затримка перед завершенням (секунди). `VOICE_AGENT_GREETING_DELAY` – затримка перед автоматичним привітанням (секунди, стандартно 0.5).
- `VOICE_AGENT_WAIT_FOR_OCCUPANT`, `VOICE_AGENT_POLL_SECONDS`, `VOICE_AGENT_WAIT_TIMEOUT` – control the pre-join guard that prevents the agent from being the first participant.
- `VOICE_AGENT_MIN_INTERRUPTION_DURATION`, `VOICE_AGENT_MIN_INTERRUPTION_WORDS`, `VOICE_AGENT_MIN_ENDPOINTING_DELAY` – тонке налаштування поведінки “barge-in”, коли користувач перебиває поточну відповідь. За замовчуванням агент реагує після ~0.2 секунди нового мовлення.

Refer to the official documentation for advanced deployment options:
- [LiveKit Agents](https://docs.livekit.io/agents/start/voice-ai/)
- [Live Video & Vision](https://docs.livekit.io/agents/build/vision/)
- [LiveKit Cloud/Dispatch setup](https://docs.livekit.io/cloud/overview/)

---

## Quick Checklist

1. Activate virtualenv → `source .venv/bin/activate`
2. Install dependencies → `pip install -r requirements.txt`
3. (For the browser tool) Install the renderer → `python -m playwright install chromium`
4. (Linux) Install the system packages required by headless Chromium (GTK, NSS, ALSA, libdrm, etc.). If anything is missing, `playwright install` will list the exact packages.
5. Populate `.env` with LiveKit/Gemini credentials, prompt overrides (if any), and optional video settings
6. Launch the worker → `python main.py`
7. Join the target room from the client; the agent should greet you immediately, consuming audio and video in real time
