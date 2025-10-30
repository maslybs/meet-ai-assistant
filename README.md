# Gemini LiveKit Voice Agent

Цей проєкт розгортає LiveKit voice-agent, який використовує Google Gemini RealtimeModel для повного циклу real-time спілкування (розпізнавання, генерація, синтез). Код побудований на [LiveKit Agents](https://docs.livekit.io/agents/start/voice-ai/) і запускає `AgentSession` з RealtimeModel (`python main.py`).

## Підготовка

1. Створіть `.env`, базуючись на `.env.example`, і заповніть обовʼязкові поля:
   - `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET`
   - `GEMINI_API_KEY` (ключ із доступом до Gemini Realtime, модель за замовчуванням `gemini-2.5-flash-native-audio-preview-09-2025`)
   - За потреби, змініть інші налаштування голосу та моделі.

2. Встановіть залежності. У контейнері або локально виконайте:

   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install --upgrade pip
   pip install -r requirements.txt
   ```

   Якщо хочете запускати агента без передачі аргументів CLI,
   додайте в `.env` змінну `VOICE_AGENT_ROOM=<ваша_кімната>`.
   Так скрипт автоматично викличе `python main.py connect --room ...`
   з параметрами `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET`
   з вашого `.env`.

   За замовчуванням агент зачекає, поки в кімнаті зʼявиться хоч один
   учасник, щоб не стати першим хостом. Поведінку можна налаштувати:
   - `VOICE_AGENT_WAIT_FOR_OCCUPANT=false` — вимкнути очікування
   - `VOICE_AGENT_POLL_SECONDS=2` — інтервал між перевірками
   - `VOICE_AGENT_WAIT_TIMEOUT=120` — максимальний час очікування (0 — без меж)

## Запуск агента

Запустіть worker:

```bash
python main.py
```

LiveKit worker підʼєднається до вашого LiveKit-сервера, очікуватиме Job і опрацьовуватиме його за допомогою Gemini RealtimeModel. Worker автоматично підписується на аудіо доріжки, в режимі реального часу передає звук до моделі й повертає синтезовані відповіді.

## Налаштування поведінки

- `VOICE_AGENT_INSTRUCTIONS` — системні інструкції. Можна задати мову, стиль відповіді, тон.
- `GEMINI_MODEL`, `GEMINI_TTS_VOICE`, `GEMINI_TEMPERATURE` — підібрані стандартно, але їх можна змінювати під потреби (інший голос, креативність моделі).

Якщо ви використовуєте LiveKit Cloud або scheduler, розгорніть worker відповідно до офіційної документації, передавши ці ж environment variables.
