#!/usr/bin/env python3
"""
Claude Remote Bot — macOS menu bar controller.

Menu:
- Receiving commands: pause / resume the bot (toggles a flag the bot reads)
- Stay awake: bot running => Mac won't sleep (pmset disablesleep, via a one-time
  passwordless-sudo grant the app sets up for you)
- Animation: rolling-bot menu bar animation on/off
- Remote sessions: live count + submenu to stop individual sessions
- Open log / Quit

The menu bar icon is a rolling robot (RunCat-style); the live session count is
baked into the rolling frames.
"""
import os
import sys
import json
import random
import fcntl
import getpass
import subprocess
import urllib.request
import urllib.parse
import rumps
from dotenv import load_dotenv

USER = getpass.getuser()

BASE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE, ".env"))

# LaunchAgent label (override via BOT_LABEL in .env). PLIST path derived from it.
LABEL = os.getenv("BOT_LABEL", "com.claude.remotebot")
PLIST = os.path.expanduser(f"~/Library/LaunchAgents/{LABEL}.plist")
LOG = "/tmp/claude-remote-bot.log"
TMUX = os.getenv("TMUX_BIN", "/opt/homebrew/bin/tmux")
APP_NAME = "Claude Remote Bot"

PAUSE_FILE = os.path.join(BASE, "paused")          # shared with bot.py: exists => paused
REMOTES_FILE = os.path.join(BASE, "remotes.json")  # shared with bot.py: remote-session metadata
ANIM_OFF_FILE = os.path.join(BASE, "anim_off")     # exists => rolling animation off
ICON_DIR = os.path.join(BASE, "icons")
ICON_RUN = os.path.join(ICON_DIR, "running.png")
ICON_PAUSED = os.path.join(ICON_DIR, "paused.png")
ICON_OFF = os.path.join(ICON_DIR, "off.png")
ICONS_OK = all(os.path.exists(p) for p in (ICON_RUN, ICON_PAUSED, ICON_OFF))


def _roll_set(label):
    return [os.path.join(ICON_DIR, f"roll_{label}_{i:02d}.png") for i in range(24)]


# Per-count rolling frame sets (the count is baked into the image). 10+ uses the "9+" set.
ROLL_SETS = {c: _roll_set(f"c{c}") for c in range(1, 10)}
ROLL_SETS[99] = _roll_set("c9p")

_LOCK_PATH = os.path.join(BASE, ".menubar.lock")
_lock_fh = None


def ensure_single_instance():
    """Exit immediately if another instance is running (avoids duplicate menu bar icons)."""
    global _lock_fh
    _lock_fh = open(_LOCK_PATH, "w")
    try:
        fcntl.flock(_lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        sys.exit(0)


TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHATS = [x for x in os.getenv("ALLOWED_CHAT_IDS", "").replace(" ", "").split(",") if x]


def notify(text):
    """Send a Telegram message directly from the menu app (works even when the bot is down)."""
    if not (TOKEN and CHATS):
        return
    for cid in CHATS:
        try:
            data = urllib.parse.urlencode({"chat_id": cid, "text": text}).encode()
            urllib.request.urlopen(
                f"https://api.telegram.org/bot{TOKEN}/sendMessage", data=data, timeout=5
            )
        except Exception:
            pass


def sh(args):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=10)
    except Exception:
        return None


def get_disablesleep():
    """Current system sleep-disabled state."""
    r = sh(["pmset", "-g"])
    if not r or r.returncode != 0:
        return False
    for line in r.stdout.splitlines():
        low = line.lower()
        if "sleepdisabled" in low or "disablesleep" in low:
            return line.split()[-1] in ("1", "true", "yes")
    return False


def set_disablesleep(on):
    """Toggle disablesleep via passwordless sudo (needs the sudoers grant)."""
    r = sh(["sudo", "-n", "/usr/bin/pmset", "-a", "disablesleep", "1" if on else "0"])
    return bool(r and r.returncode == 0)


# This app's .app path (for the login-item registration).
try:
    from Foundation import NSBundle
    _bp = NSBundle.mainBundle().bundlePath()
    APP_PATH = str(_bp) if _bp and str(_bp).endswith(".app") else os.path.expanduser(f"~/Applications/{APP_NAME}.app")
except Exception:
    APP_PATH = os.path.expanduser(f"~/Applications/{APP_NAME}.app")


def sudoers_ok():
    """Whether the passwordless pmset grant is in place (no-op set to current value)."""
    cur = get_disablesleep()
    r = sh(["sudo", "-n", "/usr/bin/pmset", "-a", "disablesleep", "1" if cur else "0"])
    return bool(r and r.returncode == 0)


def _osa(script, timeout=60):
    try:
        return subprocess.run(["osascript", "-e", script],
                              capture_output=True, text=True, timeout=timeout)
    except Exception:
        return None


def login_item_exists():
    r = _osa('tell application "System Events" to get the name of every login item')
    return bool(r and r.returncode == 0 and APP_NAME in (r.stdout or ""))


def add_login_item():
    r = _osa('tell application "System Events" to make login item at end '
             f'with properties {{path:"{APP_PATH}", hidden:true}}')
    return bool(r and r.returncode == 0)


def remote_sessions():
    """Live remote sessions + metadata (labels)."""
    r = sh([TMUX, "ls"])
    alive = set()
    if r and r.returncode == 0:
        alive = {l.split(":")[0] for l in r.stdout.splitlines() if l.startswith("rc-")}
    meta = {}
    try:
        for m in json.load(open(REMOTES_FILE)):
            meta[m["id"]] = m
    except Exception:
        pass
    out = []
    for sid in sorted(alive):
        m = meta.get(sid, {})
        out.append({"id": sid, "repo": m.get("repo", sid), "label": m.get("label", "")})
    return out


def remove_remote_meta(sid):
    try:
        lst = [x for x in json.load(open(REMOTES_FILE)) if x.get("id") != sid]
        json.dump(lst, open(REMOTES_FILE, "w"), ensure_ascii=False)
    except Exception:
        pass


def anim_enabled():
    return not os.path.exists(ANIM_OFF_FILE)


class ClaudeBotApp(rumps.App):
    def __init__(self):
        super().__init__("", icon=(ICON_RUN if ICONS_OK else None),
                         template=True, quit_button=None)
        self._icon = None          # current icon path (only update on change)
        self.last_state = None     # (alive, paused) change detection
        self.sudoers_needed = False
        self._cnt = 0              # latest remote-session count
        self._frames = None        # current rolling frame set (per count)
        self._pos = 0.0            # continuous frame position
        self._speed = 0.5          # current roll speed (frames/tick)
        self._target_speed = 0.7   # random target speed
        self._retarget = 0         # ticks until re-targeting

        self.bot_item = rumps.MenuItem("Receiving: …", callback=self.toggle_pause)
        self.awake_item = rumps.MenuItem("Stay awake: …", callback=self.on_awake_click)
        self.anim_item = rumps.MenuItem("Animation: …", callback=self.toggle_anim)
        self.remotes_item = rumps.MenuItem("Remote sessions: -", callback=None)
        self.kill_item = rumps.MenuItem("Stop all remote sessions", callback=self.kill_remotes)

        self.menu = [
            rumps.MenuItem(f"✦ {APP_NAME}"),
            None,
            self.bot_item,
            self.awake_item,
            self.anim_item,
            None,
            self.remotes_item,
            self.kill_item,
            None,
            rumps.MenuItem("Open log", callback=self.open_log),
            rumps.MenuItem("Quit (app only)", callback=self.quit_app),
        ]

        self.timer = rumps.Timer(self.refresh, 5)
        self.timer.start()
        self.anim_timer = rumps.Timer(self._animate, 0.09)
        self.anim_timer.start()
        self.refresh(None)

        # one-time onboarding shortly after the event loop starts
        self._onboarded = False
        self.onboard_timer = rumps.Timer(self._onboard_once, 1.5)
        self.onboard_timer.start()

    # ---------- state ----------
    def bot_running(self):
        r = sh(["launchctl", "list"])
        return bool(r and LABEL in r.stdout)

    def _animate(self, _):
        # When sessions exist and animation is on: roll with randomly eased speed.
        if not self._frames or not anim_enabled():
            return
        if self._retarget <= 0:
            self._target_speed = random.uniform(0.24, 2.8)
            self._retarget = random.randint(12, 45)
        self._retarget -= 1
        self._speed += (self._target_speed - self._speed) * 0.07
        self._pos = (self._pos + self._speed) % len(self._frames)
        p = self._frames[int(self._pos)]
        if os.path.exists(p):
            self.icon = p
            self._icon = p
            self._fit_icon()

    def toggle_anim(self, _):
        if os.path.exists(ANIM_OFF_FILE):
            try:
                os.remove(ANIM_OFF_FILE)
            except OSError:
                pass
        else:
            open(ANIM_OFF_FILE, "w").close()
        self.refresh(None)

    def _fit_icon(self):
        # rumps forces every NSImage to 20x20 (square) -> wide icons get squished.
        # Re-set the size to height 20pt with proportional width.
        try:
            img = self._icon_nsimage
            reps = img.representations()
            if reps:
                pw, ph = float(reps[0].pixelsWide()), float(reps[0].pixelsHigh())
                if ph > 0:
                    H = 20.0
                    img.setSize_((pw * H / ph, H))
                    self._nsapp.nsstatusitem.setImage_(img)
        except Exception:
            pass

    def _set_icon(self, path, fallback_title):
        if ICONS_OK and os.path.exists(path):
            if self._icon != path:
                self.icon = path
                self._icon = path
                self._fit_icon()
        else:
            self.title = fallback_title

    def refresh(self, _):
        alive = self.bot_running()
        # the bot process should always be up — load the LaunchAgent if it isn't
        if not alive:
            sh(["launchctl", "load", "-w", PLIST])
            alive = self.bot_running()
        paused = os.path.exists(PAUSE_FILE)

        state = (alive, paused)
        if self.last_state is not None and state != self.last_state:
            if not alive:
                notify("🔴 Claude bot process went down.")
            elif paused:
                notify("⏸️ Claude bot paused — not accepting commands. /resume or use the menu bar 🤖.")
            else:
                notify("🟢 Claude bot resumed — accepting commands.")
        self.last_state = state

        # bot running (alive & not paused) => keep Mac awake; otherwise allow sleep
        want_awake = alive and not paused
        cur = get_disablesleep()
        if cur != want_awake:
            set_disablesleep(want_awake)
            cur = get_disablesleep()

        if not alive:
            self.bot_item.title = "Receiving: bot process down 🔴"
            static_icon, fb = ICON_OFF, "🤖❌"
        elif paused:
            self.bot_item.title = "Receiving: ⏸️ paused  (click to resume)"
            static_icon, fb = ICON_PAUSED, "🤖⏸️"
        else:
            self.bot_item.title = "Receiving: 🟢 on  (click to pause)"
            static_icon, fb = ICON_RUN, "🤖"

        if want_awake and cur:
            self.awake_item.title = "Stay awake: 🟢 on — lid-closed OK"
            self.sudoers_needed = False
        elif want_awake and not cur:
            self.awake_item.title = "Stay awake: ⚠️ setup needed — click to set up"
            self.sudoers_needed = True
        else:
            self.awake_item.title = "Stay awake: ⚪️ sleep allowed (bot paused/off)"
            self.sudoers_needed = False
        self.anim_item.title = f"Animation: {'🟢 on' if anim_enabled() else '⚪️ off'}  (click to toggle)"

        sessions = remote_sessions()
        cnt = len(sessions)
        self.remotes_item.title = f"Remote sessions: {cnt}"
        self._cnt = cnt
        if cnt > 0 and anim_enabled():
            # rolling set with the count baked in (10+ -> 9+ set); number is in the image
            self._frames = ROLL_SETS.get(cnt) or ROLL_SETS.get(99)
            self.title = ""
        elif cnt > 0:  # animation off -> static icon + count text
            self._frames = None
            self.title = f" {cnt}"
            self._set_icon(static_icon, fb)
        else:  # no sessions -> static icon only
            self._frames = None
            self.title = ""
            self._set_icon(static_icon, fb)

        # rebuild submenu: each session -> click to stop
        try:
            self.remotes_item.clear()
        except Exception:
            pass
        if sessions:
            for i, s in enumerate(sessions, 1):
                lbl = s["label"] or "(no label)"
                self.remotes_item.add(
                    rumps.MenuItem(f"🛑 {i}. {s['repo']} — {lbl}",
                                   callback=self._kill_remote(s["id"]))
                )
        else:
            self.remotes_item.add(rumps.MenuItem("(none)"))

    # ---------- toggles ----------
    def toggle_pause(self, _):
        # keep the bot running; just toggle the pause flag (it replies "paused" while paused)
        if os.path.exists(PAUSE_FILE):
            try:
                os.remove(PAUSE_FILE)
            except OSError:
                pass
        else:
            open(PAUSE_FILE, "w").close()
        self.refresh(None)

    def _onboard_once(self, _):
        self.onboard_timer.stop()
        if self._onboarded:
            return
        self._onboarded = True
        self.run_onboarding()

    def run_onboarding(self):
        # 1) stay-awake permission (passwordless pmset)
        if not sudoers_ok():
            if rumps.alert(
                "Stay-awake setup",
                "To keep your Mac awake (even lid-closed) while the bot runs, a one-time "
                "permission is needed.\nSet it up now? (asks for your password once)",
                ok="Set up", cancel="Later",
            ) == 1:
                self.install_sudoers()
        # 2) launch at login
        if not login_item_exists():
            if rumps.alert(
                "Launch at login",
                f"Start {APP_NAME} automatically when you log in?",
                ok="Enable", cancel="Later",
            ) == 1:
                if add_login_item():
                    rumps.alert("Done", "Launch at login enabled.")
                else:
                    rumps.alert("Failed", "Couldn't add the login item. (Allow System Events automation if prompted.)")

    def on_awake_click(self, _):
        if self.sudoers_needed:
            self.install_sudoers()
        else:
            rumps.alert(
                "Stay awake (sleep prevention)",
                "When the bot is running, sleep is prevented so the Mac stays up even "
                "with the lid closed.\n(Unrelated to screen lock — only the system stays awake.)\n"
                "When paused/off it's released automatically. (Follows the bot state.)",
            )

    def install_sudoers(self):
        # native admin auth dialog -> register a one-time passwordless pmset grant
        rule = (f"{USER} ALL=(root) NOPASSWD: /usr/bin/pmset -a disablesleep 0, "
                f"/usr/bin/pmset -a disablesleep 1")
        inner = (
            f"echo '{rule}' > /etc/sudoers.d/claude-remote-bot-pmset && "
            f"chmod 440 /etc/sudoers.d/claude-remote-bot-pmset"
        )
        script = f'do shell script "{inner}" with administrator privileges'
        try:
            r = subprocess.run(["osascript", "-e", script],
                               capture_output=True, text=True, timeout=120)
        except Exception as e:
            rumps.alert("Setup failed", str(e)[:300])
            return
        if r.returncode == 0:
            rumps.alert("Done",
                        "Stay-awake permission registered.\nThe Mac will now stay awake (even lid-closed) "
                        "while the bot runs.\nNo need to set this up again.")
        else:
            rumps.alert("Not set up", (r.stderr or "Cancelled or failed.")[:300])
        self.refresh(None)

    def _kill_remote(self, sid):
        def cb(_):
            sh([TMUX, "kill-session", "-t", sid])
            remove_remote_meta(sid)
            self.refresh(None)
        return cb

    def kill_remotes(self, _):
        r = sh([TMUX, "ls"])
        if r and r.returncode == 0:
            for line in r.stdout.splitlines():
                if line.startswith("rc-"):
                    sh([TMUX, "kill-session", "-t", line.split(":")[0]])
        try:
            open(REMOTES_FILE, "w").write("[]")
        except Exception:
            pass
        self.refresh(None)

    def open_log(self, _):
        sh(["open", LOG])

    def quit_app(self, _):
        set_disablesleep(False)  # release sleep prevention on quit (battery safety)
        rumps.quit_application()


if __name__ == "__main__":
    ensure_single_instance()
    ClaudeBotApp().run()
