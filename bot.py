#!/usr/bin/env python3
"""
Claude Code Telegram bot (headless `claude -p` bridge).

- Only processes messages from allowed chat IDs.
- Runs in a fixed working directory (WORKDIR).
- Keeps memory by resuming the session (--resume).
- Shows a short session id + start/last time on each run.
- When Claude needs the user to choose options, it prints an ```ask-options```
  JSON block and the bot renders inline buttons (single / multi select),
  then feeds the choice back into the same session.
- /remote launches an interactive remote-control session (driven from the
  Claude mobile/web app) inside tmux, with parallel sessions supported.
"""
import os
import re
import json
import shlex
import asyncio
from pathlib import Path
from datetime import datetime

from dotenv import load_dotenv
from telegram import (
    Update,
    constants,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    MessageHandler,
    CommandHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)

BASE = Path(__file__).resolve().parent
load_dotenv(BASE / ".env")

# ───────────────────────── config ─────────────────────────
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
ALLOWED_CHAT_IDS = {
    int(x) for x in os.getenv("ALLOWED_CHAT_IDS", "").replace(" ", "").split(",") if x
}
WORKDIR = os.path.expanduser(os.getenv("WORKDIR", "~/Developer"))
CLAUDE_BIN = os.path.expanduser(os.getenv("CLAUDE_BIN", "~/.local/bin/claude"))
PERMISSION_MODE = os.getenv("PERMISSION_MODE", "acceptEdits")
ALLOWED_TOOLS = os.getenv(
    "ALLOWED_TOOLS",
    "Read,Edit,Write,Glob,Grep,TodoWrite,Task,"
    "Bash(git:*),Bash(make:*),Bash(go:*),Bash(npm:*),Bash(npx:*),Bash(node:*),"
    "Bash(./gradlew:*),Bash(docker:*),Bash(docker compose:*),Bash(ls:*),Bash(cat:*),"
    "Bash(pwd),Bash(cd:*),Bash(echo:*),Bash(grep:*),Bash(find:*),Bash(head:*),Bash(tail:*),"
    "WebSearch,WebFetch",
)
TIMEOUT_SEC = int(os.getenv("TIMEOUT_SEC", "1800"))
PATH_PREFIX = os.getenv(
    "PATH_PREFIX",
    "/opt/homebrew/bin:/opt/homebrew/opt/node@20/bin:/opt/homebrew/opt/libpq/bin",
)

# Rules injected into Claude so it works well over a non-interactive bridge.
APPEND_SYS = (
    "You are operating over a Telegram bridge in non-interactive (headless) mode. "
    "The AskUserQuestion tool is NOT available. "
    "When the user must choose among options, output exactly one fenced code block "
    "in this format and end your turn: "
    "```ask-options\\n{\"question\": \"...\", \"multiSelect\": true_or_false, \"options\": [\"A\",\"B\"]}\\n``` "
    "Write nothing after that block. The bridge shows buttons and sends the choice "
    "back as the next message. "
    "If you need free-form clarification, just ask in plain text and end your turn. "
    "Keep responses concise for a mobile chat."
)

SESS_FILE = BASE / "sessions.json"
PAUSE_FILE = BASE / "paused"  # exists => paused (commands ignored, just a notice)
TG_LIMIT = 4000

ASK_RE = re.compile(r"```ask-options\s*(\{.*?\})\s*```", re.DOTALL)

_locks: dict[int, asyncio.Lock] = {}
PENDING: dict[int, dict] = {}  # chat_id -> {question, options, multi, selected:set}


def _lock(chat_id: int) -> asyncio.Lock:
    if chat_id not in _locks:
        _locks[chat_id] = asyncio.Lock()
    return _locks[chat_id]


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def fmt_ts(iso: str | None) -> str:
    if not iso:
        return "?"
    try:
        return datetime.fromisoformat(iso).strftime("%m-%d %H:%M")
    except Exception:
        return "?"


def short_sid(sid: str | None) -> str:
    return sid.split("-")[0][:8] if sid else "?"


def load_sessions() -> dict:
    try:
        data = json.loads(SESS_FILE.read_text())
    except Exception:
        return {}
    # migrate old (str) format
    out = {}
    for k, v in data.items():
        if isinstance(v, str):
            out[k] = {"sid": v, "created": None, "last": None}
        else:
            out[k] = v
    return out


def save_sessions(d: dict) -> None:
    SESS_FILE.write_text(json.dumps(d, ensure_ascii=False))


def authorized(chat_id: int) -> bool:
    return chat_id in ALLOWED_CHAT_IDS


async def send(context, chat_id, text):
    if not text:
        return
    for i in range(0, len(text), TG_LIMIT):
        try:
            await context.bot.send_message(chat_id, text[i : i + TG_LIMIT])
        except Exception:
            pass


async def run_claude(prompt: str, session_id: str | None, on_event):
    cmd = [
        CLAUDE_BIN, "-p", prompt,
        "--output-format", "stream-json", "--verbose",
        "--permission-mode", PERMISSION_MODE,
        "--allowedTools", ALLOWED_TOOLS,
        "--append-system-prompt", APPEND_SYS,
    ]
    if session_id:
        cmd += ["--resume", session_id]

    env = dict(os.environ)
    env["PATH"] = PATH_PREFIX + ":" + env.get("PATH", "")
    print(f"[run] resume={session_id} prompt={prompt[:60]!r}", flush=True)

    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=WORKDIR,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )

    new_sid = session_id
    final = None
    try:
        while True:
            line = await asyncio.wait_for(proc.stdout.readline(), timeout=TIMEOUT_SEC)
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except Exception:
                continue
            t = ev.get("type")
            if t == "system" and ev.get("session_id"):
                new_sid = ev["session_id"]
            elif t == "assistant":
                for b in ev.get("message", {}).get("content", []):
                    if b.get("type") == "tool_use":
                        await on_event("tool", b.get("name"), b.get("input"))
                    elif b.get("type") == "text" and b.get("text", "").strip():
                        await on_event("text", None, b["text"])
            elif t == "result":
                new_sid = ev.get("session_id", new_sid)
                final = ev.get("result")
        await proc.wait()
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        await on_event("error", None, f"⏱️ Stopped after {TIMEOUT_SEC}s timeout")

    err = b""
    try:
        err = await proc.stderr.read()
    except Exception:
        pass
    return new_sid, final, err.decode(errors="ignore")


def build_kb(options, multi, selected):
    rows = []
    for i, opt in enumerate(options):
        mark = "✅ " if (multi and i in selected) else ""
        label = f"{mark}{opt}"[:60]
        rows.append([InlineKeyboardButton(label, callback_data=("tog" if multi else "opt") + f"|{i}")])
    if multi:
        rows.append([InlineKeyboardButton("➡️ Submit", callback_data="submit")])
    return InlineKeyboardMarkup(rows)


def parse_ask(text):
    if not text:
        return None
    m = ASK_RE.search(text)
    if not m:
        return None
    try:
        d = json.loads(m.group(1))
        if isinstance(d.get("options"), list) and d["options"]:
            return d
    except Exception:
        pass
    return None


async def process_prompt(chat_id, prompt, context):
    """Run a prompt on the chat's session; stream progress; render buttons if it asks options."""
    lock = _lock(chat_id)
    if lock.locked():
        await context.bot.send_message(chat_id, "⏳ Previous task still running. Send again when it finishes.")
        return

    async with lock:
        sessions = load_sessions()
        meta = sessions.get(str(chat_id), {})
        sid = meta.get("sid")

        await context.bot.send_chat_action(chat_id, constants.ChatAction.TYPING)
        if sid:
            await send(context, chat_id,
                       f"🟢 Continuing  🧵#{short_sid(sid)} · started {fmt_ts(meta.get('created'))} · last {fmt_ts(meta.get('last'))}")
        else:
            await send(context, chat_id, "🟢 New session")

        last_text = {"v": ""}

        async def on_event(kind, name, payload):
            if kind == "tool":
                if name in ("Task", "Agent"):
                    desc = ""
                    if isinstance(payload, dict):
                        desc = payload.get("description") or payload.get("prompt") or ""
                    await send(context, chat_id, f"🤖 subagent ▶ {desc[:200]}")
                else:
                    summary = json.dumps(payload, ensure_ascii=False) if payload else ""
                    await send(context, chat_id, f"🔧 {name} {summary[:280]}")
                await context.bot.send_chat_action(chat_id, constants.ChatAction.TYPING)
            elif kind == "text":
                last_text["v"] = payload
            elif kind == "error":
                await send(context, chat_id, payload)

        new_sid, final, err = await run_claude(prompt, sid, on_event)

        # update metadata
        if new_sid:
            m = sessions.get(str(chat_id), {})
            if not m.get("created") or m.get("sid") != new_sid:
                m["created"] = m.get("created") or now_iso()
            if m.get("sid") and m.get("sid") != new_sid:
                m["created"] = now_iso()
            m["sid"] = new_sid
            m["last"] = now_iso()
            sessions[str(chat_id)] = m
            save_sessions(sessions)

        tail = f"\n\n🧵#{short_sid(new_sid)} · last {fmt_ts(now_iso())}"

        # options question -> buttons
        ask = parse_ask(final) or parse_ask(last_text["v"])
        if ask:
            multi = bool(ask.get("multiSelect"))
            PENDING[chat_id] = {
                "question": ask.get("question", "Choose"),
                "options": ask["options"],
                "multi": multi,
                "selected": set(),
            }
            q = ask.get("question", "Choose")
            hint = " (select one or more, then ➡️ Submit)" if multi else " (pick one)"
            await context.bot.send_message(
                chat_id, f"❓ {q}{hint}{tail}",
                reply_markup=build_kb(ask["options"], multi, set()),
            )
            return

        if final and final.strip():
            await send(context, chat_id, "✅ " + final + tail)
        elif err.strip():
            await send(context, chat_id, "❌ Error:\n" + err[:1500])
        else:
            await send(context, chat_id, "✅ Done (no output)" + tail)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not ALLOWED_CHAT_IDS:
        await update.message.reply_text(
            f"⚠️ ALLOWED_CHAT_IDS is empty.\nYour chat_id: {chat_id}\nAdd it to .env and restart."
        )
        return
    if not authorized(chat_id):
        await update.message.reply_text(f"⛔️ Not authorized (chat_id={chat_id})")
        return
    if PAUSE_FILE.exists():
        await update.message.reply_text(
            "⏸️ Bot is paused — not accepting commands.\nSend /resume or use the menu bar 🤖 to resume."
        )
        return
    text = (update.message.text or "").strip()
    if text:
        await process_prompt(chat_id, text, context)


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    chat_id = q.message.chat_id
    if not authorized(chat_id):
        await q.answer("Not authorized")
        return
    data = q.data or ""

    # remote-session kill button (independent of options questions)
    if data.startswith("kill|"):
        sid = data.split("|", 1)[1]
        await q.answer("Stopping…")
        await kill_one(sid)
        await q.edit_message_text(f"🛑 Remote session stopped: {sid}")
        return

    pend = PENDING.get(chat_id)
    if not pend:
        await q.answer("This question expired")
        return

    if data.startswith("opt|"):
        idx = int(data.split("|")[1])
        choice = pend["options"][idx]
        await q.answer()
        await q.edit_message_text(f"🔘 Selected: {choice}")
        PENDING.pop(chat_id, None)
        await process_prompt(chat_id, f"[user selected] {choice}", context)

    elif data.startswith("tog|"):
        idx = int(data.split("|")[1])
        sel = pend["selected"]
        sel.discard(idx) if idx in sel else sel.add(idx)
        await q.answer()
        try:
            await q.edit_message_reply_markup(build_kb(pend["options"], True, sel))
        except Exception:
            pass

    elif data == "submit":
        sel = pend["selected"]
        if not sel:
            await q.answer("Select at least one", show_alert=True)
            return
        chosen = [pend["options"][i] for i in sorted(sel)]
        await q.answer()
        await q.edit_message_text("🔘 Selected: " + ", ".join(chosen))
        PENDING.pop(chat_id, None)
        await process_prompt(chat_id, "[user selected] " + ", ".join(chosen), context)


async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not authorized(chat_id):
        return
    sessions = load_sessions()
    sessions.pop(str(chat_id), None)
    save_sessions(sessions)
    PENDING.pop(chat_id, None)
    await update.message.reply_text("🆕 Session reset. The next message starts a fresh session.")


async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not authorized(chat_id):
        return
    PAUSE_FILE.touch()
    await update.message.reply_text("⏸️ Paused — not accepting commands. Send /resume to resume.")


async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not authorized(chat_id):
        return
    try:
        PAUSE_FILE.unlink()
    except FileNotFoundError:
        pass
    await update.message.reply_text("▶️ Resumed — accepting commands again.")


async def cmd_session(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not authorized(chat_id):
        return
    meta = load_sessions().get(str(chat_id))
    if not meta or not meta.get("sid"):
        await update.message.reply_text("No session (next message starts a new one)")
        return
    await update.message.reply_text(
        f"🧵 session id: {meta['sid']}\n"
        f"started: {fmt_ts(meta.get('created'))}\n"
        f"last: {fmt_ts(meta.get('last'))}\n"
        f"WORKDIR: {WORKDIR}"
    )


async def cmd_whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    ok = "✅ authorized" if authorized(chat_id) else "⛔️ not authorized"
    await update.message.reply_text(f"chat_id: {chat_id}\n{ok}\nWORKDIR: {WORKDIR}")


HELP_TEXT = (
    "Claude Code Telegram bot\n\n"
    "• Send any message → Claude works on it (session is remembered)\n"
    "• Option questions are answered with buttons\n\n"
    "Commands:\n"
    "• /remote [repo] [auto|full] [label…] — start a remote-control session\n"
    "    (parallel sessions on the same repo are OK)\n"
    "    e.g. /remote my-repo auto fix login bug\n"
    "• /remotes — list sessions + tap a button to stop one (label shows the summary)\n"
    "• /killremote <number|name> — stop a specific session\n"
    "• /pause  /resume — stop / resume accepting commands\n"
    "• /session  /new — show / reset the conversation session\n"
    "• /whoami — show your chat_id and access\n"
    "• /how  /help — this message\n\n"
    f"Scope: {WORKDIR} · permission: {PERMISSION_MODE}"
)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT)


DEV_ROOT = os.path.expanduser(os.getenv("DEV_ROOT", "~/Developer"))
# remote-control permission mode: acceptEdits (auto-accept edits) | auto | bypassPermissions (no checks)
REMOTE_PERMISSION_MODE = os.getenv("REMOTE_PERMISSION_MODE", "acceptEdits")
CLAUDE_JSON = os.path.expanduser("~/.claude.json")


def ensure_trusted(path: str) -> bool:
    """Pre-register workspace trust for a folder so remote sessions don't block on the trust dialog."""
    try:
        with open(CLAUDE_JSON) as f:
            d = json.load(f)
    except Exception:
        return False
    proj = d.setdefault("projects", {}).setdefault(path, {})
    if proj.get("hasTrustDialogAccepted") is True:
        return True
    proj["hasTrustDialogAccepted"] = True
    proj.setdefault("projectOnboardingSeenCount", 0)
    try:
        tmp = CLAUDE_JSON + ".tmp"
        with open(tmp, "w") as f:
            json.dump(d, f)
        os.replace(tmp, CLAUDE_JSON)
        return True
    except Exception:
        return False


TMUX_BIN = os.getenv("TMUX_BIN", "/opt/homebrew/bin/tmux")
REMOTES_FILE = BASE / "remotes.json"


def load_remotes():
    try:
        return json.loads(REMOTES_FILE.read_text())
    except Exception:
        return []


def save_remotes(lst):
    REMOTES_FILE.write_text(json.dumps(lst, ensure_ascii=False))


async def tmux_alive():
    """Set of live rc-* tmux session ids."""
    p = await asyncio.create_subprocess_exec(
        TMUX_BIN, "ls", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
    )
    out, _ = await p.communicate()
    return {l.split(":")[0] for l in out.decode(errors="ignore").splitlines() if l.startswith("rc-")}


async def synced_remotes():
    """Reconcile metadata with live tmux sessions and return the list."""
    alive = await tmux_alive()
    lst = [r for r in load_remotes() if r.get("id") in alive]
    known = {r["id"] for r in lst}
    for sid in sorted(alive):
        if sid not in known:  # include live sessions even without metadata
            lst.append({"id": sid, "repo": sid, "label": "", "mode": "?", "name": sid})
    save_remotes(lst)
    return lst


async def kill_one(sid: str):
    p = await asyncio.create_subprocess_exec(
        TMUX_BIN, "kill-session", "-t", sid,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    await p.wait()
    save_remotes([r for r in load_remotes() if r.get("id") != sid])


async def launch_remote(path: str, repo: str, label: str, perm: str):
    """Run claude remote-control inside tmux. Same repo can run in parallel (unique id). Returns (ok, sid, disp/err)."""
    base = "rc-" + (re.sub(r"[^A-Za-z0-9_-]", "_", repo)[:28] or "root")
    alive = await tmux_alive()
    n = 1
    while f"{base}-{n}" in alive:
        n += 1
    sid = f"{base}-{n}"
    disp = label if label else (repo if n == 1 else f"{repo} #{n}")

    inner = (
        f"cd {shlex.quote(path)} && "
        f"export PATH={shlex.quote(PATH_PREFIX)}:$PATH && "
        f"{shlex.quote(CLAUDE_BIN)} --remote-control {shlex.quote(disp)} "
        f"--permission-mode {shlex.quote(perm)}"
    )
    p = await asyncio.create_subprocess_exec(
        TMUX_BIN, "new-session", "-d", "-s", sid, "-x", "220", "-y", "50", inner,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    _, err = await p.communicate()
    if p.returncode != 0:
        return False, None, err.decode(errors="ignore")[:400]
    lst = load_remotes()
    lst.append({"id": sid, "repo": repo, "label": label, "mode": perm,
                "name": disp, "started": now_iso()})
    save_remotes(lst)
    return True, sid, disp


_ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")
_URL = re.compile(r"https://\S*(?:claude\.ai|claude\.com)\S*")


async def capture_remote_url(sess: str, tries: int = 15, delay: float = 1.0):
    """Scrape the claude remote-control session URL from the tmux pane."""
    for _ in range(tries):
        await asyncio.sleep(delay)
        p = await asyncio.create_subprocess_exec(
            TMUX_BIN, "capture-pane", "-p", "-t", sess,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await p.communicate()
        text = _ANSI.sub("", out.decode(errors="ignore"))
        m = _URL.search(text)
        if m:
            return m.group(0).rstrip(".,)]}'\"")
    return None


async def cmd_remote(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not authorized(chat_id):
        return
    if PAUSE_FILE.exists():
        await update.message.reply_text("⏸️ Paused — send /resume first.")
        return
    args = list(context.args or [])
    # extract mode (auto / full anywhere in args)
    perm = REMOTE_PERMISSION_MODE
    low = [a.lower() for a in args]
    if "full" in low or "bypass" in low:
        perm = "bypassPermissions"
        args = [a for a in args if a.lower() not in ("full", "bypass")]
    elif "auto" in low:
        perm = "auto"
        args = [a for a in args if a.lower() != "auto"]
    # first token = repo if it's a folder; otherwise root + whole thing = label
    org_repo = os.path.basename(DEV_ROOT.rstrip("/")) or "root"
    if args and args[0][0] in "/~":
        path = os.path.expanduser(args[0])
        if not os.path.isdir(path):
            await update.message.reply_text(f"No such path: {args[0]}")
            return
        repo = os.path.basename(path.rstrip("/"))
        label = " ".join(args[1:]).strip()
    elif args:
        cand = None
        direct = os.path.join(DEV_ROOT, args[0])
        if os.path.isdir(direct):
            cand = direct
        else:
            for pat in ("*", "*/*"):
                hits = [str(p) for p in Path(DEV_ROOT).glob(pat) if p.is_dir() and p.name == args[0]]
                if hits:
                    cand = hits[0]
                    break
        if cand:  # first token is a repo
            path = cand
            repo = os.path.basename(cand.rstrip("/"))
            label = " ".join(args[1:]).strip()
        else:  # not a repo → root + everything as label
            path = DEV_ROOT
            repo = org_repo
            label = " ".join(args).strip()
    else:
        path, repo, label = DEV_ROOT, org_repo, ""

    ensure_trusted(path)
    ok, sid, info = await launch_remote(path, repo, label, perm)
    if not ok:
        await update.message.reply_text(f"❌ Failed to start:\n{info}")
        return

    await update.message.reply_text(
        f"🖥️ Remote control started (mode: {perm})\n"
        f"📂 {repo}" + (f" — {label}" if label else "") + f"\n🧩 {info}\nGrabbing session URL…"
    )
    url = await capture_remote_url(sid)
    if url:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("📱 Open in Claude app", url=url)]])
        await update.message.reply_text(
            "Tap below → opens in the Claude app (or web). Works on iOS & Android.\nList / stop: /remotes",
            reply_markup=kb,
        )
    else:
        await update.message.reply_text(
            f"⚠️ Couldn't grab the URL. Find '{info}' in the Claude app session list.\nList / stop: /remotes"
        )


async def cmd_remotes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not authorized(chat_id):
        return
    lst = await synced_remotes()
    if not lst:
        await update.message.reply_text("No remote sessions running")
        return
    text = "🖥️ Remote sessions (tap a button to stop):\n"
    rows = []
    for i, r in enumerate(lst, 1):
        summary = r.get("label") or "(no label)"
        text += f"{i}. {r['repo']} — {summary}  [{r.get('mode', '?')}]\n"
        label_btn = f"🛑 {i}. {r['repo']}: {summary}"[:62]
        rows.append([InlineKeyboardButton(label_btn, callback_data=f"kill|{r['id']}")])
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(rows))


async def cmd_killremote(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not authorized(chat_id):
        return
    arg = " ".join(context.args or []).strip()
    lst = await synced_remotes()
    if not arg:
        await update.message.reply_text("Usage: /killremote <number or name>  (see /remotes)")
        return
    target = None
    if arg.isdigit():
        i = int(arg) - 1
        if 0 <= i < len(lst):
            target = lst[i]["id"]
    else:
        for r in lst:
            if arg == r["id"] or arg.lower() in (r.get("name") or "").lower() \
               or arg.lower() in (r.get("repo") or "").lower():
                target = r["id"]
                break
    if not target:
        await update.message.reply_text(f"No session matching '{arg}'. Check /remotes.")
        return
    await kill_one(target)
    await update.message.reply_text(f"🛑 Remote session stopped: {target}")


def main():
    if not BOT_TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set in .env")
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())
    ensure_trusted(WORKDIR)  # so headless runs don't block on the trust prompt
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("pause", cmd_pause))
    app.add_handler(CommandHandler("resume", cmd_resume))
    app.add_handler(CommandHandler("session", cmd_session))
    app.add_handler(CommandHandler("whoami", cmd_whoami))
    app.add_handler(CommandHandler("remote", cmd_remote))
    app.add_handler(CommandHandler("remotes", cmd_remotes))
    app.add_handler(CommandHandler("killremote", cmd_killremote))
    app.add_handler(CommandHandler(["help", "how", "start"], cmd_help))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print(f"bot started. WORKDIR={WORKDIR} allowed={ALLOWED_CHAT_IDS or '(unset)'}", flush=True)
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
