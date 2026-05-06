"""
BVoice — Desktop App
======================================
Голосовой ввод через OpenAI Whisper API.
Двойной клик Ё = toggle запись. Текст вставляется в активное окно.
Минимальный кружок-индикатор + system tray + история транскрибаций.
Single instance — не запускается больше одного.

(c) MadTwinz 2026
"""

APP_VERSION = "1.2.1"
APP_NAME = "BVoice"
APP_ABOUT = f"""{APP_NAME} v{APP_VERSION}

Голосовой ввод через OpenAI Whisper API.
Двойной тап Ё — запись/стоп.

Разработка: Mad Twinz (Daniel + KK)
madtwinz.com | beatland.app
(c) 2026 Mad Twinz"""

import sys
import os
# Redirect stdout/stderr to /tmp/bvoice.log when launched as bundle
if getattr(sys, 'frozen', False) or os.environ.get('BVOICE_LOG_REDIRECT'):
    try:
        _logf = open('/tmp/bvoice.log', 'a', buffering=1, encoding='utf-8', errors='replace')
        sys.stdout = _logf
        sys.stderr = _logf
    except Exception:
        pass
import time
import wave
import shutil
import tempfile
import threading
import datetime
import urllib.request
import urllib.error

import numpy as np
import sounddevice as sd
if sys.platform == 'darwin':
    kb_lib = None
else:
    import keyboard as kb_lib  # for key hooks + paste (SetWindowsHookEx)
from PyQt5.QtWidgets import (
    QApplication, QWidget, QLabel, QHBoxLayout, QVBoxLayout,
    QSystemTrayIcon, QMenu, QAction, QListWidget, QListWidgetItem,
    QPushButton, QMainWindow, QSplitter, QTextEdit, QComboBox,
)
from PyQt5.QtCore import Qt, QTimer, pyqtSignal, QObject, QSize
from PyQt5.QtGui import QFont, QColor, QPainter, QIcon, QPixmap, QKeySequence

# ── Config ─────────────────���────────────────────────────────

# Persistent user data lives in ~/BVoice (not inside the .app bundle).
# This makes config and history survive bundle reinstalls and avoids
# writing into a read-only signed bundle.
_USER_DATA_DIR = os.path.expanduser('~/BVoice')
if os.path.isdir(_USER_DATA_DIR):
    APP_DIR = _USER_DATA_DIR
elif getattr(sys, 'frozen', False):
    APP_DIR = os.path.dirname(sys.executable)
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))
_DEFAULT_AUDIO_DIR = os.path.join(APP_DIR, 'audio_history')
CONFIG_PATH = os.path.join(APP_DIR, 'config.json')

# Audio dir from config (can be overridden in settings)
def _get_audio_dir():
    import json as _j
    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as _f:
            return _j.load(_f).get('audio_dir', _DEFAULT_AUDIO_DIR)
    except Exception:
        return _DEFAULT_AUDIO_DIR

AUDIO_HISTORY_DIR = _get_audio_dir()
os.makedirs(AUDIO_HISTORY_DIR, exist_ok=True)


def load_config():
    """Load config from config.json."""
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            import json
            return json.load(f)
    return {}


def save_config(cfg):
    """Save config to config.json."""
    import json
    with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


def load_api_key():
    """Load OpenAI API key: ~/BVoice/.env → config.json → env var."""
    # 1. From ~/BVoice/.env (preferred — keeps secrets out of config.json)
    env_path = os.path.join(APP_DIR, '.env')
    if os.path.exists(env_path):
        try:
            with open(env_path, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if line.startswith('OPENAI_API_KEY='):
                        key = line.split('=', 1)[1].strip().strip('"').strip("'")
                        if key.startswith('sk-'):
                            return key
        except Exception:
            pass

    # 2. From config.json (legacy / Settings UI)
    cfg = load_config()
    key = cfg.get('api_key', '')
    if key.startswith('sk-'):
        return key

    # 3. Environment variable
    key = os.environ.get('OPENAI_API_KEY', '')
    if key.startswith('sk-'):
        return key

    return ''


# ── Audio Recording ─────────────────────────────────────────

class AudioRecorder:
    def __init__(self, sample_rate=48000, channels=1, device=None):
        self.sample_rate = sample_rate
        self.channels = channels
        self.recording = False
        self.frames = []
        self.stream = None
        self.device = device or self._find_working_mic()

    @staticmethod
    def _find_working_mic():
        """Use saved mic from config, or find one with signal."""
        # Check config first
        cfg = load_config()
        saved = cfg.get('mic_device', '')
        if saved:
            for i, dev in enumerate(sd.query_devices()):
                if dev.get('max_input_channels', 0) > 0 and dev['name'] == saved:
                    print(f'[whisper] Using saved mic: [{i}] {dev["name"]}')
                    return i
        # Auto-detect: find mic with signal
        try:
            import numpy as np
            for i, dev in enumerate(sd.query_devices()):
                if dev.get('max_input_channels', 0) > 0:
                    try:
                        audio = sd.rec(int(0.3 * 16000), samplerate=16000,
                                       channels=1, dtype='int16', device=i)
                        sd.wait()
                        if np.max(np.abs(audio)) > 30:
                            print(f'[whisper] Auto-selected mic: [{i}] {dev["name"]}')
                            return i
                    except Exception:
                        pass
        except Exception:
            pass
        return None  # fallback to system default

    def _resolve_device(self):
        """Re-resolve device by name each time (indices shift when devices plug/unplug)."""
        cfg = load_config()
        saved = cfg.get('mic_device', '')
        if saved:
            for i, dev in enumerate(sd.query_devices()):
                if dev.get('max_input_channels', 0) > 0 and dev['name'] == saved:
                    return i
        return self.device  # fallback to init-time device

    def start(self):
        self.frames = []
        self.recording = True
        self._rec_device = self._resolve_device()

        def _audio_callback(indata, frames, time_info, status):
            # Called by PortAudio on its own thread for every block.
            # Always copy & append — gapless capture.
            self.frames.append(indata.copy())

        def _record_loop():
            try:
                dev = self._rec_device
                try:
                    dev_info = sd.query_devices(dev) if dev is not None else None
                    name = dev_info['name'] if isinstance(dev_info, dict) else str(dev_info)
                except Exception:
                    name = '?'
                print(f'[whisper] Recording on [{dev}] {name}', flush=True)
                self._stream = sd.InputStream(
                    samplerate=self.sample_rate,
                    channels=self.channels,
                    dtype='int16',
                    device=dev,
                    callback=_audio_callback,
                )
                self._stream.start()
                # Wait for user-initiated stop
                while self.recording:
                    time.sleep(0.05)
                # Tail: keep the stream open another ~0.4s to catch the last word
                time.sleep(0.4)
                self._stream.stop()
                self._stream.close()
            except Exception as e:
                print(f"[whisper] Record thread error: {e}", flush=True)

        self._rec_thread = threading.Thread(target=_record_loop, daemon=True)
        self._rec_thread.start()

    def stop(self):
        self.recording = False
        if hasattr(self, '_rec_thread'):
            self._rec_thread.join(timeout=3)

        if not self.frames:
            return None

        audio_data = np.concatenate(self.frames, axis=0)
        if audio_data.ndim > 1:
            audio_data = audio_data[:, 0]  # ensure mono

        # Check audio energy — skip silence to prevent Whisper hallucinations
        rms = np.sqrt(np.mean(audio_data.astype(np.float32) ** 2))
        duration = len(audio_data) / self.sample_rate
        if rms < 10 or duration < 0.4:
            print(f'[whisper] Skipped: silence or too short (rms={rms:.0f}, dur={duration:.1f}s)')
            return None

        # Downsample 48k -> 16k before saving: Whisper resamples to 16k internally
        # anyway, and 16k WAV is 3x smaller on disk. Anti-alias by averaging
        # `factor` samples per output sample (cheap low-pass).
        TARGET_RATE = 16000
        save_rate = self.sample_rate
        save_audio = audio_data
        if self.sample_rate > TARGET_RATE and self.sample_rate % TARGET_RATE == 0:
            factor = self.sample_rate // TARGET_RATE
            n = (len(audio_data) // factor) * factor
            save_audio = (audio_data[:n].astype(np.int32)
                          .reshape(-1, factor).mean(axis=1)
                          .astype(np.int16))
            save_rate = TARGET_RATE

        # Save to permanent history
        ts = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
        audio_path = os.path.join(AUDIO_HISTORY_DIR, f'{ts}.wav')
        with wave.open(audio_path, 'wb') as wf:
            wf.setnchannels(self.channels)
            wf.setsampwidth(2)
            wf.setframerate(save_rate)
            wf.writeframes(save_audio.tobytes())

        return audio_path

    def cancel(self):
        self.recording = False
        if hasattr(self, '_rec_thread'):
            self._rec_thread.join(timeout=3)
        self.frames = []


# ── Whisper API ─────────────────────────────────────────────

# Known Whisper hallucination patterns (repeated on silence/noise)
_HALLUCINATION_PATTERNS = [
    'редактор субтитров', 'корректор', 'синецкая', 'егорова',
    'подписывайтесь на канал', 'subscribe', 'thank you for watching',
    'thanks for watching', 'спасибо за просмотр', 'подпишись',
    'музыка', 'аплодисменты', '♪', '♫', 'продолжение следует',
    'субтитры', 'subtitles', 'amara.org',
]


def _is_hallucination(text):
    """Detect known Whisper hallucinations."""
    low = text.lower().strip()
    if not low:
        return True
    for pat in _HALLUCINATION_PATTERNS:
        if pat in low:
            return True
    # Repeated short phrase (e.g. same word 3+ times)
    words = low.split()
    if len(words) >= 3 and len(set(words)) == 1:
        return True
    return False


def transcribe(api_key, audio_path, model='whisper-1', language=''):
    """Send audio to OpenAI Whisper API and return text."""
    boundary = f'----WhisperInput{int(time.time())}'

    with open(audio_path, 'rb') as f:
        file_data = f.read()

    if len(file_data) < 1000:
        return ''

    parts = []
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="audio.wav"\r\nContent-Type: audio/wav\r\n\r\n'.encode())
    parts.append(file_data)
    parts.append(b'\r\n')
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="model"\r\n\r\n{model}\r\n'.encode())
    if language:
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="language"\r\n\r\n{language}\r\n'.encode())
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="response_format"\r\n\r\ntext\r\n'.encode())
    # Empty prompt reduces hallucinations on silence
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="prompt"\r\n\r\n \r\n'.encode())
    parts.append(f'--{boundary}--\r\n'.encode())

    body = b''.join(parts)

    req = urllib.request.Request(
        'https://api.openai.com/v1/audio/transcriptions',
        data=body,
        headers={
            'Authorization': f'Bearer {api_key}',
            'Content-Type': f'multipart/form-data; boundary={boundary}',
        },
        method='POST',
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            text = resp.read().decode('utf-8').strip()
            if _is_hallucination(text):
                print(f'[whisper] Filtered hallucination: "{text}"')
                return ''
            return text
    except urllib.error.HTTPError as e:
        error_body = e.read().decode('utf-8')
        print(f'[whisper] API error {e.code}: {error_body}')
        return f'[Error: {e.code}]'
    except Exception as e:
        print(f'[whisper] Network error: {e}')
        return f'[Error: {e}]'


def transcribe_cloud(api_token, audio_path, proxy_url, language=''):
    """Send audio to BVoice Cloud proxy instead of OpenAI directly."""
    boundary = f'----BVoice{int(time.time())}'

    with open(audio_path, 'rb') as f:
        file_data = f.read()

    if len(file_data) < 1000:
        return ''

    parts = []
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="audio"; filename="audio.wav"\r\nContent-Type: audio/wav\r\n\r\n'.encode())
    parts.append(file_data)
    parts.append(b'\r\n')
    if language:
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="language"\r\n\r\n{language}\r\n'.encode())
    parts.append(f'--{boundary}--\r\n'.encode())

    body = b''.join(parts)

    req = urllib.request.Request(
        f'{proxy_url}/api/v1/transcribe',
        data=body,
        headers={
            'Authorization': f'Bearer {api_token}',
            'Content-Type': f'multipart/form-data; boundary={boundary}',
        },
        method='POST',
    )

    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            import json as _json
            data = _json.loads(resp.read().decode('utf-8'))
            remaining = data.get('remaining', '?')
            print(f'[whisper] Cloud: remaining={remaining}')
            return data.get('text', '')
    except urllib.error.HTTPError as e:
        error_body = e.read().decode('utf-8')
        print(f'[whisper] Cloud error {e.code}: {error_body}')
        try:
            import json as _json
            err = _json.loads(error_body)
            return f'[Error: {err.get("error", e.code)}]'
        except Exception:
            return f'[Error: {e.code}]'
    except Exception as e:
        print(f'[whisper] Cloud network error: {e}')
        return f'[Error: {e}]'


# ── Text Insertion (cross-platform clipboard) ──────────────

import subprocess as _sp

_IS_MAC = sys.platform == 'darwin'


def set_clipboard(txt):
    if _IS_MAC:
        # Use NSPasteboard directly — pbcopy without UTF-8 locale corrupts
        # non-ASCII text in bundle context.
        try:
            from AppKit import NSPasteboard
            pb = NSPasteboard.generalPasteboard()
            pb.clearContents()
            pb.setString_forType_(txt, 'public.utf8-plain-text')
            return
        except Exception:
            env = dict(os.environ)
            env['LC_CTYPE'] = 'UTF-8'
            p = _sp.Popen(['pbcopy'], stdin=_sp.PIPE, env=env)
            p.communicate(txt.encode('utf-8'))
    else:
        import ctypes, ctypes.wintypes
        u32 = ctypes.windll.user32
        k32 = ctypes.windll.kernel32
        # Set proper argtypes for 64-bit compatibility
        k32.GlobalAlloc.argtypes = [ctypes.wintypes.UINT, ctypes.c_size_t]
        k32.GlobalAlloc.restype = ctypes.c_void_p
        k32.GlobalLock.argtypes = [ctypes.c_void_p]
        k32.GlobalLock.restype = ctypes.c_void_p
        k32.GlobalUnlock.argtypes = [ctypes.c_void_p]
        u32.SetClipboardData.argtypes = [ctypes.wintypes.UINT, ctypes.c_void_p]
        u32.OpenClipboard(0)
        u32.EmptyClipboard()
        data = txt.encode('utf-16-le') + b'\x00\x00'
        h = k32.GlobalAlloc(0x0042, ctypes.c_size_t(len(data)))  # GMEM_MOVEABLE | GMEM_ZEROINIT
        p = k32.GlobalLock(h)
        ctypes.memmove(p, data, len(data))
        k32.GlobalUnlock(h)
        u32.SetClipboardData(13, h)
        u32.CloseClipboard()


def get_clipboard():
    if _IS_MAC:
        try:
            from AppKit import NSPasteboard
            pb = NSPasteboard.generalPasteboard()
            s = pb.stringForType_('public.utf8-plain-text')
            return s if s is not None else ''
        except Exception:
            try:
                env = dict(os.environ)
                env['LC_CTYPE'] = 'UTF-8'
                return _sp.check_output(['pbpaste'], text=True, env=env)
            except Exception:
                return ''
    else:
        import ctypes, ctypes.wintypes
        u32 = ctypes.windll.user32
        k32 = ctypes.windll.kernel32
        u32.OpenClipboard(0)
        h = u32.GetClipboardData(13)
        if h:
            p = k32.GlobalLock(h)
            txt = ctypes.wstring_at(p) if p else ''
            k32.GlobalUnlock(h)
            u32.CloseClipboard()
            return txt
        u32.CloseClipboard()
        return ''


def insert_text(text):
    """Insert text via clipboard + Cmd+V (mac) or Ctrl+V (win)."""
    old_clip = get_clipboard()
    set_clipboard(text)
    time.sleep(0.3)

    if _IS_MAC:
        # Verify clipboard was set
        current = get_clipboard()
        if current != text:
            set_clipboard(text)
            time.sleep(0.2)
        # Use CGEvent directly (works without Accessibility permission for System Events)
        import Quartz
        src = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStateHIDSystemState)
        # Cmd down
        cmd_down = Quartz.CGEventCreateKeyboardEvent(src, 55, True)  # 55 = Cmd
        Quartz.CGEventSetFlags(cmd_down, Quartz.kCGEventFlagMaskCommand)
        # V down
        v_down = Quartz.CGEventCreateKeyboardEvent(src, 9, True)  # 9 = V
        Quartz.CGEventSetFlags(v_down, Quartz.kCGEventFlagMaskCommand)
        # V up
        v_up = Quartz.CGEventCreateKeyboardEvent(src, 9, False)
        Quartz.CGEventSetFlags(v_up, Quartz.kCGEventFlagMaskCommand)
        # Cmd up
        cmd_up = Quartz.CGEventCreateKeyboardEvent(src, 55, False)
        # Post events
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, v_down)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, v_up)
        print(f"[whisper] Inserted via CGEvent: {text[:50]}", flush=True)
    else:
        time.sleep(0.15)
        print(f"[whisper] Inserting via Ctrl+V: {text[:60]}", flush=True)
        kb_lib.send('ctrl+v')

    def restore():
        time.sleep(1.0)
        try:
            set_clipboard(old_clip)
        except Exception:
            pass

    threading.Thread(target=restore, daemon=True).start()


# ── Signals ─────────────────────────────────────────────────

class Signals(QObject):
    status_changed = pyqtSignal(str, str)
    history_added = pyqtSignal(str, str, str)  # timestamp, audio_path, text
    start_rec = pyqtSignal()
    stop_rec = pyqtSignal()
    cancel_rec = pyqtSignal()
    cycle_lang = pyqtSignal()


# ── Overlay Dot (minimal, no blinking) ──────────────────────

class OverlayBar(QWidget):
    WIDTH = 70
    HEIGHT = 30
    DOT_R = 9

    # Muted hacker green
    COLOR_IDLE = QColor(80, 80, 80, 180)
    COLOR_REC = QColor(0, 200, 50, 230)
    COLOR_ARC = QColor(0, 120, 35, 180)
    COLOR_ERR = QColor(120, 20, 20, 180)
    COLOR_LANG = QColor(220, 220, 220, 200)

    def __init__(self):
        super().__init__()
        self.setWindowFlags(
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
            | Qt.BypassWindowManagerHint | Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_MacAlwaysShowToolWindow, True)

        self.status = 'idle'
        self.arc_angle = 360  # for snake ring animation
        self.language_text = ''
        self._position_bottom_center()

        self.arc_timer = QTimer()
        self.arc_timer.timeout.connect(self._tick_arc)

        if _IS_MAC:
            QTimer.singleShot(100, self._set_mac_window_level)
            self._level_timer = QTimer(self)
            self._level_timer.timeout.connect(self._set_mac_window_level)
            self._level_timer.start(2000)

    def _ns_window(self):
        """Resolve our exact NSWindow via the Qt winId (NSView pointer)."""
        try:
            import objc
            view = objc.objc_object(c_void_p=int(self.winId()))
            return view.window()
        except Exception as e:
            print(f'[whisper] NSWindow resolve error: {e}')
            return None

    def _set_mac_window_level(self):
        if not self.isVisible():
            return
        w = self._ns_window()
        if w is None:
            return
        try:
            # NonactivatingPanel (1<<7=128) — utility-style panel that never
            # takes focus and is allowed above fullscreen apps.
            try:
                cur_mask = int(w.styleMask())
                w.setStyleMask_(cur_mask | (1 << 7))
            except Exception:
                pass

            # MoveToActiveSpace (1<<1) — window jumps to whichever Space
            # the user is currently on (incl. another app's fullscreen Space).
            # FullScreenAuxiliary (1<<8) — allowed in fullscreen Spaces.
            # IgnoresCycle (1<<6) — out of Cmd-Tab.
            behavior = (1 << 1) | (1 << 8) | (1 << 6)
            try:
                from Quartz import CGShieldingWindowLevel
                target_level = int(CGShieldingWindowLevel())
            except Exception:
                target_level = 1000

            w.setLevel_(target_level)
            w.setCollectionBehavior_(behavior)
            w.setHidesOnDeactivate_(False)
            w.orderFront_(None)
        except Exception as e:
            print(f'[whisper] NSWindow level error: {e}')

    def _position_bottom_center(self):
        screen = QApplication.primaryScreen().availableGeometry()
        x = (screen.width() - self.WIDTH) // 2
        y = screen.height() - self.HEIGHT - 12
        self.setGeometry(x, y, self.WIDTH, self.HEIGHT)

    def set_language(self, code):
        self.language_text = (code or 'auto').upper()
        self.update()

    def paintEvent(self, event):
        from PyQt5.QtGui import QPen, QFont
        from PyQt5.QtCore import QRect
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        r = self.DOT_R
        cx = r + 6
        cy = self.height() / 2

        if self.status == 'idle':
            painter.setPen(Qt.NoPen)
            painter.setBrush(self.COLOR_IDLE)
            painter.drawEllipse(int(cx - r), int(cy - r), int(r * 2), int(r * 2))

        elif self.status == 'listening':
            painter.setPen(Qt.NoPen)
            painter.setBrush(self.COLOR_REC)
            painter.drawEllipse(int(cx - r), int(cy - r), int(r * 2), int(r * 2))

        elif self.status == 'processing':
            painter.setPen(Qt.NoPen)
            painter.setBrush(self.COLOR_IDLE)
            painter.drawEllipse(int(cx - r), int(cy - r), int(r * 2), int(r * 2))

            if self.arc_angle > 0:
                pen = QPen(self.COLOR_ARC, 2.5)
                painter.setPen(pen)
                painter.setBrush(Qt.NoBrush)
                rect = QRect(int(cx - r - 3), int(cy - r - 3), int((r + 3) * 2), int((r + 3) * 2))
                painter.drawArc(rect, 90 * 16, self.arc_angle * 16)

        elif self.status == 'error':
            painter.setPen(Qt.NoPen)
            painter.setBrush(self.COLOR_ERR)
            painter.drawEllipse(int(cx - r), int(cy - r), int(r * 2), int(r * 2))

        if self.language_text:
            painter.setPen(QPen(self.COLOR_LANG))
            font = QFont('Helvetica', 10, QFont.Bold)
            painter.setFont(font)
            text_rect = QRect(int(cx + r + 4), 0, self.width() - int(cx + r + 4) - 2, self.height())
            painter.drawText(text_rect, Qt.AlignVCenter | Qt.AlignLeft, self.language_text)

    def set_status(self, status, message=''):
        self.status = status
        self.arc_timer.stop()

        if status == 'processing':
            self.arc_angle = 360
            self.arc_timer.start(80)  # ~3 sec total (360/10 * 80ms)
        elif status == 'error':
            QTimer.singleShot(3000, lambda: self.set_status('idle'))
        # 'done' → go straight to idle (no intermediate state)
        elif status == 'done':
            self.status = 'idle'

        if self.status == 'idle':
            self.hide()
        else:
            self._show_on_top()

        self.update()

    def _show_on_top(self):
        if not self.isVisible():
            self.show()
        if _IS_MAC:
            self._set_mac_window_level()

    def flash_language(self, duration_ms=1500):
        """Briefly surface the overlay (e.g. on language switch) then hide."""
        self._show_on_top()
        if not hasattr(self, '_flash_timer') or self._flash_timer is None:
            self._flash_timer = QTimer(self)
            self._flash_timer.setSingleShot(True)
            self._flash_timer.timeout.connect(
                lambda: self.hide() if self.status == 'idle' else None
            )
        self._flash_timer.stop()
        self._flash_timer.start(duration_ms)

    def _tick_arc(self):
        self.arc_angle -= 10
        if self.arc_angle <= 0:
            self.arc_timer.stop()
            self.set_status('idle')
        self.update()


# ── History Window (Play + Re-transcribe) ───────────────────

if _IS_MAC:
    winsound = None
else:
    import winsound


class HistoryItemWidget(QWidget):
    """Custom widget for each history entry: Play | Re-transcribe | timestamp | text."""

    def __init__(self, audio_path, txt_path, text, display_ts, app_ref, parent_window):
        super().__init__()
        self.audio_path = audio_path
        self.txt_path = txt_path
        self.text = text
        self.app_ref = app_ref
        self.parent_window = parent_window
        self.playing = False

        layout = QHBoxLayout()
        layout.setContentsMargins(4, 2, 4, 2)

        has_audio = os.path.exists(audio_path)

        # Play button (only when WAV is still present)
        self.btn_play = QPushButton('▶')
        self.btn_play.setFixedSize(30, 26)
        self.btn_play.setToolTip('Play audio')
        self.btn_play.clicked.connect(self._play)
        self.btn_play.setVisible(has_audio)
        layout.addWidget(self.btn_play)

        # Re-transcribe button (needs WAV)
        btn_retrans = QPushButton('↻')
        btn_retrans.setFixedSize(30, 26)
        btn_retrans.setToolTip('Re-transcribe')
        btn_retrans.clicked.connect(self._retranscribe)
        btn_retrans.setVisible(has_audio)
        layout.addWidget(btn_retrans)

        # Timestamp
        ts_label = QLabel(display_ts)
        ts_label.setFont(QFont('Segoe UI', 9))
        ts_label.setStyleSheet('color: gray;')
        ts_label.setFixedWidth(80)
        layout.addWidget(ts_label)

        # Text
        self.text_label = QLabel(text or '(not transcribed)')
        self.text_label.setFont(QFont('Segoe UI', 10))
        self.text_label.setWordWrap(True)
        layout.addWidget(self.text_label, 1)

        # Copy button
        btn_copy = QPushButton('Copy')
        btn_copy.setFixedSize(45, 26)
        btn_copy.clicked.connect(self._copy)
        layout.addWidget(btn_copy)

        # Insert button
        btn_insert = QPushButton('Insert')
        btn_insert.setFixedSize(50, 26)
        btn_insert.clicked.connect(self._insert)
        layout.addWidget(btn_insert)

        self.setLayout(layout)

    def _play(self):
        if not os.path.exists(self.audio_path):
            return
        if self.playing:
            if _IS_MAC:
                _sp.run(['killall', 'afplay'], capture_output=True)
            else:
                winsound.PlaySound(None, winsound.SND_PURGE)
            self.btn_play.setText('▶')
            self.playing = False
        else:
            self.btn_play.setText('■')
            self.playing = True

            def play_audio():
                try:
                    if _IS_MAC:
                        _sp.run(['afplay', self.audio_path])
                    else:
                        winsound.PlaySound(self.audio_path, winsound.SND_FILENAME)
                except Exception:
                    pass
                self.playing = False
                QTimer.singleShot(0, lambda: self.btn_play.setText('▶'))

            threading.Thread(target=play_audio, daemon=True).start()

    def _retranscribe(self):
        if not os.path.exists(self.audio_path):
            return
        self.text_label.setText('Transcribing...')

        def work():
            text = transcribe(self.app_ref.api_key, self.audio_path)
            if text and not text.startswith('[Error'):
                tp = self.txt_path or self.audio_path.replace('.wav', '.txt')
                with open(tp, 'w', encoding='utf-8') as f:
                    f.write(text)
                self.text = text
                QTimer.singleShot(0, lambda: self.text_label.setText(text))
            else:
                QTimer.singleShot(0, lambda: self.text_label.setText(text or 'No speech'))

        threading.Thread(target=work, daemon=True).start()

    def _copy(self):
        if self.text:
            QApplication.clipboard().setText(self.text)

    def _insert(self):
        if self.text:
            self.parent_window.hide()
            time.sleep(0.3)
            insert_text(self.text)


class BVoiceMainWindow(QMainWindow):
    """Main window with sidebar navigation: History, Settings, About."""

    def __init__(self, app_ref):
        super().__init__()
        self.app_ref = app_ref
        self.setWindowTitle(f'{APP_NAME} v{APP_VERSION}')
        self.setMinimumSize(650, 480)

        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QHBoxLayout(central)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # ── Sidebar ──
        sidebar = QWidget()
        sidebar.setFixedWidth(150)
        sidebar.setStyleSheet('background: #1a1b2e; border-right: 1px solid #2a2b3e;')
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(8, 12, 8, 12)
        sidebar_layout.setSpacing(4)

        logo = QLabel(f'{APP_NAME}')
        logo.setFont(QFont('Segoe UI', 13, QFont.Bold))
        logo.setStyleSheet('color: #33a6ff; border: none; padding: 4px;')
        sidebar_layout.addWidget(logo)

        ver = QLabel(f'v{APP_VERSION}')
        ver.setFont(QFont('Segoe UI', 8))
        ver.setStyleSheet('color: #666; border: none; padding: 0 4px 8px 4px;')
        sidebar_layout.addWidget(ver)

        self._nav_buttons = []
        for label, page_idx in [('History', 0), ('Settings', 1), ('About', 2)]:
            btn = QPushButton(label)
            btn.setFixedHeight(34)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setStyleSheet('''
                QPushButton { background: transparent; color: #ccc; border: none;
                              border-radius: 6px; text-align: left; padding: 6px 10px;
                              font-size: 12px; }
                QPushButton:hover { background: #2a2b3e; color: #fff; }
            ''')
            btn.clicked.connect(lambda _, i=page_idx: self._switch_page(i))
            sidebar_layout.addWidget(btn)
            self._nav_buttons.append(btn)

        sidebar_layout.addStretch()

        quit_btn = QPushButton('Quit')
        quit_btn.setFixedHeight(30)
        quit_btn.setStyleSheet('''
            QPushButton { background: transparent; color: #a55; border: none;
                          border-radius: 6px; text-align: left; padding: 6px 10px;
                          font-size: 11px; }
            QPushButton:hover { background: #3a1a1a; color: #f66; }
        ''')
        quit_btn.clicked.connect(self.app_ref._quit if hasattr(self.app_ref, '_quit') else self.close)
        sidebar_layout.addWidget(quit_btn)

        main_layout.addWidget(sidebar)

        # ── Pages (stacked) ──
        from PyQt5.QtWidgets import QStackedWidget, QScrollArea, QLineEdit, QCheckBox
        self.stack = QStackedWidget()
        self.stack.setStyleSheet('background: #0f1017;')
        main_layout.addWidget(self.stack, 1)

        # Page 0: History
        history_page = QWidget()
        hl = QVBoxLayout(history_page)
        hl.setContentsMargins(16, 16, 16, 16)
        htitle = QLabel('History')
        htitle.setFont(QFont('Segoe UI', 14, QFont.Bold))
        htitle.setStyleSheet('color: #eee;')
        hl.addWidget(htitle)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setStyleSheet('border: none;')
        self.scroll_content = QWidget()
        self.scroll_layout = QVBoxLayout(self.scroll_content)
        self.scroll_layout.setSpacing(2)
        self.scroll_layout.addStretch()
        self.scroll.setWidget(self.scroll_content)
        hl.addWidget(self.scroll)
        self.stack.addWidget(history_page)

        # Page 1: Settings
        settings_page = QWidget()
        from PyQt5.QtWidgets import QLineEdit, QCheckBox, QFileDialog, QScrollArea as SA2
        settings_scroll = SA2()
        settings_scroll.setWidgetResizable(True)
        settings_scroll.setStyleSheet('border: none;')
        settings_inner = QWidget()
        sl = QVBoxLayout(settings_inner)
        sl.setContentsMargins(16, 16, 16, 16)
        sl.setSpacing(10)

        stitle = QLabel('Settings')
        stitle.setFont(QFont('Segoe UI', 14, QFont.Bold))
        stitle.setStyleSheet('color: #eee;')
        sl.addWidget(stitle)

        _input_style = 'background: #1a1b2e; color: #eee; border: 1px solid #333; border-radius: 4px; padding: 6px;'
        _label_style = 'color: #ccc;'
        _hint_style = 'color: #666;'

        # --- Mode selector ---
        from PyQt5.QtWidgets import QRadioButton, QButtonGroup
        cfg = load_config()
        mode_label = QLabel('Transcription mode:')
        mode_label.setFont(QFont('Segoe UI', 10))
        mode_label.setStyleSheet(_label_style)
        sl.addWidget(mode_label)

        mode_row = QHBoxLayout()
        self.radio_own = QRadioButton('Own API Key')
        self.radio_own.setStyleSheet('color: #ccc;')
        self.radio_cloud = QRadioButton('BVoice Cloud')
        self.radio_cloud.setStyleSheet('color: #33a6ff;')
        self.mode_group = QButtonGroup()
        self.mode_group.addButton(self.radio_own, 0)
        self.mode_group.addButton(self.radio_cloud, 1)
        if cfg.get('mode') == 'cloud':
            self.radio_cloud.setChecked(True)
        else:
            self.radio_own.setChecked(True)
        mode_row.addWidget(self.radio_own)
        mode_row.addWidget(self.radio_cloud)
        mode_row.addStretch()
        sl.addLayout(mode_row)

        # --- Cloud: API Token ---
        self.cloud_widget = QWidget()
        cloud_layout = QVBoxLayout(self.cloud_widget)
        cloud_layout.setContentsMargins(0, 0, 0, 0)
        cloud_layout.setSpacing(6)

        token_label = QLabel('API Token (from @beatvoice_bot):')
        token_label.setFont(QFont('Segoe UI', 10))
        token_label.setStyleSheet(_label_style)
        cloud_layout.addWidget(token_label)

        self.token_input = QLineEdit()
        self.token_input.setPlaceholderText('Paste token from Telegram bot...')
        self.token_input.setFont(QFont('Segoe UI', 10))
        self.token_input.setStyleSheet(_input_style)
        if cfg.get('api_token'):
            self.token_input.setText(cfg['api_token'])
        cloud_layout.addWidget(self.token_input)

        bot_link = QLabel('<a href="https://t.me/beatvoice_bot" style="color: #33a6ff;">Get token → @beatvoice_bot</a>')
        bot_link.setOpenExternalLinks(True)
        bot_link.setFont(QFont('Segoe UI', 9))
        cloud_layout.addWidget(bot_link)
        sl.addWidget(self.cloud_widget)

        # --- Own Key: OpenAI API Key ---
        self.own_widget = QWidget()
        own_layout = QVBoxLayout(self.own_widget)
        own_layout.setContentsMargins(0, 0, 0, 0)
        own_layout.setSpacing(6)

        key_label = QLabel('OpenAI API Key:')
        key_label.setFont(QFont('Segoe UI', 10))
        key_label.setStyleSheet(_label_style)
        own_layout.addWidget(key_label)

        self.key_input = QLineEdit()
        self.key_input.setEchoMode(QLineEdit.Password)
        self.key_input.setPlaceholderText('sk-...')
        self.key_input.setFont(QFont('Segoe UI', 10))
        self.key_input.setStyleSheet(_input_style)
        if cfg.get('api_key'):
            self.key_input.setText(cfg['api_key'])
        own_layout.addWidget(self.key_input)

        key_row = QHBoxLayout()
        self.show_key_cb = QCheckBox('Show')
        self.show_key_cb.setStyleSheet('color: #999;')
        self.show_key_cb.toggled.connect(lambda on: self.key_input.setEchoMode(
            QLineEdit.Normal if on else QLineEdit.Password))
        key_row.addWidget(self.show_key_cb)

        api_link = QLabel('<a href="https://platform.openai.com/api-keys" style="color: #33a6ff;">Get API Key</a>')
        api_link.setOpenExternalLinks(True)
        api_link.setFont(QFont('Segoe UI', 9))
        key_row.addWidget(api_link)
        key_row.addStretch()
        own_layout.addLayout(key_row)
        sl.addWidget(self.own_widget)

        # Toggle visibility based on mode
        def _toggle_mode():
            is_cloud = self.radio_cloud.isChecked()
            self.cloud_widget.setVisible(is_cloud)
            self.own_widget.setVisible(not is_cloud)
        self.radio_own.toggled.connect(lambda: _toggle_mode())
        _toggle_mode()

        self.status_label = QLabel('')
        self.status_label.setFont(QFont('Segoe UI', 9))
        self._update_key_status()
        sl.addWidget(self.status_label)

        # --- Hotkey (capture field) ---
        hk_label = QLabel('Hotkey (double-tap to record):')
        hk_label.setFont(QFont('Segoe UI', 10))
        hk_label.setStyleSheet(_label_style + ' margin-top: 8px;')
        sl.addWidget(hk_label)

        self._captured_scan_code = cfg.get('hotkey_scan', 41)
        self._captured_key_name = cfg.get('hotkey_name', 'Ё (`)')

        self.hotkey_field = QLineEdit()
        self.hotkey_field.setReadOnly(True)
        self.hotkey_field.setText(self._captured_key_name)
        self.hotkey_field.setFont(QFont('Segoe UI', 11, QFont.Bold))
        self.hotkey_field.setAlignment(Qt.AlignCenter)
        self.hotkey_field.setStyleSheet(_input_style + ' color: #33a6ff; cursor: pointer;')
        self.hotkey_field.setPlaceholderText('Click and press any key...')
        self.hotkey_field.setFocusPolicy(Qt.StrongFocus)
        self.hotkey_field.installEventFilter(self)
        self._hotkey_listening = False
        sl.addWidget(self.hotkey_field)

        hk_hint = QLabel('Click the field, then press the key you want to use. Restart required.')
        hk_hint.setFont(QFont('Segoe UI', 8))
        hk_hint.setStyleSheet(_hint_style)
        hk_hint.setWordWrap(True)
        sl.addWidget(hk_hint)

        # --- Language switch hotkey (Fn + <modifier>) ---
        lc_label = QLabel('Language switch hotkey (Fn + …):')
        lc_label.setFont(QFont('Segoe UI', 10))
        lc_label.setStyleSheet(_label_style + ' margin-top: 8px;')
        sl.addWidget(lc_label)

        self.lang_combo = QComboBox()
        self.lang_combo.setFont(QFont('Segoe UI', 10))
        self.lang_combo.setStyleSheet(_input_style + ' padding: 4px 8px;')
        self._lang_modifier_options = [
            ('control', 'Control'),
            ('shift',   'Shift'),
            ('option',  'Option (⌥)'),
            ('command', 'Command (⌘)'),
            ('',        'Disabled'),
        ]
        for code, label in self._lang_modifier_options:
            self.lang_combo.addItem(label, code)
        current_mod = cfg.get('hotkey_lang_change_name', 'control')
        for i, (code, _) in enumerate(self._lang_modifier_options):
            if code == current_mod:
                self.lang_combo.setCurrentIndex(i)
                break
        sl.addWidget(self.lang_combo)

        lc_hint = QLabel('Hold Fn, tap the chosen modifier — cycles through languages list. Restart required.')
        lc_hint.setFont(QFont('Segoe UI', 8))
        lc_hint.setStyleSheet(_hint_style)
        lc_hint.setWordWrap(True)
        sl.addWidget(lc_hint)

        # --- Save folder ---
        folder_label = QLabel('Save recordings to:')
        folder_label.setFont(QFont('Segoe UI', 10))
        folder_label.setStyleSheet(_label_style + ' margin-top: 8px;')
        sl.addWidget(folder_label)

        folder_row = QHBoxLayout()
        self.folder_input = QLineEdit()
        self.folder_input.setText(cfg.get('audio_dir', AUDIO_HISTORY_DIR))
        self.folder_input.setFont(QFont('Segoe UI', 9))
        self.folder_input.setStyleSheet(_input_style)
        folder_row.addWidget(self.folder_input, 1)

        btn_browse = QPushButton('Browse')
        btn_browse.setFixedHeight(30)
        btn_browse.setStyleSheet('background: #2a2b3e; color: #ccc; border: 1px solid #333; border-radius: 4px; padding: 0 12px;')
        btn_browse.clicked.connect(self._browse_folder)
        folder_row.addWidget(btn_browse)
        sl.addLayout(folder_row)

        sl.addStretch()

        btn_save = QPushButton('Save')
        btn_save.setFixedHeight(34)
        btn_save.setStyleSheet('background: #33a6ff; color: #111; border: none; border-radius: 6px; font-weight: bold;')
        btn_save.clicked.connect(self._save_settings)
        sl.addWidget(btn_save)

        settings_scroll.setWidget(settings_inner)
        sp_layout = QVBoxLayout(settings_page)
        sp_layout.setContentsMargins(0, 0, 0, 0)
        sp_layout.addWidget(settings_scroll)
        self.stack.addWidget(settings_page)

        # Page 2: About
        about_page = QWidget()
        al = QVBoxLayout(about_page)
        al.setContentsMargins(16, 16, 16, 16)
        about_text = QLabel(APP_ABOUT)
        about_text.setFont(QFont('Segoe UI', 11))
        about_text.setStyleSheet('color: #ccc;')
        about_text.setWordWrap(True)
        al.addWidget(about_text)
        al.addStretch()
        self.stack.addWidget(about_page)

        # Default page
        self._switch_page(0)
        self._load_history()

    def eventFilter(self, obj, event):
        """Capture key press in hotkey field."""
        from PyQt5.QtCore import QEvent
        if obj == self.hotkey_field and event.type() == QEvent.KeyPress:
            scan = event.nativeScanCode()
            key_name = event.text() or QKeySequence(event.key()).toString()
            if not key_name or key_name.isspace():
                # Special keys
                from PyQt5.QtCore import Qt as QtKeys
                special = {
                    QtKeys.Key_F1: 'F1', QtKeys.Key_F2: 'F2', QtKeys.Key_F3: 'F3',
                    QtKeys.Key_F4: 'F4', QtKeys.Key_F5: 'F5', QtKeys.Key_F6: 'F6',
                    QtKeys.Key_F7: 'F7', QtKeys.Key_F8: 'F8', QtKeys.Key_F9: 'F9',
                    QtKeys.Key_F10: 'F10', QtKeys.Key_F11: 'F11', QtKeys.Key_F12: 'F12',
                    QtKeys.Key_Pause: 'Pause', QtKeys.Key_Insert: 'Insert',
                    QtKeys.Key_ScrollLock: 'Scroll Lock', QtKeys.Key_Home: 'Home',
                    QtKeys.Key_End: 'End', QtKeys.Key_CapsLock: 'Caps Lock',
                    QtKeys.Key_Space: 'Space',
                }
                key_name = special.get(event.key(), f'Key {scan}')
            self._captured_scan_code = scan
            self._captured_key_name = f'{key_name} (scan {scan})'
            self.hotkey_field.setText(self._captured_key_name)
            self.hotkey_field.setStyleSheet(
                'background: #1a1b2e; color: #22C55E; border: 1px solid #22C55E; border-radius: 4px; padding: 6px;')
            return True
        if obj == self.hotkey_field and event.type() == QEvent.FocusIn:
            self.hotkey_field.setText('Press any key...')
            self.hotkey_field.setStyleSheet(
                'background: #1a1b2e; color: #ff9933; border: 1px solid #ff9933; border-radius: 4px; padding: 6px;')
            return False
        if obj == self.hotkey_field and event.type() == QEvent.FocusOut:
            self.hotkey_field.setText(self._captured_key_name)
            self.hotkey_field.setStyleSheet(
                'background: #1a1b2e; color: #33a6ff; border: 1px solid #333; border-radius: 4px; padding: 6px;')
            return False
        return super().eventFilter(obj, event)

    def _browse_folder(self):
        from PyQt5.QtWidgets import QFileDialog
        folder = QFileDialog.getExistingDirectory(self, 'Select folder for recordings', self.folder_input.text())
        if folder:
            self.folder_input.setText(folder)

    def _switch_page(self, idx):
        self.stack.setCurrentIndex(idx)
        for i, btn in enumerate(self._nav_buttons):
            if i == idx:
                btn.setStyleSheet('''
                    QPushButton { background: #2a2b3e; color: #33a6ff; border: none;
                                  border-radius: 6px; text-align: left; padding: 6px 10px;
                                  font-size: 12px; font-weight: bold; }
                ''')
            else:
                btn.setStyleSheet('''
                    QPushButton { background: transparent; color: #ccc; border: none;
                                  border-radius: 6px; text-align: left; padding: 6px 10px;
                                  font-size: 12px; }
                    QPushButton:hover { background: #2a2b3e; color: #fff; }
                ''')

    def _load_history(self):
        while self.scroll_layout.count() > 1:
            item = self.scroll_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        # Iterate over .txt files — WAVs are deleted after successful transcription
        # but a few may linger (failed transcription / older history).
        seen = set()
        entries = []
        for fname in os.listdir(AUDIO_HISTORY_DIR):
            if fname.endswith('.txt'):
                stem = fname[:-4]
            elif fname.endswith('.wav'):
                stem = fname[:-4]
            else:
                continue
            if stem in seen:
                continue
            seen.add(stem)
            entries.append(stem)

        for stem in sorted(entries, reverse=True):
            txt_path = os.path.join(AUDIO_HISTORY_DIR, stem + '.txt')
            audio_path = os.path.join(AUDIO_HISTORY_DIR, stem + '.wav')
            text = ''
            if os.path.exists(txt_path):
                with open(txt_path, 'r', encoding='utf-8') as f:
                    text = f.read().strip()
            try:
                dt = datetime.datetime.strptime(stem, '%Y%m%d_%H%M%S')
                display_ts = dt.strftime('%H:%M:%S')
            except ValueError:
                display_ts = stem
            widget = HistoryItemWidget(audio_path, txt_path, text, display_ts, self.app_ref, self)
            self.scroll_layout.insertWidget(self.scroll_layout.count() - 1, widget)

    def add_entry(self, timestamp, audio_path, text):
        self._load_history()

    def _update_key_status(self):
        key = self.app_ref.api_key
        if key:
            src = 'config.json' if load_config().get('api_key') else '.env / environment'
            self.status_label.setText(f'Key loaded ({key[:7]}...{key[-4:]}) from {src}')
            self.status_label.setStyleSheet('color: green;')
        else:
            self.status_label.setText('No API key found — enter above or add to .env')
            self.status_label.setStyleSheet('color: red;')

    def _save_settings(self):
        cfg = load_config()

        # Save mode
        cfg['mode'] = 'cloud' if self.radio_cloud.isChecked() else 'own_key'

        # Save cloud token
        token = self.token_input.text().strip()
        if token:
            cfg['api_token'] = token

        # Save own API key
        key = self.key_input.text().strip()
        if key and key.startswith('sk-'):
            cfg['api_key'] = key
            self.app_ref.api_key = key
        elif not key and cfg.get('mode') == 'own_key':
            cfg.pop('api_key', None)
            self.app_ref.api_key = load_api_key()
        elif key and not key.startswith('sk-') and cfg.get('mode') == 'own_key':
            self.status_label.setText('Invalid key format (must start with sk-)')
            self.status_label.setStyleSheet('color: red;')
            return

        # Save hotkey
        old_scan = cfg.get('hotkey_scan', 41)
        cfg['hotkey_scan'] = self._captured_scan_code
        cfg['hotkey_name'] = self._captured_key_name
        hotkey_changed = (self._captured_scan_code != old_scan)

        # Save language switch modifier
        old_mod = cfg.get('hotkey_lang_change_name', 'control')
        new_mod = self.lang_combo.currentData()
        cfg['hotkey_lang_change_name'] = new_mod
        if new_mod != old_mod:
            hotkey_changed = True

        # Save audio folder
        new_dir = self.folder_input.text().strip()
        if new_dir and os.path.isdir(new_dir):
            cfg['audio_dir'] = new_dir
        elif new_dir:
            os.makedirs(new_dir, exist_ok=True)
            cfg['audio_dir'] = new_dir

        save_config(cfg)
        self._update_key_status()

        if hotkey_changed:
            self.status_label.setText(f'Saved! Hotkey changed — restart to apply')
            self.status_label.setStyleSheet('color: orange;')
        else:
            self.status_label.setText('Saved!')
            self.status_label.setStyleSheet('color: green;')

# Keep old class names as aliases for compatibility
HistoryWindow = BVoiceMainWindow
SettingsWindow = BVoiceMainWindow


# ── Tray Icon ───────────────────────────────────────────────

def create_tray_icon():
    """Green circle with thick white border (like AquaVoice but green)."""
    pixmap = QPixmap(64, 64)
    pixmap.fill(QColor(0, 0, 0, 0))
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    from PyQt5.QtGui import QPen
    # White border
    painter.setPen(QPen(QColor(255, 255, 255, 255), 6))
    painter.setBrush(QColor(40, 200, 60, 255))  # green fill
    painter.drawEllipse(6, 6, 52, 52)
    painter.end()
    return QIcon(pixmap)


# ── Single Instance Lock ────────────────────────────────────

SHOW_EVENT_NAME = "BVoice_ShowWindow"

def signal_existing_instance():
    """Signal the running instance to show its window."""
    import ctypes
    event = ctypes.windll.kernel32.OpenEventW(0x2, False, SHOW_EVENT_NAME)  # EVENT_MODIFY_STATE
    if event:
        ctypes.windll.kernel32.SetEvent(event)
        ctypes.windll.kernel32.CloseHandle(event)
        print('[whisper] Signaled running instance to show window.')
    else:
        print('[whisper] Could not signal running instance.')

def acquire_lock():
    """Ensure only one instance via lock file (cross-platform)."""
    lock_path = '/tmp/bvoice.lock'
    if _IS_MAC:
        import fcntl
        lock_fd = open(lock_path, 'w')
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            lock_fd.write(str(os.getpid()))
            lock_fd.flush()
            return lock_fd
        except IOError:
            print('[whisper] Already running! Exiting.')
            sys.exit(0)
    else:
        import ctypes
        mutex = ctypes.windll.kernel32.CreateMutexW(None, True, "BVoice_SingleInstance")
        last_err = ctypes.windll.kernel32.GetLastError()
        if last_err == 183:
            signal_existing_instance()
            sys.exit(0)
        return mutex


# ── Double-tap Ё detector ──────────────────────────────────

# Hotkey presets: display name → (scan_code, key_name)
HOTKEY_PRESETS = {
    'Ё (`)': (41, 'Ё'),
    'F1': (59, 'F1'),
    'F2': (60, 'F2'),
    'F5': (63, 'F5'),
    'F9': (67, 'F9'),
    'F10': (68, 'F10'),
    'Scroll Lock': (70, 'Scroll Lock'),
    'Pause': (69, 'Pause'),
    'Insert': (82, 'Insert'),
}
DEFAULT_HOTKEY = 'Ё (`)'


class TapDetector:
    """
    Double-tap hotkey = start recording.
    Single tap hotkey = stop recording (when already recording).
    Key is SUPPRESSED — never reaches the active window.
    """

    DOUBLE_TAP_THRESHOLD = 0.4  # seconds

    def __init__(self, on_start, on_stop, is_recording_fn, scan_code=41,
                 on_cycle_lang=None, on_cancel=None):
        self.on_start = on_start
        self.on_stop = on_stop
        self.on_cycle_lang = on_cycle_lang
        self.on_cancel = on_cancel
        self.is_recording = is_recording_fn
        self.scan_code = scan_code
        self.last_tap_time = 0
        self._rec_start_time = 0

    def start(self):
        if _IS_MAC:
            self._start_mac_event_tap()
        else:
            kb_lib.hook_key(self.scan_code, self._on_event, suppress=True)

    def _start_mac_event_tap(self):
        """Listen-only CGEventTap — works WITHOUT Accessibility/Input Monitoring."""
        import json as _j
        _config_path = os.path.join(APP_DIR, 'config.json')
        try:
            with open(_config_path, 'r') as _f:
                _cfg = _j.load(_f)
            vk_hotkey = _cfg.get('hotkey_mac_vk', 50)
        except Exception:
            vk_hotkey = 50

        import Quartz

        use_fn = (vk_hotkey == 63)
        self._fn_pressed = False
        self._mod_pressed = False

        # Modifier key for language cycle combo (Fn+<modifier>).
        # Configurable via config.json "hotkey_lang_change_name":
        #   "control" / "shift" / "option" / "command" / "" (disabled)
        _mod_map = {
            'control': Quartz.kCGEventFlagMaskControl,
            'shift':   Quartz.kCGEventFlagMaskShift,
            'option':  Quartz.kCGEventFlagMaskAlternate,
            'command': Quartz.kCGEventFlagMaskCommand,
        }
        mod_name = (_cfg.get('hotkey_lang_change_name', 'control') or '').lower()
        cycle_mask = _mod_map.get(mod_name, 0)

        def callback(proxy, event_type, event, refcon):
            if event_type == 0xFFFFFFFE:  # tap disabled by timeout
                Quartz.CGEventTapEnable(self._tap_ref, True)
                return event
            if use_fn and event_type == Quartz.kCGEventFlagsChanged:
                flags = Quartz.CGEventGetFlags(event)
                fn_now = bool(flags & Quartz.kCGEventFlagMaskSecondaryFn)
                mod_now = bool(flags & cycle_mask) if cycle_mask else False
                # Fn+<modifier> combo: cycle through configured languages.
                # Detected when modifier transitions 0->1 while Fn is held.
                if cycle_mask and fn_now and mod_now and not self._mod_pressed:
                    if self.on_cycle_lang:
                        self.on_cycle_lang()
                    # Consume the Fn-tap so it doesn't accidentally arm a
                    # double-tap recording start.
                    self.last_tap_time = 0
                    self._mod_pressed = mod_now
                    self._fn_pressed = fn_now
                    return event
                # Plain Fn press (no modifier) — normal hotkey logic.
                if fn_now and not self._fn_pressed and not mod_now:
                    self._handle_tap()
                self._fn_pressed = fn_now
                self._mod_pressed = mod_now
                return event  # don't suppress — Fn used in system combos
            if event_type in (Quartz.kCGEventKeyDown, Quartz.kCGEventKeyUp):
                vk = Quartz.CGEventGetIntegerValueField(event, Quartz.kCGKeyboardEventKeycode)
                if vk == vk_hotkey:
                    if event_type == Quartz.kCGEventKeyDown:
                        is_repeat = Quartz.CGEventGetIntegerValueField(
                            event, Quartz.kCGKeyboardEventAutorepeat)
                        if not is_repeat:
                            self._handle_tap()
                    return None  # suppress hotkey
                # Escape (vk=53) while recording — cancel without sending.
                if vk == 53 and self.on_cancel and self.is_recording():
                    if event_type == Quartz.kCGEventKeyDown:
                        is_repeat = Quartz.CGEventGetIntegerValueField(
                            event, Quartz.kCGKeyboardEventAutorepeat)
                        if not is_repeat:
                            self.on_cancel()
                    return None  # swallow Esc so it doesn't reach the focused app
            return event

        mask = (Quartz.CGEventMaskBit(Quartz.kCGEventKeyDown)
                | Quartz.CGEventMaskBit(Quartz.kCGEventKeyUp))
        if use_fn:
            mask |= Quartz.CGEventMaskBit(Quartz.kCGEventFlagsChanged)

        def _run_tap():
            # Retry loop: GUI session may not be ready at boot
            for attempt in range(30):
                self._tap_ref = Quartz.CGEventTapCreate(
                    Quartz.kCGSessionEventTap,
                    Quartz.kCGHeadInsertEventTap,
                    0,  # full mode — suppresses hotkey
                    mask,
                    callback, None)
                if self._tap_ref:
                    break

                print(f'[whisper] Waiting for GUI session... ({attempt+1}/30)', flush=True)
                time.sleep(2)

            if not self._tap_ref:
                print('[whisper] ERROR: CGEventTap failed after 30 retries', flush=True)
                return

            src = Quartz.CFMachPortCreateRunLoopSource(None, self._tap_ref, 0)
            loop = Quartz.CFRunLoopGetCurrent()
            Quartz.CFRunLoopAddSource(loop, src, Quartz.kCFRunLoopCommonModes)
            Quartz.CGEventTapEnable(self._tap_ref, True)
            print('[whisper] Hotkey listener ready', flush=True)
            Quartz.CFRunLoopRun()

        threading.Thread(target=_run_tap, daemon=True).start()

    def stop(self):
        try:
            if _IS_MAC and hasattr(self, '_ns_monitor') and self._ns_monitor:
                import Cocoa
                Cocoa.NSEvent.removeMonitor_(self._ns_monitor)
                self._ns_monitor = None
            elif not _IS_MAC:
                kb_lib.unhook_all()
        except Exception:
            pass

    def _on_event(self, event):
        if event.event_type != 'down':
            return
        self._handle_tap()

    MIN_RECORD_TIME = 0.3  # short guard, key-repeat filtered in CGEventTap

    def _handle_tap(self):
        now = time.time()
        if self.is_recording():
            if now - self._rec_start_time < self.MIN_RECORD_TIME:
                return  # key repeat protection
            self.last_tap_time = 0
            self.on_stop()
        elif now - self.last_tap_time < self.DOUBLE_TAP_THRESHOLD:
            self.last_tap_time = 0
            self._rec_start_time = now
            self.on_start()
        else:
            self.last_tap_time = now
# ── Main App ────────────────────────────────────────────────

class WhisperApp:
    @staticmethod
    def _request_mic_permission():
        """Request microphone permission on macOS (must be called before recording)."""
        if not _IS_MAC:
            return
        try:
            import AVFoundation as AVF
            import threading
            status = AVF.AVCaptureDevice.authorizationStatusForMediaType_(AVF.AVMediaTypeAudio)
            if status == 0:  # notDetermined
                event = threading.Event()
                AVF.AVCaptureDevice.requestAccessForMediaType_completionHandler_(
                    AVF.AVMediaTypeAudio, lambda granted: event.set())
                event.wait(10)
                print(f'[whisper] Mic permission: requested')
            elif status == 3:
                print('[whisper] Mic permission: authorized')
            else:
                print(f'[whisper] Mic permission: DENIED (status={status}) — enable in System Settings > Privacy > Microphone')
        except Exception as e:
            print(f'[whisper] Mic permission check error: {e}')

    def __init__(self):
        self.api_key = load_api_key()
        if not self.api_key:
            print('[whisper] WARNING: No OpenAI API key found!')

        self._request_mic_permission()
        self.recorder = AudioRecorder()
        self.is_recording = False
        self.signals = Signals()

        # Set AppUserModelID so Windows shows our icon on taskbar
        if not _IS_MAC:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('com.madtwinz.bvoice')

        self.qt_app = QApplication(sys.argv)
        self.qt_app.setQuitOnLastWindowClosed(False)
        self.qt_app.setApplicationName('BVoice')

        # Set app icon (taskbar + window title)
        icon_path = os.path.join(APP_DIR, 'bvoice.ico')
        if os.path.exists(icon_path):
            self.qt_app.setWindowIcon(QIcon(icon_path))

        # Overlay — hidden by default; surfaces only during record/processing
        # or briefly on language switch.
        self.overlay = OverlayBar()

        # History + Settings windows
        self.main_window = BVoiceMainWindow(self)
        self.history_window = self.main_window  # alias for signals

        # Last transcription for quick insert
        self.last_text = ''

        # System tray
        self.tray_icon = QSystemTrayIcon(create_tray_icon())
        self.tray_menu = QMenu()
        self._actions = []  # prevent garbage collection

        # -- Menu items --
        self._add_action('Просмотреть историю', self._show_history)
        self._add_action('Настройки', self._show_settings)
        self.action_insert_last = self._add_action('Вставить последнюю транскрипцию', self._insert_last)
        self.action_insert_last.setEnabled(False)

        self.tray_menu.addSeparator()

        # Microphone submenu
        self.mic_menu = self.tray_menu.addMenu('Микрофон')
        try:
            devices = sd.query_devices()
            saved_mic = load_config().get('mic_device', '')
            for i, dev in enumerate(devices):
                if dev.get('max_input_channels', 0) > 0:
                    a = self.mic_menu.addAction(dev['name'])
                    a.setCheckable(True)
                    if dev['name'] == saved_mic or (not saved_mic and i == self.recorder.device):
                        a.setChecked(True)
                    a.triggered.connect(lambda checked, idx=i, name=dev['name'], act=a: self._set_mic(idx, name, act))
                    self._actions.append(a)
        except Exception as e:
            print(f'[whisper] Mic list error: {e}')
            self._actions.append(self.mic_menu.addAction('(default)'))

        # Language submenu — items come from config.json "languages" list.
        # Each entry is "<code>" (auto-labeled) or {"code": "...", "label": "..."}.
        # Empty code "" means Auto-Detect. Default if config missing: ru + en.
        _LANG_LABELS = {
            '': 'Auto-Detect', 'ru': 'Русский', 'en': 'English',
            'de': 'Deutsch', 'fr': 'Français', 'es': 'Español',
            'it': 'Italiano', 'pt': 'Português', 'pl': 'Polski',
            'uk': 'Українська', 'ja': '日本語', 'zh': '中文', 'ko': '한국어',
        }
        cfg_for_lang = load_config()
        lang_list = cfg_for_lang.get('languages', ['', 'ru', 'en'])
        default_lang = cfg_for_lang.get('language', 'ru')

        self.lang_menu = self.tray_menu.addMenu('Language')
        self.language = default_lang
        self.overlay.set_language(default_lang)
        for entry in lang_list:
            if isinstance(entry, dict):
                code = entry.get('code', '')
                label = entry.get('label') or _LANG_LABELS.get(code, code or 'Auto-Detect')
            else:
                code = entry
                label = _LANG_LABELS.get(code, code or 'Auto-Detect')
            a = self.lang_menu.addAction(label)
            a.setCheckable(True)
            if code == default_lang:
                a.setChecked(True)
            a.triggered.connect(lambda checked, c=code, act=a: self._set_language(c, act))
            self._actions.append(a)

        self.tray_menu.addSeparator()

        self._add_action(f'О программе (v{APP_VERSION})', self._show_about)
        self._add_action('Перезапустить...', self._restart)
        self._add_action('Полностью выйти', self._quit)

        self.tray_icon.setContextMenu(self.tray_menu)
        self.tray_icon.activated.connect(self._tray_activated)
        self.tray_icon.setToolTip(f'{APP_NAME} v{APP_VERSION}')
        self.tray_icon.show()

        # Signals
        self.signals.status_changed.connect(self.overlay.set_status)
        self.signals.history_added.connect(self.history_window.add_entry)
        self.signals.start_rec.connect(self.start_recording)
        self.signals.stop_rec.connect(self.stop_recording)
        self.signals.cancel_rec.connect(self.cancel_recording)
        self.signals.cycle_lang.connect(self._cycle_language)

        # Kill AquaVoice — it steals the Ё key via its own CGEventTap
        if _IS_MAC:
            for proc in ['Aqua Voice', 'AquaMacOSBridge']:
                _sp.run(['pkill', '-9', '-f', proc], capture_output=True)
            time.sleep(0.5)

        # Hotkey from config
        cfg = load_config()
        scan_code = cfg.get('hotkey_scan', 41)
        hotkey_name = cfg.get('hotkey_name', 'Ё (`)')
        self.current_hotkey = hotkey_name

        # Tap detector
        self.tap_detector = TapDetector(
            on_start=lambda: self.signals.start_rec.emit(),
            on_stop=lambda: self.signals.stop_rec.emit(),
            is_recording_fn=lambda: self.is_recording,
            scan_code=scan_code,
            on_cycle_lang=lambda: self.signals.cycle_lang.emit(),
            on_cancel=lambda: self.signals.cancel_rec.emit(),
        )
        self.tap_detector.start()

        print(f'[whisper] Ready. Double-tap {hotkey_name} = start, single tap = stop.')
        print(f'[whisper] API key: {"loaded" if self.api_key else "MISSING"}')
        print(f'[whisper] Audio history: {AUDIO_HISTORY_DIR}')

        # Listen for "show window" signal from second instance
        if not _IS_MAC:
            self._setup_show_event()

        # If no API key — open settings on first launch
        if not self.api_key:
            QTimer.singleShot(1000, self._show_settings)

    def _setup_show_event(self):
        """Listen for signal from second instance to show history window."""
        import ctypes
        self._show_event = ctypes.windll.kernel32.CreateEventW(None, False, False, SHOW_EVENT_NAME)

        def poll_event():
            import ctypes
            result = ctypes.windll.kernel32.WaitForSingleObject(self._show_event, 0)
            if result == 0:  # WAIT_OBJECT_0 — event was signaled
                print('[whisper] Show window signal received')
                self._show_history()

        self._event_timer = QTimer()
        self._event_timer.timeout.connect(poll_event)
        self._event_timer.start(500)  # check every 500ms

    def _add_action(self, text, callback):
        action = self.tray_menu.addAction(text)
        action.triggered.connect(callback)
        self._actions.append(action)
        return action

    def toggle(self):
        if self.is_recording:
            self.stop_recording()
        else:
            self.start_recording()

    def start_recording(self):
        print('[whisper] >>> START RECORDING')
        self.is_recording = True
        self.signals.status_changed.emit('listening', '')
        try:
            self.recorder.start()
        except Exception as e:
            print(f'[whisper] Mic error: {e}')
            self.signals.status_changed.emit('error', f'Mic: {e}')
            self.is_recording = False

    def stop_recording(self):
        print('[whisper] >>> STOP RECORDING')
        self.is_recording = False
        self.signals.status_changed.emit('processing', '')

        def process():
            try:
                audio_path = self.recorder.stop()
                if not audio_path:
                    self.signals.status_changed.emit('error', 'No audio')
                    return

                cfg = load_config()
                if cfg.get('mode') == 'cloud' and cfg.get('api_token'):
                    text = transcribe_cloud(cfg['api_token'], audio_path,
                                            cfg.get('proxy_url', 'https://api.bvoice.beatland.app'),
                                            language=self.language)
                else:
                    text = transcribe(self.api_key, audio_path, language=self.language)

                # Save transcription text alongside audio
                if text and not text.startswith('[Error'):
                    txt_path = audio_path.replace('.wav', '.txt')
                    with open(txt_path, 'w', encoding='utf-8') as f:
                        f.write(text)

                    # Drop the WAV — text is in .txt and history reads from there.
                    try:
                        os.remove(audio_path)
                    except OSError:
                        pass

                    self.last_text = text
                    self.action_insert_last.setEnabled(True)
                    print(f'[whisper] "{text}"')
                    insert_text(text)
                    self.signals.status_changed.emit('done', text[:40])

                    ts = datetime.datetime.now().strftime('%H:%M:%S')
                    self.signals.history_added.emit(ts, audio_path, text)
                elif text and text.startswith('[Error'):
                    self.signals.status_changed.emit('error', text[:30])
                else:
                    self.signals.status_changed.emit('error', 'No speech')
            except Exception as e:
                print(f'[whisper] Error: {e}')
                self.signals.status_changed.emit('error', str(e)[:30])

        threading.Thread(target=process, daemon=True).start()

    def cancel_recording(self):
        if not self.is_recording:
            return
        print('[whisper] >>> CANCEL RECORDING')
        self.is_recording = False
        try:
            self.recorder.cancel()
        except Exception as e:
            print(f'[whisper] Cancel error: {e}')
        self.signals.status_changed.emit('idle', '')

    def _show_history(self):
        self.main_window._load_history()
        self.main_window._switch_page(0)
        self.main_window.show()
        self.main_window.raise_()
        self.main_window.activateWindow()

    def _show_settings(self):
        self.main_window._update_key_status()
        self.main_window._switch_page(1)
        self.main_window.show()
        self.main_window.raise_()
        self.main_window.activateWindow()

    def _insert_last(self):
        if self.last_text:
            insert_text(self.last_text)
            self.signals.status_changed.emit('done', self.last_text[:40])

    def _set_mic(self, device_idx, device_name, action=None):
        self.recorder.device = device_idx
        for a in self.mic_menu.actions():
            a.setChecked(False)
        if action:
            action.setChecked(True)
        import json
        cfg = load_config()
        cfg["mic_device"] = device_name
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        print(f"[whisper] Mic set to: [{device_idx}] {device_name}")

    def _set_language(self, code, action=None):
        self.language = code
        for a in self.lang_menu.actions():
            a.setChecked(False)
        if action:
            action.setChecked(True)
        try:
            self.overlay.set_language(code)
        except Exception:
            pass
        print(f'[whisper] Language set to: {code or "auto"}')

    def _cycle_language(self):
        """Switch to the next language in the tray menu (Fn+Ctrl hotkey)."""
        actions = self.lang_menu.actions()
        if not actions:
            return
        current_idx = next(
            (i for i, a in enumerate(actions) if a.isChecked()), -1
        )
        next_action = actions[(current_idx + 1) % len(actions)]
        # Recover the code from the menu order — we re-derive from label.
        label = next_action.text()
        code = next(
            (c for c, l in [
                ('', 'Auto-Detect'), ('ru', 'Русский'), ('en', 'English'),
                ('de', 'Deutsch'), ('fr', 'Français'), ('es', 'Español'),
                ('it', 'Italiano'), ('pt', 'Português'), ('pl', 'Polski'),
                ('uk', 'Українська'), ('ja', '日本語'), ('zh', '中文'),
                ('ko', '한국어'),
            ] if l == label), label
        )
        self._set_language(code, next_action)
        # Brief on-screen feedback — flash overlay for ~1.5s.
        try:
            self.overlay.flash_language(1500)
        except Exception:
            pass

    def _show_about(self):
        from PyQt5.QtWidgets import QMessageBox
        msg = QMessageBox()
        msg.setWindowTitle(f'{APP_NAME} v{APP_VERSION}')
        msg.setText(APP_ABOUT)
        msg.setIcon(QMessageBox.Information)
        msg.exec_()

    def _restart(self):
        self.tap_detector.stop()
        if self.recorder.stream:
            self.recorder.stop()
        os.execv(sys.executable, [sys.executable] + sys.argv)

    def _tray_activated(self, reason):
        if reason == QSystemTrayIcon.DoubleClick:
            self._show_history()

    def _quit(self):
        self.tap_detector.stop()
        if self.recorder.stream:
            self.recorder.stop()
        self.qt_app.quit()

    def run(self):
        sys.exit(self.qt_app.exec_())


if __name__ == '__main__':
    lock = acquire_lock()  # exits if already running
    app = WhisperApp()
    app.run()