"""JARVIS voice: speaks Claude Code's replies out loud.

Used three ways:
  python speak.py                 Stop-hook mode: reads hook JSON on stdin, speaks the last reply
  python speak.py --text "Hi"     Speak a line directly (for testing)
  python speak.py --say <file>    Internal: background worker that synthesizes and plays

Voice engines, tried in order:
  1. ElevenLabs  (only if ELEVENLABS_API_KEY is set in ../.env)
  2. edge-tts    (free Microsoft neural voices, no key)
  3. Windows built-in speech (always available, offline)

Create an empty file named "mute" in the jarvis folder to silence JARVIS.
"""
import asyncio
import ctypes
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PID_FILE = os.path.join(HERE, ".speaking.pid")
SPEAKING_FLAG = os.path.join(HERE, ".speaking")
REFUSED_FILE = os.path.join(HERE, ".refused_voice")
CACHE_DIR = os.path.join(HERE, "cache")
LOG_FILE = os.path.join(HERE, "speak.log")
MUTE_FILE = os.path.join(ROOT, "mute")

MAX_CHARS = 700  # keeps ElevenLabs credit use and listening time reasonable
DEFAULT_ELEVEN_VOICE = "JBFqnCBsd6RMkjVDRZzb"  # "George", a British male voice
ELEVEN_MODEL = "eleven_flash_v2_5"  # cheapest ElevenLabs model (half the credits)
EDGE_VOICE = "en-GB-RyanNeural"

# Written word -> how the voice should say it (case-sensitive, whole words)
PRONUNCIATIONS = {
    "Adhidev": "Adhi-dhev",
    "Dev": "Dhev",
}


def log(msg):
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except OSError:
        pass


def load_env():
    env = {}
    path = os.path.join(ROOT, ".env")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip().strip('"').strip("'")
    return env


# ---------- getting the reply text ----------

def text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def last_reply_from_transcript(path):
    """Final text Claude wrote after its last tool call in the latest turn."""
    try:
        with open(path, encoding="utf-8") as f:
            entries = [json.loads(l) for l in f if l.strip()]
    except (OSError, ValueError):
        return ""
    parts = []
    for e in reversed(entries):
        if e.get("type") == "user":
            break
        if e.get("type") != "assistant":
            continue
        content = (e.get("message") or {}).get("content")
        if isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "tool_use" for b in content):
            break
        t = text_of(content)
        if t.strip():
            parts.append(t)
    return "\n".join(reversed(parts))


def clean_for_speech(text):
    text = re.sub(r"```.*?```", " I've put the code on screen. ", text, flags=re.S)
    text = "\n".join(l for l in text.splitlines() if not l.strip().startswith("|"))  # tables
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)  # [label](url) -> label
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"^\s*#+\s*", "", text, flags=re.M)
    text = re.sub(r"^\s*([-*+]|\d+\.)\s+", "", text, flags=re.M)
    text = re.sub(r"[*_~>]+", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    for word, said in PRONUNCIATIONS.items():
        text = re.sub(rf"\b{word}\b", said, text)
    if len(text) > MAX_CHARS:
        cut = text[:MAX_CHARS]
        end = max(cut.rfind(". "), cut.rfind("? "), cut.rfind("! "))
        text = (cut[: end + 1] if end > 200 else cut) + " The rest is on screen."
    return text


# ---------- synthesis ----------

_eleven_blocked = {}  # api key -> time before which it isn't tried again


def synth_elevenlabs(text, out, env):
    key = env.get("ELEVENLABS_API_KEY")
    if not key or time.time() < _eleven_blocked.get(key, 0):
        return False
    voice = env.get("ELEVENLABS_VOICE_ID") or DEFAULT_ELEVEN_VOICE
    if voice == read_refused():
        voice = DEFAULT_ELEVEN_VOICE  # already known to be refused on this plan; don't waste a request
    try:
        eleven_request(text, out, key, voice)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):  # bad/expired key: skip ElevenLabs for 10 min instead of failing every sentence
            _eleven_blocked[key] = time.time() + 600
            log(f"ElevenLabs key rejected ({e.code}); using free voice for 10 min. Fix ELEVENLABS_API_KEY in .env")
            return False
        # Free plans can't use Voice Library voices via the API (402); fall back to a default voice.
        if e.code not in (402, 404) or voice == DEFAULT_ELEVEN_VOICE:
            raise
        log(f"voice {voice} refused ({e.code}), using default voice from now on")
        with open(REFUSED_FILE, "w") as f:
            f.write(voice)
        eleven_request(text, out, key, DEFAULT_ELEVEN_VOICE)
    return True


def is_speaking():
    """True while JARVIS is talking. A marker older than 90 s is left over from an interrupted voice, so ignore it."""
    try:
        return time.time() - os.path.getmtime(SPEAKING_FLAG) < 90
    except OSError:
        return False


def read_refused():
    try:
        with open(REFUSED_FILE) as f:
            return f.read().strip()
    except OSError:
        return ""


def synth_file(text, out):
    """Synthesize speech to an mp3 file with the best available engine. Returns False if none worked."""
    env = load_env()
    for engine in (synth_elevenlabs, synth_edge):
        try:
            if engine(text, out, env):
                return True
        except Exception as e:
            log(f"{engine.__name__} failed: {e!r}")
    return False


def cached_clip(text):
    """Path to an mp3 of a fixed line, synthesized once and reused (greetings, acknowledgements)."""
    import hashlib
    env = load_env()
    key = hashlib.sha1(f"{text}|{env.get('ELEVENLABS_VOICE_ID')}|{bool(env.get('ELEVENLABS_API_KEY'))}".encode()).hexdigest()[:16]
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, key + ".mp3")
    if not os.path.exists(path) and not synth_file(text, path):
        return None
    return path


def eleven_request(text, out, key, voice):
    req = urllib.request.Request(
        f"https://api.elevenlabs.io/v1/text-to-speech/{voice}?output_format=mp3_44100_128",
        data=json.dumps({"text": text, "model_id": ELEVEN_MODEL}).encode(),
        headers={"xi-api-key": key, "Content-Type": "application/json", "Accept": "audio/mpeg"},
    )
    with urllib.request.urlopen(req, timeout=30) as r, open(out, "wb") as f:
        f.write(r.read())


def synth_edge(text, out, env):
    import edge_tts
    voice = env.get("EDGE_VOICE") or EDGE_VOICE
    asyncio.run(edge_tts.Communicate(text, voice).save(out))
    return True


def play_mp3(path, alias="jarvis"):
    winmm = ctypes.windll.winmm

    def mci(cmd):
        err = winmm.mciSendStringW(cmd, None, 0, 0)
        if err:
            raise RuntimeError(f"MCI error {err} on: {cmd}")

    mci(f'open "{path}" type mpegvideo alias {alias}')
    try:
        mci(f"play {alias} wait")
    finally:
        winmm.mciSendStringW(f"close {alias}", None, 0, 0)


def speak_windows(text):
    ps = (
        "Add-Type -AssemblyName System.Speech;"
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer;"
        "$s.Speak([Console]::In.ReadToEnd())"
    )
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], input=text, text=True,
                   creationflags=0x08000000)


def speak_now(text):
    out = os.path.join(tempfile.gettempdir(), f"jarvis_{os.getpid()}.mp3")
    try:
        if synth_file(text, out):
            play_mp3(out)
        else:
            speak_windows(text)
    finally:
        if os.path.exists(out):
            try:
                os.remove(out)
            except OSError:
                pass


# ---------- background worker ----------

def stop_previous():
    """Cut off JARVIS if it is still talking about the previous reply."""
    try:
        with open(PID_FILE) as f:
            pid = f.read().strip()
    except OSError:
        return
    if pid.isdigit():
        subprocess.run(["taskkill", "/F", "/PID", pid, "/FI", "IMAGENAME eq python*"],
                       capture_output=True, creationflags=0x08000000)


def worker(text_file):
    with open(text_file, encoding="utf-8") as f:
        text = f.read()
    os.remove(text_file)
    stop_previous()
    with open(PID_FILE, "w") as f:
        f.write(str(os.getpid()))
    open(SPEAKING_FLAG, "w").close()  # lets the HUD animate and the clap listener ignore JARVIS's own voice
    try:
        speak_now(text)
    except Exception as e:
        log(f"worker failed: {e!r}")
    finally:
        try:
            os.remove(SPEAKING_FLAG)
        except OSError:
            pass


def launch_background(text):
    fd, path = tempfile.mkstemp(prefix="jarvis_", suffix=".txt")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    DETACHED_PROCESS, NEW_GROUP = 0x00000008, 0x00000200
    subprocess.Popen([sys.executable, os.path.abspath(__file__), "--say", path],
                     creationflags=DETACHED_PROCESS | NEW_GROUP, close_fds=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main():
    args = sys.argv[1:]
    if args[:1] == ["--say"] and len(args) == 2:
        worker(args[1])
        return
    if args[:1] == ["--text"]:
        speak_now(clean_for_speech(" ".join(args[1:])))
        return

    # Stop-hook mode
    if os.path.exists(MUTE_FILE) or os.environ.get("JARVIS_HUD"):
        return  # the HUD speaks its own replies sentence-by-sentence
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return
    text = payload.get("last_assistant_message")
    text = text_of(text) if text else last_reply_from_transcript(payload.get("transcript_path", ""))
    text = clean_for_speech(text or "")
    if text:
        launch_background(text)


if __name__ == "__main__":
    main()
