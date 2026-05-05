from setuptools import setup

APP = ['app.py']
OPTIONS = {
    'argv_emulation': False,
    'plist': {
        'CFBundleName': 'BVoice',
        'CFBundleDisplayName': 'BVoice',
        'CFBundleIdentifier': 'com.madtwinz.bvoice',
        'LSUIElement': True,  # no dock icon, tray only
        'NSMicrophoneUsageDescription': 'BVoice needs microphone access for voice input.',
    },
    'packages': ['numpy', 'sounddevice'],
    'includes': ['AVFoundation', 'Quartz', 'AppKit'],
}

setup(
    app=APP,
    name='BVoice',
    options={'py2app': OPTIONS},
    setup_requires=['py2app'],
)