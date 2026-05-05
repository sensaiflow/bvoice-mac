#!/bin/bash
# BVoice installer (macOS, Apple Silicon, Python 3.13 from python.org)
# Builds a native ~/Applications/BVoice.app and stores user data in ~/BVoice/

set -e

SRC="$(cd "$(dirname "$0")" && pwd)"
APP="$HOME/Applications/BVoice.app"
DATA="$HOME/BVoice"
PY="/Library/Frameworks/Python.framework/Versions/3.13/bin/python3"
CERT_CMD="/Applications/Python 3.13/Install Certificates.command"

echo "=== BVoice installer ==="

if [ ! -x "$PY" ]; then
    echo "Python 3.13 not found at $PY"
    PY_VERSION="3.13.7"
    PY_PKG_URL="https://www.python.org/ftp/python/${PY_VERSION}/python-${PY_VERSION}-macos11.pkg"
    PY_PKG="/tmp/python-${PY_VERSION}-macos11.pkg"
    printf "Download and install Python ${PY_VERSION} from python.org now? Requires admin password. [Y/n] "
    read -r answer
    case "${answer:-Y}" in
        n|N|no|No)
            echo "Aborted. Install Python 3.13 manually from https://python.org and re-run install.sh"
            exit 1
            ;;
    esac
    echo "Downloading Python ${PY_VERSION} (~50 MB)..."
    curl -fL "$PY_PKG_URL" -o "$PY_PKG"
    echo "Installing (sudo will prompt for your password)..."
    sudo installer -pkg "$PY_PKG" -target /
    rm -f "$PY_PKG"
    if [ ! -x "$PY" ]; then
        echo "ERROR: Install completed but $PY still missing. Please install manually."
        exit 1
    fi
    echo "Python ${PY_VERSION} installed."
fi

echo "[1/6] Installing Python dependencies..."
"$PY" -m pip install --user --quiet \
    PyQt5 sounddevice numpy pynput \
    pyobjc-framework-Quartz pyobjc-framework-AVFoundation \
    py2app

echo "[2/6] Installing SSL certificates..."
if [ -f "$CERT_CMD" ]; then
    bash "$CERT_CMD" >/dev/null 2>&1 || true
fi

echo "[3/6] Setting up user data dir at $DATA..."
mkdir -p "$DATA"
cp "$SRC/app.py" "$DATA/"
[ -f "$DATA/config.json" ] || cp "$SRC/config.json" "$DATA/"

# Copy .env if present in source; otherwise warn the user — without it
# the app starts but can't talk to OpenAI.
if [ -f "$SRC/.env" ]; then
    [ -f "$DATA/.env" ] || cp "$SRC/.env" "$DATA/.env"
    echo "      .env copied to $DATA/.env"
else
    NEED_ENV=1
fi

echo "[4/6] Building BVoice.app (py2app alias mode)..."
cd "$SRC"
rm -rf build dist
"$PY" setup.py py2app -A >/dev/null

echo "[5/6] Installing to ~/Applications and stripping x86_64..."
mkdir -p "$HOME/Applications"
rm -rf "$APP"
mv "$SRC/dist/BVoice.app" "$APP"
EXE="$APP/Contents/MacOS/BVoice"
lipo "$EXE" -extract arm64 -output "$EXE.arm64"
mv "$EXE.arm64" "$EXE"
chmod +x "$EXE"

echo "[6/6] Code-signing (ad-hoc)..."
codesign --force --deep --sign - "$APP" 2>&1 | head -1

echo ""
echo "=== Done! ==="
echo ""

if [ -n "$NEED_ENV" ]; then
    echo "⚠️  WARNING: no .env found in source — приложение не сможет обращаться к OpenAI."
    echo "    Создайте файл $DATA/.env с одной строкой:"
    echo "        OPENAI_API_KEY=sk-...ваш-ключ..."
    echo ""
fi

echo "Next steps:"
echo "  1. Open BVoice:    open \"$APP\""
echo "  2. Grant permissions when macOS asks (Microphone, Input Monitoring,"
echo "     Accessibility)"
echo "  3. (Optional) Add $APP to Login Items for autostart"
echo ""
echo "Hotkey:        double-tap Fn = start, single tap Fn = stop"
echo "Lang switch:   Fn + Control (cycles through configured languages)"
echo "Logs:          /tmp/bvoice.log"
