BVoice v1.2.0 — Voice Input (macOS) — Fixed Build
===================================================

Голосовой ввод через OpenAI Whisper API.
Двойной тап Fn = старт записи, одиночный тап Fn = стоп + расшифровка + автовставка.

ЗАПУСК:
  Open ~/Applications/BVoice.app  (или Spotlight → "BVoice")

АВТОЗАПУСК:
  System Settings → General → Login Items → Open at Login → +
  Добавить ~/Applications/BVoice.app

REQUIREMENTS:
- macOS 12+ (Apple Silicon: M1/M2/M3/M4)
- Python 3.13 (из python.org, framework-сборка)
- OpenAI API Key (https://platform.openai.com/api-keys), permission: Audio → Write
- Микрофон

УСТАНОВКА:
  1. bash install.sh
  2. Создайте файл .env с одной строкой:
       OPENAI_API_KEY=sk-...ваш-ключ...
  3. open ~/Applications/BVoice.app
  4. Разрешить в System Settings → Privacy & Security:
     - Input Monitoring → +, добавить ~/Applications/BVoice.app, тумблер ON
     - Accessibility → +, добавить ~/Applications/BVoice.app, тумблер ON
  5. При первом разговоре всплывет окно с запросом на разрешение для микрофона, нужно нажать Allow

ЯЗЫКИ:
- Текущий язык переключается через tray-меню BVoice → Language
- Хоткей переключения: Fn + Control (по умолчанию). Меняется в Settings GUI
  или вручную в config.json (поле "hotkey_lang_change_name":
  "control" / "shift" / "option" / "command" / "" — отключить).

- Список языков в меню задаётся в config.json, поле "languages":
       "languages": ["ru", "en"]              — только русский + английский
       "languages": ["", "ru", "en"]          — добавить Auto-Detect
       "languages": ["ru", "en", "de", "fr"]  — больше вариантов

- Whisper понимает любой ISO-код языка — просто впишите его в "languages",
- После правки config.json нужно перезапустить BVoice (tray → Перезапустить).

ХРАНЕНИЕ:
- API ключ:      ~/BVoice/.env
- Конфиг:       ~/BVoice/config.json
- Записи:        ~/BVoice/audio_history/  (.wav + .txt)
- Логи:          /tmp/bvoice.log

ПОДСКАЗКА:
 - Чтобы сразу открыть папки хранения в FINDER: CMD + SHIFT + G

COST: ~$0.006/min аудио (тарификация OpenAI Whisper)

СМ. ТАКЖЕ: UPDATES.md — что было изменено относительно оригинального кода и почему.

(c) 2026 Mad Twinz | madtwinz.com | beatland.app
