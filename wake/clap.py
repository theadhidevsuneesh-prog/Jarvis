"""JARVIS background ears: wakes the HUD on a double clap or when Dev says "Jarvis".

Runs silently from Windows startup and keeps listening even when the HUD window is closed.

  pythonw wake\\clap.py          background mode (what Windows startup uses)
  python  wake\\clap.py --test   show live mic level, claps and heard wake words, to tune sensitivity
  pythonw wake\\clap.py --open   just open the HUD (desktop shortcut)

Tuning in ../.env:
  CLAP_SENSITIVITY=1.0   higher = softer claps count, lower = fewer false alarms
  WAKE_WORD=on           set to off to disable the spoken "Jarvis" wake word
"""
import ctypes
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
from ctypes import wintypes

import numpy as np
import sounddevice as sd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "voice"))
import speak  # noqa: E402

RATE = 16000
BLOCK = 320                     # 20 ms of audio per block
MIN_GAP, MAX_GAP = 0.15, 0.9    # seconds allowed between the two claps
COOLDOWN = 4.0                  # ignore triggers for this long after waking
HUD_URL = "http://127.0.0.1:8765"
HUD_TITLE = "J.A.R.V.I.S."
EDGE = os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe")
MODEL_DIR = os.path.join(HERE, "model")
DEBUG_LOG = os.path.join(HERE, "clap_debug.log")
NO_WINDOW = 0x08000000
TEST = "--test" in sys.argv

env = speak.load_env()
sensitivity = float(env.get("CLAP_SENSITIVITY") or 1.0)
PEAK_MIN = 0.14 / sensitivity   # a clap must reach this (0..1); Dev's claps measure 0.24–0.65, room taps ~0.1
RATIO_MIN = 12.0 / sensitivity  # how many times louder than background noise
WAKE_WORD_ON = (env.get("WAKE_WORD") or "on").lower() != "off"

# Anything Dev says to JARVIS by name or pet name. Phrases with words the speech model doesn't know are skipped.
NAMES = ["jarvis", "sweetheart", "baby", "buddy", "darling", "honey"]
# Bare pet names ("honey", "buddy") are too easy to mishear from background talk, so from the background they only
# count as phrases ("hey buddy", "wake up sweetheart"). "jarvis" alone is allowed but needs high confidence.
WAKE_PHRASES = sorted({"jarvis", *(f"hey {n}" for n in NAMES), *(f"wake up {n}" for n in NAMES), *(f"okay {n}" for n in NAMES),
                       "wake up daddy's home", "daddy's home", "daddy is home", "i'm home", "i am home",
                       *(f"wake up {n} daddy's home" for n in NAMES), *(f"wake up {n} daddy is home" for n in NAMES)})
WAKE_SET = set(WAKE_PHRASES)

last_wake = float("-inf")
wake_lock = threading.Lock()


def debug(msg):
    if TEST:
        print("\n" + msg)
    try:
        if os.path.exists(DEBUG_LOG) and os.path.getsize(DEBUG_LOG) > 200_000:
            os.remove(DEBUG_LOG)
        with open(DEBUG_LOG, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except OSError:
        pass


# ---------- opening / focusing the HUD ----------

user32 = ctypes.windll.user32


def find_hud_window():
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _):
        n = user32.GetWindowTextLengthW(hwnd)
        if n and user32.IsWindowVisible(hwnd):
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            if buf.value.startswith(HUD_TITLE):
                found.append(hwnd)
        return True

    user32.EnumWindows(cb, 0)
    return found[0] if found else None


def bring_to_front(hwnd):
    # Windows blocks background apps from stealing focus; a synthetic Alt tap lifts that restriction.
    user32.keybd_event(0x12, 0, 0, 0)
    user32.keybd_event(0x12, 0, 2, 0)
    user32.ShowWindow(hwnd, 3)  # SW_MAXIMIZE
    user32.SetForegroundWindow(hwnd)


def hud_running():
    try:
        urllib.request.urlopen(HUD_URL + "/ping", timeout=1)
        return True
    except Exception:
        return False


def post(path, payload=None):
    req = urllib.request.Request(HUD_URL + path, data=json.dumps(payload or {}).encode(),
                                 headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=3)


def wake(reason, phrase=""):
    global last_wake
    with wake_lock:
        now = time.monotonic()
        if now - last_wake < COOLDOWN:
            return
        last_wake = now
    speak.log(f"wake: {reason}")
    if TEST:
        print(f"\n>>> WAKING JARVIS ({reason})")
    if not hud_running():
        pyw = sys.executable.replace("python.exe", "pythonw.exe")
        subprocess.Popen([pyw, os.path.join(ROOT, "hud", "server.py")], cwd=ROOT, creationflags=NO_WINDOW)
        for _ in range(40):
            if hud_running():
                break
            time.sleep(0.25)
    hwnd = find_hud_window()
    if hwnd:
        try:  # JARVIS is mid-answer: the HUD's own ears handle "stop", so don't barge in from here
            status = json.loads(urllib.request.urlopen(HUD_URL + "/status", timeout=2).read())
            if status.get("busy") or status.get("speaking"):
                debug(f"wake ignored: HUD is busy/speaking ({reason} {phrase!r})")
                return
        except Exception:
            pass
        post("/wake", {"reason": reason, "phrase": phrase})  # the open page reacts
    else:
        from urllib.parse import quote
        browser = EDGE if os.path.exists(EDGE) else "msedge"
        subprocess.Popen([browser, f"--app={HUD_URL}/?wake={reason}&phrase={quote(phrase)}", "--start-maximized",
                          "--autoplay-policy=no-user-gesture-required"], creationflags=NO_WINDOW)
        for _ in range(40):
            time.sleep(0.25)
            hwnd = find_hud_window()
            if hwnd:
                break
    if hwnd:
        bring_to_front(hwnd)


# ---------- clap detection ----------

HF_MIN = 0.2          # share of energy above 2 kHz; claps ~0.4–0.8, voices ~0.05


def high_freq_share(block):
    """A clap is a broadband crack; speech and hums live mostly below 2 kHz."""
    spec = np.abs(np.fft.rfft(block * np.hanning(len(block)))) ** 2
    cut = int(len(spec) * 2000 / (RATE / 2))
    total = spec[3:].sum()
    return float(spec[cut:].sum() / total) if total > 0 else 0.0


class ClapDetector:
    """Two sharp, loud transients 0.15–0.9 s apart, with no other loud taps around them (so typing never counts).

    Tuned from Dev's real claps (peaks 0.24–0.65, ringing up to ~200 ms from room echo and mic auto-gain)
    versus background taps (mostly 0.08–0.15).
    """
    QUIET_BEFORE, QUIET_AFTER = 0.5, 0.35
    ECHO = 0.12  # a much quieter transient this soon after a clap is its own echo, not another tap

    def __init__(self):
        self.noise = 0.003
        self.prev_peak = 0.0
        self.pending = None   # (time, peak, blocks_seen) for a candidate awaiting the decay check
        self.events = []      # (time, peak) of recent sharp transients, loud or soft
        self.confirm_at = None

    def feed(self, block, now):
        peak = float(np.max(np.abs(block)))
        rms = float(np.sqrt(np.mean(block ** 2)))
        if TEST:
            print(f"\rlevel {peak:5.3f}  noise {self.noise:6.4f}  need >{max(PEAK_MIN, self.noise * RATIO_MIN):5.3f}  "
                  + "#" * min(int(peak * 60), 60) + " " * 10, end="")

        # A transient is a sudden jump that dies away within ~160 ms (speech and music sustain).
        if self.pending:
            t, p, n = self.pending
            if peak < p * 0.4:
                self.pending = None
                self.events = [e for e in self.events if now - e[0] < 3] + [(t, p)]
                if p > PEAK_MIN and p > self.noise * RATIO_MIN:
                    self.check_pair(t)
            elif n >= 15:  # still loud after 300 ms: speech, music, a door…
                self.pending = None
                if p > PEAK_MIN:
                    debug(f"ignored loud sound {p:.3f}: too long for a clap")
            else:
                self.pending = (t, max(p, peak), n + 1)
        elif (peak > PEAK_MIN * 0.5 and peak > self.noise * 5 and peak > self.prev_peak * 1.8
              and high_freq_share(block) > HF_MIN):
            self.pending = (now, peak, 1)

        if self.confirm_at is not None and now >= self.confirm_at:
            self.confirm_at = None
            self.confirm()

        if rms < self.noise * 4:  # track background noise, ignoring loud events
            self.noise = max(0.995 * self.noise + 0.005 * rms, 0.001)
        self.prev_peak = peak

    def check_pair(self, t):
        loud = [e for e in self.events if e[1] > PEAK_MIN]
        if len(loud) >= 2 and MIN_GAP <= t - loud[-2][0] <= MAX_GAP:
            self.confirm_at = t + self.QUIET_AFTER  # wait to make sure nothing else follows
        else:
            debug(f"clap {self.events[-1][1]:.3f} — waiting for a second")

    def confirm(self):
        loud = [e for e in self.events if e[1] > PEAK_MIN]
        first, second = loud[-2], loud[-1]
        others = [e for e in self.events if first[0] - self.QUIET_BEFORE <= e[0] <= second[0] + self.QUIET_AFTER
                  and e not in (first, second)
                  and not (0 < e[0] - first[0] < self.ECHO and e[1] < first[1] * 0.35)      # echo of clap 1
                  and not (0 < e[0] - second[0] < self.ECHO and e[1] < second[1] * 0.35)]  # echo of clap 2
        if others:
            debug(f"ignored clap pair ({first[1]:.3f}, {second[1]:.3f}): {len(others)} other taps nearby (typing?)")
            return
        if speak.is_speaking():
            return  # probably JARVIS's own voice
        debug(f"DOUBLE CLAP ({first[1]:.3f}, {second[1]:.3f}, {second[0] - first[0]:.2f}s apart)")
        self.events.clear()
        threading.Thread(target=wake, args=("clap",), daemon=True).start()


# ---------- "Jarvis" wake word (offline, Vosk) ----------

class WakeWord:
    def __init__(self):
        self.rec = None
        self.model = None
        if not WAKE_WORD_ON or not os.path.isdir(MODEL_DIR):
            return
        try:
            import vosk
            vosk.SetLogLevel(-1)
            self.model = vosk.Model(MODEL_DIR)  # words the model doesn't know are ignored inside phrases
            self.rec = vosk.KaldiRecognizer(self.model, RATE, json.dumps(WAKE_PHRASES + ["[unk]"]))
            self.rec.SetWords(True)
        except Exception as e:
            speak.log(f"wake word unavailable: {e!r}")

    def feed(self, block):
        if not self.rec:
            return
        pcm = (np.clip(block, -1, 1) * 32767).astype(np.int16).tobytes()
        if self.rec.AcceptWaveform(pcm):
            res = json.loads(self.rec.Result())
            words = [w for w in res.get("result", []) if w.get("word") != "[unk]"]
            text = " ".join(w["word"] for w in words)
            conf = min((w.get("conf", 0) for w in words), default=0)
            # Must be one whole wake phrase, heard confidently — not scraps of ordinary talk.
            needed = 0.9 if " " not in text else 0.75  # single words need to be heard very clearly
            if text in WAKE_SET and conf >= needed and not speak.is_speaking():
                debug(f'heard wake phrase: "{text}" (confidence {conf:.2f})')
                threading.Thread(target=wake, args=("voice", text), daemon=True).start()
            elif text:
                debug(f'ignored speech "{text}" (confidence {conf:.2f})')


# ---------- "stop" while JARVIS is thinking/talking (offline, independent of the browser) ----------

STOP_PHRASES = ["stop", "stop it", "stop stop", "jarvis stop", "stop jarvis", "okay stop", "wait", "wait wait",
                "hold on", "cancel", "shut up", "enough", "be quiet", "quiet"]


class Hub:
    """Keeps a fresh copy of the HUD server's /status (busy, speaking, listen requests)."""

    def __init__(self):
        self.status = {}
        threading.Thread(target=self.poll, daemon=True).start()

    def poll(self):
        while True:
            try:
                self.status = json.loads(urllib.request.urlopen(HUD_URL + "/status", timeout=1).read())
            except Exception:
                self.status = {}
                time.sleep(1)
            time.sleep(0.1)


hub = Hub()


class StopWord:
    """Only active while the HUD reports busy/speaking. The server double-checks the word isn't JARVIS's own echo."""

    def __init__(self, model):
        self.rec, self.active, self.last = None, False, 0.0
        if model is None:
            return
        import vosk
        self.vosk, self.model = vosk, model
        threading.Thread(target=self.poll, daemon=True).start()

    def poll(self):
        while True:
            s = hub.status
            active = bool(s.get("busy") or s.get("speaking"))
            if active and not self.active:  # fresh recognizer per busy period
                self.rec = self.vosk.KaldiRecognizer(self.model, RATE, json.dumps(STOP_PHRASES + ["[unk]"]))
                self.rec.SetWords(True)
            self.active = active
            time.sleep(0.1)

    def feed(self, block):
        rec = self.rec
        if not self.active or rec is None:
            return
        pcm = (np.clip(block, -1, 1) * 32767).astype(np.int16).tobytes()
        if not rec.AcceptWaveform(pcm):
            return  # partial guesses are unreliable (JARVIS's own sentences look like "stop"), wait for the final
        res = json.loads(rec.Result())
        all_words = res.get("result", [])
        words = [w for w in all_words if w.get("word") != "[unk]"]
        text = " ".join(w["word"] for w in words)
        conf = min((w.get("conf", 0) for w in words), default=0)
        # A real "stop" is a short utterance on its own; a stop-word buried in a long sentence is JARVIS talking.
        if len(all_words) - len(words) > 2:
            return
        if text in STOP_PHRASES and conf >= 0.85 and time.monotonic() - self.last > 2:
            self.last = time.monotonic()
            debug(f'heard "{text}" while JARVIS was busy/talking (confidence {conf:.2f})')
            threading.Thread(target=post, args=("/bargein", {"word": text}), daemon=True).start()
            rec.Reset()


# ---------- listening for commands: record Dev when the HUD asks, transcribe offline with Whisper ----------

WHISPER_MODEL = env.get("WHISPER_MODEL") or "small.en"
# Voice level that counts as "Dev is talking". The laptop mic's noise suppression gates silence to ~0.0001,
# so a low floor is safe. Tune with VOICE_MIN in .env if JARVIS misses quiet speech (lower) or hears noise (higher).
VOICE_MIN = float(env.get("VOICE_MIN") or 0.0035)


class Recorder:
    PRE_ROLL = 15          # blocks (0.3 s) kept from before speech starts, so the first word isn't clipped
    END_SILENCE = 40       # blocks (0.8 s) of quiet that mean Dev has finished
    MAX_BLOCKS = 50 * 20   # 20 s cap

    def __init__(self, claps):
        self.claps = claps        # shares its background-noise estimate
        self.session = None
        self.model = None
        self.jobs = []
        threading.Thread(target=self.load, daemon=True).start()

    def load(self):
        try:
            from faster_whisper import WhisperModel
            t = time.time()
            self.model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8", cpu_threads=6)
            self.model.transcribe(np.zeros(RATE, np.float32), language="en", beam_size=1)  # warm-up
            debug(f"whisper {WHISPER_MODEL} ready in {time.time() - t:.1f}s")
        except Exception as e:
            speak.log(f"whisper failed to load: {e!r}")

    def feed(self, block, now):
        req = hub.status.get("listen") or {}
        if not req.get("active"):
            self.session = None
            return
        s = self.session
        if s is None or s["id"] != req["id"]:  # a new listen request from the HUD
            s = self.session = {"id": req["id"], "deadline": now + req.get("timeout", 8000) / 1000, "pre": [], "buf": [],
                                "started": False, "loud": 0, "quiet": 0, "done": False}
        if s["done"]:
            return
        rms = float(np.sqrt(np.mean(block ** 2)))
        threshold = max(self.claps.noise * 3.0, VOICE_MIN)
        speech = rms > threshold
        s["max_rms"] = max(s.get("max_rms", 0), rms)
        if not s["started"]:
            s["pre"] = (s["pre"] + [block])[-self.PRE_ROLL:]
            s["loud"] = s["loud"] + 1 if speech else 0
            if s["loud"] >= 3:  # 60 ms of voice: Dev has started talking
                s["started"], s["buf"] = True, list(s["pre"])
                debug(f"listen #{s['id']}: voice detected (level {rms:.4f}, threshold {threshold:.4f})")
                self.phase(s["id"], "recording")
            elif now > s["deadline"]:
                debug(f"listen #{s['id']}: nothing heard (loudest {s['max_rms']:.4f}, threshold {threshold:.4f})")
                self.finish(s, empty=True)
            return
        s["buf"].append(block)
        s["quiet"] = 0 if speech else s["quiet"] + 1
        if s["quiet"] >= self.END_SILENCE or len(s["buf"]) >= self.MAX_BLOCKS:
            self.finish(s)

    def phase(self, sid, phase):
        threading.Thread(target=lambda: post("/listen/phase", {"id": sid, "phase": phase}), daemon=True).start()

    def finish(self, s, empty=False):
        s["done"] = True  # ignore further audio until the HUD asks again
        if empty:
            threading.Thread(target=lambda: post("/heard", {"id": s["id"], "text": ""}), daemon=True).start()
            return
        audio = np.concatenate(s["buf"])
        self.phase(s["id"], "transcribing")
        threading.Thread(target=self.transcribe, args=(s["id"], audio), daemon=True).start()

    def transcribe(self, sid, audio):
        text = ""
        try:
            for _ in range(100):  # model may still be loading right after startup
                if self.model:
                    break
                time.sleep(0.2)
            t = time.time()
            segs, _ = self.model.transcribe(audio, language="en", beam_size=1, condition_on_previous_text=False,
                                            initial_prompt="Jarvis, Dev, Kochi, sweetheart, daddy's home.")
            text = " ".join(x.text.strip() for x in segs).strip()
            if text.lower().strip(" .!?") in ("", "you", "thank you", "thanks for watching", "bye"):
                text = "" if len(audio) < RATE * 1.2 else text  # Whisper's classic hallucinations on near-silence
            debug(f'transcribed {len(audio) / RATE:.1f}s in {time.time() - t:.1f}s: "{text}"')
        except Exception as e:
            speak.log(f"transcription failed: {e!r}")
        post("/heard", {"id": sid, "text": text})


def main():
    if "--open" in sys.argv:
        global COOLDOWN
        COOLDOWN = 0
        wake("manual")
        return
    claps, word = ClapDetector(), WakeWord()
    stopper = StopWord(word.model)
    recorder = Recorder(claps)
    debug(f"listening (clap threshold {PEAK_MIN:.3f}, wake word {'on' if word.rec else 'off'})")
    while True:  # reopen the mic if it disappears (sleep/resume, headset unplugged)
        try:
            with sd.InputStream(samplerate=RATE, channels=1, blocksize=BLOCK, dtype="float32") as stream:
                while True:
                    data, _ = stream.read(BLOCK)
                    block = data[:, 0]
                    now = time.monotonic()
                    claps.feed(block, now)
                    word.feed(block)
                    stopper.feed(block)
                    recorder.feed(block, now)
        except KeyboardInterrupt:
            return
        except Exception as e:
            speak.log(f"listener mic error: {e!r}; retrying")
            time.sleep(3)


if __name__ == "__main__":
    main()
