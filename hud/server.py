"""JARVIS HUD server: the Iron Man interface's backend.

  pythonw hud\\server.py      (started automatically by the clap / wake-word listener)

- Keeps one Claude Code process warm (stream-json mode) so answers start in ~2-3 s instead of ~12 s.
- Streams the reply to the page and synthesizes speech sentence-by-sentence, so JARVIS starts talking
  while the rest of the answer is still being written.
- Serves live system telemetry, the to-do list, weather and engineering/coding news.
Everything listens on 127.0.0.1 only (this laptop).
"""
import datetime
import json
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import psutil

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "voice"))
import speak  # noqa: E402

PORT = 8765
CLAUDE = os.path.expanduser(r"~\.local\bin\claude.exe")
NO_WINDOW = 0x08000000
TASKS = os.path.join(ROOT, "tasks.md")
NEWS_MD = os.path.join(ROOT, "news.md")
BRAIN_LOG = os.path.join(HERE, "brain.log")
SESSION_IDLE_RESET = 30 * 60
ENV = speak.load_env()
MODEL = ENV.get("JARVIS_MODEL") or "sonnet"     # fast + capable; set JARVIS_MODEL=opus in .env for max smarts
EFFORT = ENV.get("JARVIS_EFFORT") or "low"      # low = quickest answers

state = {"weather": ("", 0.0), "wake": 0, "wake_reason": "", "wake_phrase": "", "busy": False,
         "server_speaking_until": 0.0, "browser_speaking": False}
# Listening is done offline by the background listener (wake/clap.py, Whisper). The page asks for a session here,
# the listener records + transcribes, and posts the text back. (Edge's built-in speech service never returned
# results on this laptop, which is why JARVIS used to sit on "standing by".)
listen = {"id": 0, "active": False, "mode": "", "timeout": 6000, "phase": "", "heard_id": 0, "heard_text": ""}
lock = threading.Lock()


# ======================================================================
# The brain: a persistent Claude Code process
# ======================================================================

# ----------------------------------------------------------------------
# Permissions: JARVIS has full access to the PC, but anything that changes something is put to Dev first,
# and important/irreversible actions need a second, explicit "confirm". Unanswered requests are denied.
# ----------------------------------------------------------------------

SAFE_TOOL = re.compile(r"(^|__)(search|list|get|read|fetch|find|query|view|lookup|count|browser_(navigate|snapshot|"
                       r"take_screenshot|wait_for|tabs|console_messages|network_requests))", re.I)
DANGER_CMD = re.compile(r"\b(Remove-Item|rm|rmdir|rd|del|erase|Clear-(Content|Item|RecycleBin)|Format-Volume|format|"
                        r"diskpart|Stop-Computer|Restart-Computer|shutdown|logoff|reg\s+(add|delete)|Remove-ItemProperty|"
                        r"Set-ItemProperty|New-ItemProperty|Uninstall|winget\s+uninstall|choco\s+uninstall|Stop-Process|"
                        r"taskkill|takeown|icacls|bcdedit|cipher|Set-ExecutionPolicy|Disable-|Move-Item|mv|"
                        r"git\s+(push|reset|clean)|npm\s+publish|pip\s+uninstall)\b", re.I)
DANGER_TOOL = re.compile(r"(delete|remove|trash|send|reply|forward|create_event|update|publish|archive|move|"
                         r"cancel|decline|purchase|pay|deploy|browser_(click|type|fill_form|press_key|file_upload))", re.I)
AUTO_ALLOW = {"Read", "Glob", "Grep", "WebSearch", "WebFetch", "TodoWrite", "Task", "Agent", "Skill", "ToolSearch"}


class Gate:
    def __init__(self):
        self.pending = {}  # id -> {"event": Event, "allow": bool, "info": {...}}

    @staticmethod
    def describe(tool, inp):
        cmd = inp.get("command") or ""
        if tool in ("PowerShell", "Bash"):
            summary = inp.get("description") or f"run a command"
            detail = cmd
            danger = bool(DANGER_CMD.search(cmd))
        elif tool in ("Write", "Edit", "NotebookEdit", "MultiEdit"):
            path = inp.get("file_path") or inp.get("notebook_path") or ""
            summary = f"{'create or overwrite' if tool == 'Write' else 'edit'} {os.path.basename(path)}"
            detail = path
            danger = tool == "Write" and os.path.exists(path)
        else:
            server, _, name = tool.partition("__")[2].partition("__") if tool.startswith("mcp__") else ("", "", tool)
            app = server.replace("claude_ai_", "").replace("_", " ") or "a tool"
            summary = f"use {app}: {name.replace('_', ' ') or tool}"
            detail = json.dumps(inp, ensure_ascii=False)[:400]
            danger = bool(DANGER_TOOL.search(name or tool))
        return {"tool": tool, "summary": summary[:160], "detail": detail[:600], "danger": danger}

    def decide(self, tool, inp, emit):
        if tool in AUTO_ALLOW or SAFE_TOOL.search(tool):
            return {"behavior": "allow", "updatedInput": inp}
        info = self.describe(tool, inp)
        if emit is None:  # nobody to ask (shouldn't happen)
            return {"behavior": "deny", "message": "Dev isn't available to approve this right now."}
        pid = uuid.uuid4().hex[:10]
        slot = {"event": threading.Event(), "allow": False, "info": info}
        self.pending[pid] = slot
        speak.log(f"permission asked: {info['summary']} | {info['detail'][:120]}")
        emit({"t": "permission", "id": pid, **info})
        slot["event"].wait(timeout=150)
        self.pending.pop(pid, None)
        speak.log(f"permission {'GRANTED' if slot['allow'] else 'denied'}: {info['summary']}")
        if slot["allow"]:
            return {"behavior": "allow", "updatedInput": inp}
        return {"behavior": "deny", "message": "Dev did not approve this action. Don't try another way around it; "
                                               "just tell Dev briefly that you didn't do it."}

    def answer(self, pid, allow):
        slot = self.pending.get(pid)
        if slot:
            slot["allow"] = bool(allow)
            slot["event"].set()

    def deny_all(self):
        for slot in list(self.pending.values()):
            slot["allow"] = False
            slot["event"].set()


gate = Gate()


class Brain:
    def __init__(self):
        self.proc = None
        self.lines = None
        self.lock = threading.Lock()   # one conversation turn at a time
        self.wlock = threading.Lock()  # stdin writes (turns, permission answers, interrupts)
        self.ready = threading.Event()
        self.last_used = time.time()
        self.emit = None               # the HUD stream of the turn in progress (for permission prompts)

    def _spawn(self):
        env = dict(os.environ, JARVIS_HUD="1", CLAUDE_CODE_EFFORT_LEVEL=EFFORT)
        errlog = open(BRAIN_LOG, "a", encoding="utf-8")
        self.proc = subprocess.Popen(
            [CLAUDE, "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
             "--include-partial-messages", "--model", MODEL, "--permission-prompt-tool", "stdio"],
            cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errlog, text=True,
            encoding="utf-8", errors="replace", env=env, creationflags=NO_WINDOW)
        self.lines = queue.Queue()
        proc, q = self.proc, self.lines

        def pump():
            for line in proc.stdout:
                q.put(line)
            q.put(None)

        threading.Thread(target=pump, daemon=True).start()

    def _send(self, obj):
        with self.wlock:
            self.proc.stdin.write(json.dumps(obj) + "\n")
            self.proc.stdin.flush()

    def interrupt(self):
        """Dev said "stop": abandon the current answer immediately."""
        gate.deny_all()
        try:
            if self.proc and self.proc.poll() is None:
                self._send({"type": "control_request", "request_id": uuid.uuid4().hex[:8], "request": {"subtype": "interrupt"}})
        except OSError:
            pass

    def _permission(self, ev):
        req = ev.get("request", {})
        tool, inp = req.get("tool_name", ""), req.get("input") or {}
        decision = gate.decide(tool, inp, self.emit)
        self._send({"type": "control_response", "response": {"subtype": "success", "request_id": ev["request_id"],
                                                             "response": decision}})

    def _turn(self, text, on_text=None, timeout=300):
        self._send({"type": "user", "message": {"role": "user", "content": text}})
        full, deadline = [], time.time() + timeout
        while True:
            line = self.lines.get(timeout=max(1, deadline - time.time()))
            if line is None:
                raise RuntimeError("Claude process exited")
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if ev.get("type") == "control_request" and ev.get("request", {}).get("subtype") == "can_use_tool":
                threading.Thread(target=self._permission, args=(ev,), daemon=True).start()
            elif ev.get("type") == "stream_event":
                e = ev.get("event", {})
                if e.get("type") == "content_block_delta" and e.get("delta", {}).get("type") == "text_delta":
                    full.append(e["delta"]["text"])
                    if on_text:
                        on_text(e["delta"]["text"])
                elif e.get("type") == "content_block_start" and full and on_text:
                    on_text("\n")  # separate text written before/after a tool call
            elif ev.get("type") == "result":
                if ev.get("subtype") == "error_during_execution":  # interrupted by Dev
                    return "".join(full), False
                return ev.get("result") or "".join(full), bool(ev.get("is_error"))

    def warm(self):
        """Start a fresh process and pay the ~10 s startup cost now, not when Dev asks something."""
        with self.lock:
            self.ready.clear()
            self.kill()
            try:
                self._spawn()
                self._turn("[System warm-up, not from Dev. Reply with just: OK]", timeout=120)
                self.last_used = time.time()
            except Exception as e:
                speak.log(f"brain warm-up failed: {e!r}")
                self.kill()
            finally:
                self.ready.set()

    def kill(self):
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.kill()
            except OSError:
                pass
        self.proc = None

    def ask(self, text, on_text, emit=None):
        if time.time() - self.last_used > SESSION_IDLE_RESET and self.proc:
            self.warm()  # long silence: start a clean conversation
        self.ready.wait(timeout=150)
        with self.lock:
            self.emit = emit
            for attempt in (1, 2):
                if not self.proc or self.proc.poll() is not None:
                    self._spawn()
                try:
                    reply, err = self._turn(text, on_text)
                    self.last_used = time.time()
                    return reply, err
                except Exception as e:
                    speak.log(f"brain turn failed (attempt {attempt}): {e!r}")
                    self.kill()
            return "I'm afraid my connection dropped, Dev. Try that again.", True


brain = Brain()


# ======================================================================
# Voice: sentence-by-sentence synthesis, played by the page (or here as fallback)
# ======================================================================

CLIP_DIR = os.path.join(tempfile.gettempdir(), "jarvis_clips")
os.makedirs(CLIP_DIR, exist_ok=True)
clips = {}              # id -> mp3 path
play_q = queue.Queue()  # fallback playback on the laptop speakers when the page can't autoplay


def new_clip_from_text(text):
    text = speak.clean_for_speech(text)
    if not text:
        return None
    cid = uuid.uuid4().hex[:12]
    path = os.path.join(CLIP_DIR, cid + ".mp3")
    if not speak.synth_file(text, path):
        return None
    clips[cid] = path
    return cid


def clip_from_cache(text):
    path = speak.cached_clip(speak.clean_for_speech(text))
    if not path:
        return None
    cid = uuid.uuid4().hex[:12]
    clips[cid] = path
    return cid


def local_player():
    while True:
        cid = play_q.get()
        path = clips.get(cid)
        if not path:
            continue
        state["server_speaking_until"] = time.time() + 60
        open(speak.SPEAKING_FLAG, "w").close()
        try:
            speak.play_mp3(path, alias="hud")
        except Exception as e:
            speak.log(f"local playback failed: {e!r}")
        if play_q.empty():
            state["server_speaking_until"] = 0
            update_speaking_flag()


def update_speaking_flag():
    speaking = state["browser_speaking"] or state["server_speaking_until"] > time.time()
    try:
        if speaking:
            open(speak.SPEAKING_FLAG, "w").close()
        elif os.path.exists(speak.SPEAKING_FLAG):
            os.remove(speak.SPEAKING_FLAG)
    except OSError:
        pass


def hush():
    while not play_q.empty():
        try:
            play_q.get_nowait()
        except queue.Empty:
            break
    import ctypes
    ctypes.windll.winmm.mciSendStringW("stop hud", None, 0, 0)
    speak.stop_previous()
    state["server_speaking_until"] = 0
    state["browser_speaking"] = False
    update_speaking_flag()


SENTENCE_END = re.compile(r"(?<=[.!?…])\s+|\n+")


turn = {"n": 0, "cancelled": -1}  # turn counter; "stop" cancels the turn in progress


def cancel_turn():
    turn["cancelled"] = turn["n"]
    brain.interrupt()
    hush()


def stream_answer(text, emit):
    """Ask Claude; emit text deltas immediately and audio clips per sentence as they're ready."""
    turn["n"] += 1
    me = turn["n"]
    live = lambda: turn["cancelled"] != me
    sentences = queue.Queue()
    buf = [""]
    started = [False]
    raw_emit = emit
    emit = lambda obj: live() and raw_emit(obj)

    state["reply_text"] = ""

    def on_text(delta):
        emit({"t": "text", "d": delta})
        state["reply_text"] += delta
        buf[0] += delta
        parts = SENTENCE_END.split(buf[0])
        for s in parts[:-1]:
            if len(s.strip()) > 1:
                sentences.put(s.strip())
                started[0] = True
        buf[0] = parts[-1]
        # Get the voice going fast: speak the opening clause at the first comma/dash instead of waiting.
        if not started[0] and len(buf[0]) > 30:
            cut = max(buf[0].rfind(", "), buf[0].rfind(" — "), buf[0].rfind(" - "))
            if cut > 20:
                sentences.put(buf[0][:cut + 1].strip())
                buf[0] = buf[0][cut + 1:]
                started[0] = True

    def synth_worker():
        pending = ""
        while True:
            s = sentences.get()
            if s is None:
                if pending:
                    cid = new_clip_from_text(pending)
                    if cid:
                        emit({"t": "audio", "id": cid})
                return
            if not live():
                continue  # Dev said stop: drop the rest
            pending = (pending + " " + s).strip()
            if len(pending) < 25 and not sentences.empty() and emitted[0]:
                continue  # glue very short fragments to the next sentence (never delay the first one)
            cid = new_clip_from_text(pending)
            pending = ""
            if cid:
                emitted[0] = True
                emit({"t": "audio", "id": cid})

    emitted = [False]
    worker = threading.Thread(target=synth_worker, daemon=True)
    worker.start()
    state["busy"] = True
    try:
        reply, _ = brain.ask(text, on_text, emit)
    finally:
        state["busy"] = False
    if buf[0].strip():
        sentences.put(buf[0].strip())
    sentences.put(None)
    worker.join(timeout=60)
    raw_emit({"t": "done", "text": reply, "cancelled": not live()})


# ======================================================================
# Telemetry, weather, tasks, news
# ======================================================================

telemetry = {}


def sample_forever():
    net_prev, t_prev = psutil.net_io_counters(), time.time()
    slow_at = 0.0
    psutil.cpu_percent(None)
    while True:
        time.sleep(2)
        now = time.time()
        net = psutil.net_io_counters()
        dt = max(now - t_prev, 0.1)
        vm = psutil.virtual_memory()
        disk = psutil.disk_usage("C:\\")
        batt = psutil.sensors_battery()
        freq = psutil.cpu_freq()
        top = sorted(((p.info["memory_info"].rss, p.info["name"]) for p in psutil.process_iter(["name", "memory_info"])
                      if p.info["memory_info"]), reverse=True)[:3]
        telemetry.update({
            "cpu": psutil.cpu_percent(None), "cpu_ghz": round((freq.current if freq else 0) / 1000, 2),
            "cores": psutil.cpu_count(), "ram_used": vm.used, "ram_total": vm.total, "ram_pct": vm.percent,
            "disk_used": disk.used, "disk_total": disk.total, "disk_pct": disk.percent,
            "down": (net.bytes_recv - net_prev.bytes_recv) / dt, "up": (net.bytes_sent - net_prev.bytes_sent) / dt,
            "battery": round(batt.percent) if batt else None, "plugged": batt.power_plugged if batt else None,
            "batt_secs": batt.secsleft if batt and batt.secsleft > 0 else None,
            "boot": psutil.boot_time(), "procs": len(psutil.pids()),
            "top": [{"name": n.replace(".exe", ""), "mb": round(m / 2**20)} for m, n in top],
        })
        net_prev, t_prev = net, now
        if now >= slow_at:
            slow_at = now + 6
            telemetry.update(gpu_stats())
            telemetry["sys_temp"] = thermal_zone()


def gpu_stats():
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=name,temperature.gpu,utilization.gpu,memory.used,memory.total,power.draw",
                              "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=5,
                             creationflags=NO_WINDOW).stdout.strip().split(", ")
        name, temp, util, mem_used, mem_total, power = out
        num = lambda s: float(s) if s.replace(".", "").isdigit() else None
        return {"gpu_name": name.replace("NVIDIA GeForce ", ""), "gpu_temp": num(temp), "gpu_util": num(util),
                "vram_used": num(mem_used), "vram_total": num(mem_total), "gpu_watts": num(power)}
    except Exception:
        return {}


def thermal_zone():
    try:
        out = subprocess.run(["typeperf", r"\Thermal Zone Information(*)\Temperature", "-sc", "1"],
                             capture_output=True, text=True, timeout=8, creationflags=NO_WINDOW).stdout
        row = [l for l in out.splitlines() if l.startswith('"') and "/" in l][0]
        kelvins = [float(v.strip('"')) for v in row.split(",")[1:] if v.strip('"')]
        return round(max(kelvins) - 273.15, 1)
    except Exception:
        return None


def weather():
    text, fetched = state["weather"]
    if time.time() - fetched > 900:
        try:
            with urllib.request.urlopen("https://wttr.in/?format=%l|%C|%t|%h|%w", timeout=10) as r:
                text = r.read().decode("utf-8").strip()
            state["weather"] = (text, time.time())
        except Exception:
            pass
    return text


def read_tasks():
    try:
        with open(TASKS, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return []
    return [{"line": i, "done": l.lstrip()[3].lower() == "x", "text": l.lstrip()[6:].strip()}
            for i, l in enumerate(lines) if l.lstrip().startswith(("- [ ]", "- [x]", "- [X]"))]


def edit_tasks(action, line=None, text=""):
    with lock:
        try:
            with open(TASKS, encoding="utf-8") as f:
                lines = f.read().splitlines()
        except OSError:
            lines = ["# Tasks", ""]
        if action == "add" and text.strip():
            lines.append(f"- [ ] {text.strip()[:200]}")
        elif action in ("toggle", "delete") and isinstance(line, int) and 0 <= line < len(lines):
            l = lines[line]
            if l.lstrip().startswith("- ["):
                if action == "delete":
                    del lines[line]
                else:
                    lines[line] = l.replace("- [ ]", "- [x]", 1) if "- [ ]" in l else l.replace("- [x]", "- [ ]", 1).replace("- [X]", "- [ ]", 1)
        with open(TASKS, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")


news = {"code": [], "repos": [], "wire": [], "topics": [], "updated": 0}
UA = {"User-Agent": "JARVIS-HUD/1.0 (personal dashboard)"}
WIRE_FEEDS = {
    "TechCrunch": "https://techcrunch.com/feed/",
    "The Verge": "https://www.theverge.com/rss/index.xml",
    "Ars Technica": "https://feeds.arstechnica.com/arstechnica/index",
    "IEEE Spectrum": "https://spectrum.ieee.org/feeds/feed.rss",
}


def fetch(url, as_json=True):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=15) as r:
        data = r.read()
    return json.loads(data) if as_json else data


def parse_feed(source, data, limit=6):
    root = ET.fromstring(data)
    out = []
    for item in [e for e in root.iter() if e.tag.split("}")[-1] in ("item", "entry")][:limit]:
        title = link = ""
        for c in item:
            tag = c.tag.split("}")[-1]
            if tag == "title":
                title = (c.text or "").strip()
            elif tag == "link":
                link = c.get("href") or (c.text or "").strip()
        if title:
            out.append({"title": title, "url": link, "source": source})
    return out


def refresh_news():
    fresh = {}
    try:
        hits = fetch("https://hn.algolia.com/api/v1/search?tags=front_page&hitsPerPage=15")["hits"]
        fresh["code"] = [{"title": h["title"], "url": h.get("url") or f"https://news.ycombinator.com/item?id={h['objectID']}",
                          "source": "Hacker News", "score": h.get("points", 0)} for h in hits]
    except Exception as e:
        speak.log(f"news HN failed: {e!r}")
    try:
        arts = fetch("https://dev.to/api/articles?top=1&per_page=10")
        fresh["dev"] = [{"title": a["title"], "url": a["url"], "source": "DEV", "tags": a.get("tag_list", [])} for a in arts]
    except Exception as e:
        speak.log(f"news dev.to failed: {e!r}")
    try:
        week = (datetime.date.today() - datetime.timedelta(days=7)).isoformat()
        items = fetch(f"https://api.github.com/search/repositories?q=created:%3E{week}&sort=stars&order=desc&per_page=10")["items"]
        fresh["repos"] = [{"title": r["full_name"], "desc": (r.get("description") or "")[:120], "url": r["html_url"],
                           "stars": r["stargazers_count"], "lang": r.get("language") or "", "topics": r.get("topics", [])}
                          for r in items]
    except Exception as e:
        speak.log(f"news GitHub failed: {e!r}")
    wire = []
    for source, url in WIRE_FEEDS.items():
        try:
            wire.append(parse_feed(source, fetch(url, as_json=False)))
        except Exception as e:
            speak.log(f"news {source} failed: {e!r}")
    if wire:  # interleave sources so no single outlet dominates
        fresh["wire"] = [x for group in zip(*[w + [None] * (6 - len(w)) for w in wire]) for x in group if x]

    # Hot topics: languages of trending repos + DEV tags + repo topics
    counts = {}
    for r in fresh.get("repos", []):
        for t in ([r["lang"]] if r["lang"] else []) + r["topics"][:3]:
            counts[t.lower()] = counts.get(t.lower(), 0) + 1
    for a in fresh.get("dev", []):
        for t in a["tags"]:
            counts[t.lower()] = counts.get(t.lower(), 0) + 1
    fresh["topics"] = [t for t, _ in sorted(counts.items(), key=lambda kv: -kv[1])[:12]]
    fresh["code"] = (fresh.get("code") or news["code"])[:10] + [
        {"title": a["title"], "url": a["url"], "source": "DEV"} for a in fresh.get("dev", [])[:5]]
    for k in ("repos", "wire", "topics"):
        if not fresh.get(k):
            fresh[k] = news[k]
    fresh["updated"] = time.time()
    news.update(fresh)
    write_news_md()


def write_news_md():
    t = datetime.datetime.fromtimestamp(news["updated"]).strftime("%d %b %Y %I:%M %p")
    lines = [f"# Latest tech headlines (auto-updated every 15 min — last {t})", "",
             "## International tech & industry news"]
    lines += [f"- {n['title']} — {n['source']}" for n in news["wire"][:16]]
    lines += ["", "## Trending in coding (Hacker News / DEV)"]
    lines += [f"- {n['title']} — {n['source']}" for n in news["code"][:12]]
    lines += ["", "## Hottest new GitHub repos this week"]
    lines += [f"- {r['title']} ({r['lang'] or 'n/a'}, ★{r['stars']}) — {r['desc']}" for r in news["repos"][:8]]
    lines += ["", "## Hot topics: " + ", ".join(news["topics"])]
    with open(NEWS_MD, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def news_forever():
    while True:
        try:
            refresh_news()
        except Exception as e:
            speak.log(f"news refresh failed: {e!r}")
        time.sleep(15 * 60)


# ======================================================================
# HTTP
# ======================================================================

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            with open(os.path.join(HERE, "index.html"), "rb") as f:
                self.send(200, f.read(), "text/html; charset=utf-8")
        elif path in ("/preview3d", "/preview3d.html"):
            with open(os.path.join(HERE, "preview3d.html"), "rb") as f:
                self.send(200, f.read(), "text/html; charset=utf-8")
        elif path == "/core3d.js":
            with open(os.path.join(HERE, "core3d.js"), "rb") as f:
                self.send(200, f.read(), "text/javascript; charset=utf-8")
        elif path == "/ping":
            self.send(200, {"ok": True, "brain_ready": brain.ready.is_set()})
        elif path == "/status":
            speaking = os.path.exists(speak.SPEAKING_FLAG) and time.time() - os.path.getmtime(speak.SPEAKING_FLAG) < 180
            self.send(200, {"speaking": speaking, "busy": state["busy"], "wake": state["wake"],
                            "wake_reason": state["wake_reason"], "wake_phrase": state["wake_phrase"],
                            "brain_ready": brain.ready.is_set(), "listen": listen})
        elif path == "/weather":
            self.send(200, {"weather": weather()})
        elif path == "/sysinfo":
            self.send(200, telemetry)
        elif path == "/tasks":
            self.send(200, {"tasks": read_tasks()})
        elif path == "/news":
            self.send(200, news)
        elif path.startswith("/audio/"):
            p = clips.get(path[7:])
            if p and os.path.exists(p):
                with open(p, "rb") as f:
                    self.send(200, f.read(), "audio/mpeg")
            else:
                self.send(404, {"error": "no clip"})
        else:
            self.send(404, {"error": "not found"})

    def do_POST(self):
        origin = self.headers.get("Origin", "")
        if origin and origin not in (f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"):
            return self.send(403, {"error": "forbidden"})
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            body = {}

        if self.path == "/ask":
            text = str(body.get("text", "")).strip()[:4000]
            if not text:
                return self.send(400, {"error": "empty"})
            stamp = datetime.datetime.now().strftime("%A %d %B %Y, %I:%M %p")
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            out_lock = threading.Lock()

            def emit(obj):
                with out_lock:
                    try:
                        self.wfile.write((json.dumps(obj) + "\n").encode())
                        self.wfile.flush()
                    except OSError:
                        pass

            stream_answer(f"[HUD · {stamp}] {text}", emit)
        elif self.path == "/say":  # speak a line without Claude: fixed lines are cached, one-offs synthesized
            text = str(body.get("text", ""))[:300]
            self.send(200, {"id": clip_from_cache(text) if body.get("cache", True) else new_clip_from_text(text)})
        elif self.path == "/cancel":  # Dev said "stop"
            cancel_turn()
            self.send(200, {"ok": True})
        elif self.path == "/bargein":  # the offline listener heard "stop" while JARVIS was busy/talking
            word = str(body.get("word", "")).lower()
            said = state.get("reply_text", "").lower()
            if word and word.split()[-1] in said:  # JARVIS is saying that word itself: probably its own echo
                speak.log(f"barge-in ignored (JARVIS said '{word}' itself)")
                return self.send(200, {"ok": False})
            speak.log(f"barge-in: '{word}'")
            cancel_turn()
            state["wake"] += 1
            state["wake_reason"], state["wake_phrase"] = "stop", word
            self.send(200, {"ok": True})
        elif self.path == "/listen":  # page: "record Dev now" (mode: command / permission)
            listen.update(id=listen["id"] + 1, active=True, mode=str(body.get("mode", "command")),
                          timeout=int(body.get("timeoutMs") or 8000), phase="waiting")
            self.send(200, {"id": listen["id"]})
        elif self.path == "/listen/stop":  # page: never mind
            if body.get("id") in (None, listen["id"]):
                listen.update(active=False, phase="")
            self.send(200, {"ok": True})
        elif self.path == "/listen/phase":  # listener: recording / transcribing
            if body.get("id") == listen["id"] and listen["active"]:
                listen["phase"] = str(body.get("phase", ""))
            self.send(200, {"ok": True})
        elif self.path == "/heard":  # listener: here's what Dev said ("" = nothing)
            if body.get("id") == listen["id"]:
                listen.update(active=False, phase="", heard_id=listen["id"], heard_text=str(body.get("text", ""))[:2000])
                if body.get("text"):
                    speak.log(f"heard: {body['text'][:120]}")
            self.send(200, {"ok": True})
        elif self.path == "/log":  # diagnostics from the page (speech-recognition failures etc.)
            speak.log("HUD: " + str(body.get("msg", ""))[:300])
            self.send(200, {"ok": True})
        elif self.path == "/permission":
            gate.answer(str(body.get("id", "")), bool(body.get("allow")))
            self.send(200, {"ok": True})
        elif self.path == "/play":  # page couldn't autoplay: play on the laptop speakers instead
            if body.get("id") in clips:
                play_q.put(body["id"])
            self.send(200, {"ok": True})
        elif self.path == "/speaking":  # page reports its own playback, so the clap listener ignores JARVIS
            state["browser_speaking"] = bool(body.get("on"))
            update_speaking_flag()
            self.send(200, {"ok": True})
        elif self.path == "/hush":
            hush()
            self.send(200, {"ok": True})
        elif self.path == "/wake":
            state["wake"] += 1
            state["wake_reason"] = str(body.get("reason", "clap"))
            state["wake_phrase"] = str(body.get("phrase", ""))[:200]
            self.send(200, {"ok": True})
        elif self.path == "/reset":
            threading.Thread(target=brain.warm, daemon=True).start()
            self.send(200, {"ok": True})
        elif self.path == "/tasks":
            line = body.get("line")
            edit_tasks(str(body.get("action", "")), line if isinstance(line, int) else None, str(body.get("text", "")))
            self.send(200, {"tasks": read_tasks()})
        else:
            self.send(404, {"error": "not found"})


def prewarm_lines():
    for line in ("Yes, Dev?", "At your service, Dev.", "Stopped. I'm listening.", "Understood, I won't do it.",
                 "This one is important. Say confirm to go ahead, or cancel.", "Going ahead.", "Cancelled.",
                 "Goodnight, Dev.", "Welcome home, Dev. Pulling up your day now.",
                 "Good morning, Dev. Pulling up your day now.", "Very good, Dev. I'll be listening if you need me.",
                 "One moment, Dev."):
        try:
            speak.cached_clip(speak.clean_for_speech(line))
        except Exception:
            pass


if __name__ == "__main__":
    for target in (sample_forever, news_forever, local_player, brain.warm, prewarm_lines):
        threading.Thread(target=target, daemon=True).start()
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
