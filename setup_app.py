"""
py2app build script for the menu bar app.
Build:  ./venv/bin/python setup_app.py py2app -A   (alias mode: fast, local)
Result: dist/Claude Remote Bot.app
"""
from setuptools import setup

APP = ["menubar.py"]
OPTIONS = {
    "iconfile": "icons/AppIcon.icns",
    "plist": {
        "CFBundleName": "Claude Remote Bot",
        "CFBundleDisplayName": "Claude Remote Bot",
        "CFBundleIdentifier": "com.claude.remotebot",
        "CFBundleShortVersionString": "1.0",
        "CFBundleVersion": "1",
        "LSUIElement": True,
    },
}

setup(
    name="Claude Remote Bot",
    app=APP,
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)
