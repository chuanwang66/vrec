#!/usr/bin/env python3
"""
vrec — a small, low-profile audio recorder + transcriber for macOS.

Records from a microphone via ffmpeg (no GUI window), supports immediate or
scheduled start/end, and one-command transcription through an OpenAI-compatible
speech-recognition model (default: Aliyun DashScope qwen3-asr-flash).

Usage examples:
    vrec devices                          # list microphone input devices
    vrec config --show                    # show current configuration
    vrec config --set audio_device=1      # pick the mic from `vrec devices`
    vrec rec -d 30m                        # record 30 minutes, then stop
    vrec rec                               # record until `vrec stop`
    vrec rec --at "14:00" -d 45m -t        # start at 14:00, record 45m, transcribe
    vrec rec --start "2026-06-22 09:00" --end "2026-06-22 10:30"
    vrec stop                              # stop the current recording
    vrec status                            # is anything recording?
    vrec transcribe path/to/file.mp3       # transcribe a file
    vrec transcribe                        # transcribe the most recent recording

Notes:
  * macOS shows the orange "microphone in use" indicator while recording. That
    is an OS privacy feature and is intentionally not circumvented.
  * The controlling app (Terminal/iTerm/launchd) must be granted Microphone
    access in System Settings > Privacy & Security > Microphone.
  * Recording others without consent may be illegal in your jurisdiction. Use
    this only where you have the right to record.
"""

import argparse
import base64
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
import warnings
from datetime import datetime, timedelta
from pathlib import Path

# Quiet a noisy version-mismatch warning some requests/urllib3 combos emit.
warnings.filterwarnings("ignore", message=r"urllib3 .*", module="requests")

try:
    import requests
except ImportError:  # pragma: no cover
    sys.exit("This tool needs the 'requests' package: pip3 install requests")

HOME = Path.home()
APP_DIR = HOME / ".vrec"
CONFIG_PATH = APP_DIR / "config.json"
PID_PATH = APP_DIR / "current.json"
LOG_PATH = APP_DIR / "vrec.log"
MODELS_DIR = APP_DIR / "models"

# Keep local-model downloads inside the app dir (always writable & portable)
# instead of relying on ~/.cache/huggingface, which may be missing/unwritable.
os.environ.setdefault("HF_HOME", str(MODELS_DIR))

DEFAULT_CONFIG = {
    # --- transcription engine: "cloud" (external API) or "local" (offline Whisper) ---
    "engine": "cloud",
    "language": None,        # e.g. "zh", "en"; None = auto-detect
    # --- cloud engine (OpenAI-compatible speech recognition) ---
    "api_key": "",           # set in the app's Settings, or: vrec config --set api_key=sk-...
    "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "model": "qwen3-asr-flash",
    "enable_itn": True,      # inverse text normalization (digits/punctuation)
    # --- local engine (faster-whisper, runs fully offline) ---
    "local_model": "small",  # tiny | base | small | medium | large-v3
    # --- recording ---
    "audio_device": "0",     # avfoundation input index/name (see `vrec devices`)
    "sample_rate": 16000,    # 16 kHz is ideal for speech recognition
    "channels": 1,           # mono
    "bitrate": "48k",        # MP3 bitrate (small + plenty for voice)
    "recordings_dir": str(APP_DIR / "recordings"),
    # --- transcription chunking (API limit: 10 MB / 5 min per request) ---
    "chunk_seconds": 240,    # split long audio into <=4 min pieces
    # --- menu-bar app ---
    "logo_path": "",         # custom menu-bar icon path (set via the app)
}

# Per-request safety ceilings for the sync endpoint.
MAX_SINGLE_SECONDS = 290
MAX_SINGLE_BYTES = int(9.5 * 1024 * 1024)


# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #
def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text()))
        except (json.JSONDecodeError, OSError) as e:
            print(f"warning: could not read config ({e}); using defaults", file=sys.stderr)
    return cfg


def save_config(cfg: dict) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))
    try:
        os.chmod(CONFIG_PATH, 0o600)  # the file holds an API key
    except OSError:
        pass


def coerce_value(raw: str):
    """Turn a CLI string into a bool/int/None when it obviously is one."""
    low = raw.lower()
    if low in ("true", "false"):
        return low == "true"
    if low in ("none", "null"):
        return None
    if re.fullmatch(r"-?\d+", raw):
        return int(raw)
    return raw


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def which_or_die(binary: str) -> str:
    path = subprocess.run(["bash", "-lc", f"command -v {binary}"],
                          capture_output=True, text=True).stdout.strip()
    if not path:
        sys.exit(
            f"'{binary}' not found. Install it with:  brew install ffmpeg"
        )
    return path


def log(msg: str, quiet: bool = False) -> None:
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    try:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_PATH, "a") as fh:
            fh.write(line + "\n")
    except OSError:
        pass
    if not quiet:
        print(line, file=sys.stderr)


def parse_duration(text: str) -> int:
    """'90'->90s, '30m', '1h', '1h30m', '90s' -> seconds."""
    text = text.strip().lower()
    if re.fullmatch(r"\d+", text):
        return int(text)
    total, matched = 0, False
    for value, unit in re.findall(r"(\d+)\s*([hms])", text):
        matched = True
        total += int(value) * {"h": 3600, "m": 60, "s": 1}[unit]
    if not matched:
        raise ValueError(f"invalid duration: {text!r} (try 30m, 1h30m, 90s)")
    return total


def parse_when(text: str) -> datetime:
    """Parse a start/end time. Accepts 'now', 'HH:MM[:SS]', 'YYYY-MM-DD HH:MM[:SS]'."""
    text = text.strip()
    if text.lower() == "now":
        return datetime.now()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
                "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            pass
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            t = datetime.strptime(text, fmt).time()
            now = datetime.now()
            cand = now.replace(hour=t.hour, minute=t.minute,
                               second=t.second, microsecond=0)
            if cand <= now:               # time already passed today -> tomorrow
                cand += timedelta(days=1)
            return cand
        except ValueError:
            pass
    raise ValueError(f"invalid time: {text!r} (try '14:00' or '2026-06-22 09:00')")


def fmt_secs(s: float) -> str:
    s = int(s)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m{sec:02d}s"
    if m:
        return f"{m}m{sec:02d}s"
    return f"{sec}s"


def fmt_clock(s: float) -> str:
    """Stopwatch style for the live recording display: M:SS or H:MM:SS."""
    s = max(0, int(s))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def ffprobe_duration(path: str) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nokey=1:noprint_wrappers=1", path],
        capture_output=True, text=True,
    ).stdout.strip()
    try:
        return float(out)
    except ValueError:
        return 0.0


def make_icon(src: str, dst: str, size: int = 44) -> None:
    """Center-crop + resize an image into a small square PNG for the menu bar."""
    src = os.path.expanduser(src)
    info = subprocess.run(["sips", "-g", "pixelWidth", "-g", "pixelHeight", src],
                          capture_output=True, text=True).stdout
    w = h = 0
    for line in info.splitlines():
        if "pixelWidth:" in line:
            w = int(line.split(":")[1])
        elif "pixelHeight:" in line:
            h = int(line.split(":")[1])
    Path(dst).parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["sips", "-s", "format", "png", src, "--out", dst],
                   check=True, capture_output=True)
    if w and h:  # scale by the shorter side, then center-crop to a square
        flag = "--resampleWidth" if w <= h else "--resampleHeight"
        subprocess.run(["sips", flag, str(size), dst, "--out", dst],
                       check=True, capture_output=True)
        subprocess.run(["sips", "-c", str(size), str(size), dst, "--out", dst],
                       check=True, capture_output=True)


# --------------------------------------------------------------------------- #
# commands: devices / config
# --------------------------------------------------------------------------- #
def list_audio_devices() -> list:
    """Return [(index, name), …] for avfoundation audio input devices."""
    which_or_die("ffmpeg")
    # avfoundation prints the device list to stderr and then "errors" out.
    res = subprocess.run(
        ["ffmpeg", "-hide_banner", "-f", "avfoundation",
         "-list_devices", "true", "-i", ""],
        capture_output=True, text=True,
    )
    audio, in_audio = [], False
    for line in res.stderr.splitlines():
        if "AVFoundation audio devices" in line:
            in_audio = True
            continue
        if "AVFoundation video devices" in line:
            in_audio = False
            continue
        m = re.search(r"\[(\d+)\]\s+(.*)$", line)
        if in_audio and m:
            audio.append((m.group(1), m.group(2).strip()))
    return audio


# Virtual loopback input devices that actually carry system/speaker audio.
LOOPBACK_HINTS = ("blackhole", "soundflower", "loopback", "aggregate",
                  "multi-output", "multioutput", "vb-cable", "vb-audio",
                  "ishowu", "background music", "存在音频", "聚集设备", "多输出")


def device_kind(name: str) -> str:
    """'system' if this input captures speaker/system audio, else 'mic'."""
    low = name.lower()
    return "system" if any(h in low for h in LOOPBACK_HINTS) else "mic"


def cmd_devices(_args) -> int:
    audio = list_audio_devices()
    cfg = load_config()
    current = str(cfg.get("audio_device"))
    print("录音输入设备 (avfoundation)：\n")
    if audio:
        for idx, name in audio:
            kind = "🔊 系统声音/扬声器" if device_kind(name) == "system" else "🎙 麦克风/输入"
            mark = "   ← 当前" if idx == current else ""
            print(f"  [{idx}] {name}  — {kind}{mark}")
    else:
        print("  （未发现任何输入设备：没有麦克风，也没有回环设备）")

    if not any(device_kind(n) == "system" for _, n in audio):
        print("\n要录『扬声器 / 系统声音』？macOS 不能直接录输出设备，需装虚拟回环设备：")
        print("    brew install blackhole-2ch")
        print("  装好后 BlackHole 会作为输入设备出现在上面，选它即可录系统声音。")
        print("  想同时还能听见：在『音频 MIDI 设置』里建一个『多输出设备』= 显示器 + BlackHole。")
    print("\n选择设备：  vrec config --set audio_device=<序号或设备名>")
    return 0


def cmd_config(args) -> int:
    cfg = load_config()
    changed = False
    for pair in args.set or []:
        if "=" not in pair:
            print(f"ignoring '{pair}' (expected key=value)", file=sys.stderr)
            continue
        key, raw = pair.split("=", 1)
        key = key.strip()
        if key not in DEFAULT_CONFIG:
            print(f"unknown key '{key}'. Valid keys: {', '.join(DEFAULT_CONFIG)}",
                  file=sys.stderr)
            continue
        cfg[key] = coerce_value(raw.strip())
        changed = True
    if changed:
        save_config(cfg)
        print(f"saved {CONFIG_PATH}")
    if args.show or not changed:
        shown = dict(cfg)
        if shown.get("api_key"):                       # don't print the secret
            k = shown["api_key"]
            shown["api_key"] = k[:6] + "…" + k[-4:] if len(k) > 12 else "set"
        print(json.dumps(shown, indent=2, ensure_ascii=False))
        print(f"\nconfig file: {CONFIG_PATH}")
    return 0


# --------------------------------------------------------------------------- #
# commands: record / stop / status
# --------------------------------------------------------------------------- #
def _build_ffmpeg_cmd(cfg: dict, out_path: str, duration: int | None,
                      device: str) -> list:
    base = ["ffmpeg", "-hide_banner", "-loglevel", "error"]
    if os.environ.get("VREC_FAKE_MIC"):
        # Demo/verification source for machines with no microphone (a quiet tone),
        # so the recording indicator + live timer can be exercised.
        inp = ["-re", "-f", "lavfi", "-i",   # -re: feed at real-time, else it floods
               f"sine=frequency=440:sample_rate={cfg['sample_rate']}"]
    else:
        inp = ["-f", "avfoundation", "-thread_queue_size", "1024", "-i", f":{device}"]
    cmd = base + inp + [
        "-ar", str(cfg["sample_rate"]),
        "-ac", str(cfg["channels"]),
        "-c:a", "libmp3lame", "-b:a", str(cfg["bitrate"]),
    ]
    if duration:
        cmd += ["-t", str(duration)]
    cmd += ["-y", out_path]
    return cmd


def _write_pidfile(ffmpeg_pid: int, out_path: str) -> None:
    PID_PATH.write_text(json.dumps({
        "ffmpeg_pid": ffmpeg_pid,
        "python_pid": os.getpid(),
        "file": out_path,
        "started_at": datetime.now().isoformat(timespec="seconds"),
    }))


def _clear_pidfile() -> None:
    try:
        PID_PATH.unlink()
    except FileNotFoundError:
        pass


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True            # exists, just owned by another user
    except OSError:
        return False


def cmd_rec(args) -> int:
    which_or_die("ffmpeg")
    cfg = load_config()
    quiet = args.quiet
    device = args.device if args.device is not None else str(cfg["audio_device"])

    if PID_PATH.exists():
        try:
            info = json.loads(PID_PATH.read_text())
            if _pid_alive(int(info["ffmpeg_pid"])):
                print(f"A recording is already in progress -> {info['file']}\n"
                      f"Stop it with: vrec stop", file=sys.stderr)
                return 1
        except (json.JSONDecodeError, OSError, KeyError, ValueError):
            pass
        _clear_pidfile()

    # ---- resolve start time ----
    start_dt = parse_when(args.at) if args.at else datetime.now()

    # ---- output path ----
    if args.out:
        out_path = str(Path(args.out).expanduser())
    else:
        rec_dir = Path(os.path.expanduser(cfg["recordings_dir"]))
        rec_dir.mkdir(parents=True, exist_ok=True)
        out_path = str(rec_dir / f"rec-{start_dt:%Y%m%d-%H%M%S}.mp3")
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)

    # ---- wait for scheduled start ----
    if start_dt > datetime.now():
        log(f"scheduled: recording starts at {start_dt:%Y-%m-%d %H:%M:%S} "
            f"(in {fmt_secs((start_dt - datetime.now()).total_seconds())})", quiet)
        try:
            while datetime.now() < start_dt:
                time.sleep(min(5, (start_dt - datetime.now()).total_seconds()))
        except KeyboardInterrupt:
            log("scheduled recording cancelled before start", quiet)
            return 130

    # ---- resolve duration ----
    duration = None
    if args.duration:
        duration = parse_duration(args.duration)
    elif args.end:
        end_dt = parse_when(args.end)
        duration = int((end_dt - datetime.now()).total_seconds())
        if duration <= 0:
            print(f"end time {end_dt} is not in the future", file=sys.stderr)
            return 1

    # ---- record ----
    cmd = _build_ffmpeg_cmd(cfg, out_path, duration, device)
    human_dur = fmt_secs(duration) if duration else "until 'vrec stop'"
    log(f"recording -> {out_path}  (device :{device}, {human_dur})", quiet)

    proc = subprocess.Popen(cmd)
    _write_pidfile(proc.pid, out_path)

    def _forward(signum, _frame):
        try:
            proc.send_signal(signal.SIGINT)   # let ffmpeg finalize the file
        except ProcessLookupError:
            pass
    signal.signal(signal.SIGINT, _forward)
    signal.signal(signal.SIGTERM, _forward)

    rc = proc.wait()
    _clear_pidfile()

    if not Path(out_path).exists() or Path(out_path).stat().st_size < 1024:
        log("WARNING: output is empty/tiny. Check microphone permission "
            "(System Settings > Privacy & Security > Microphone) and the "
            "selected device (vrec devices).", quiet)
        return 1

    size_mb = Path(out_path).stat().st_size / 1024 / 1024
    log(f"saved {out_path} ({size_mb:.2f} MB, {fmt_secs(ffprobe_duration(out_path))})",
        quiet)

    if args.transcribe:
        return _do_transcribe(cfg, out_path, None, args.language, quiet,
                              engine=getattr(args, "engine", None))
    return 0 if rc in (0, 130, 255) else rc   # 130/255 == ffmpeg interrupted (normal stop)


def cmd_stop(_args) -> int:
    if not PID_PATH.exists():
        print("no recording in progress", file=sys.stderr)
        return 1
    try:
        info = json.loads(PID_PATH.read_text())
        pid = int(info["ffmpeg_pid"])
    except (json.JSONDecodeError, OSError, KeyError, ValueError):
        _clear_pidfile()
        print("no valid recording state found", file=sys.stderr)
        return 1
    try:
        os.kill(pid, signal.SIGINT)   # graceful: ffmpeg writes the trailer
        print(f"stopping recording -> {info.get('file')}")
    except ProcessLookupError:
        print("recording process already gone", file=sys.stderr)
        _clear_pidfile()
        return 1
    return 0


def cmd_status(_args) -> int:
    if not PID_PATH.exists():
        print("idle (no recording in progress)")
        return 0
    try:
        info = json.loads(PID_PATH.read_text())
        pid = int(info["ffmpeg_pid"])
    except (json.JSONDecodeError, OSError, KeyError, ValueError):
        print("idle (stale state cleared)")
        _clear_pidfile()
        return 0
    if _pid_alive(pid):
        started = info.get("started_at", "?")
        elapsed = ""
        try:
            elapsed = " (" + fmt_secs(
                (datetime.now() - datetime.fromisoformat(started)).total_seconds()
            ) + " elapsed)"
        except ValueError:
            pass
        print(f"RECORDING -> {info.get('file')}\nstarted {started}{elapsed}\n"
              f"stop with: vrec stop")
    else:
        print("idle (previous recording ended)")
        _clear_pidfile()
    return 0


# --------------------------------------------------------------------------- #
# transcription
# --------------------------------------------------------------------------- #
def _normalize_audio(src: str, dst: str, cfg: dict) -> None:
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", src,
         "-ar", str(cfg["sample_rate"]), "-ac", str(cfg["channels"]),
         "-c:a", "libmp3lame", "-b:a", str(cfg["bitrate"]), dst],
        check=True,
    )


def _segment_audio(src: str, out_dir: str, chunk_seconds: int) -> list:
    pattern = os.path.join(out_dir, "chunk_%04d.mp3")
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", src,
         "-f", "segment", "-segment_time", str(chunk_seconds), "-c", "copy",
         pattern],
        check=True,
    )
    return sorted(Path(out_dir).glob("chunk_*.mp3"))


def _api_transcribe(cfg: dict, audio_path: str) -> str:
    if not cfg.get("api_key"):
        raise RuntimeError("no api_key set (vrec config --set api_key=...)")
    b64 = base64.b64encode(Path(audio_path).read_bytes()).decode()
    data_uri = f"data:audio/mpeg;base64,{b64}"
    asr_options = {"enable_itn": bool(cfg.get("enable_itn", True))}
    if cfg.get("language"):
        asr_options["language"] = cfg["language"]
    body = {
        "model": cfg["model"],
        "messages": [{
            "role": "user",
            "content": [{"type": "input_audio", "input_audio": {"data": data_uri}}],
        }],
        "asr_options": asr_options,
        "stream": False,
    }
    url = cfg["base_url"].rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {cfg['api_key']}",
               "Content-Type": "application/json"}
    resp = requests.post(url, headers=headers, json=body, timeout=300)
    if resp.status_code != 200:
        raise RuntimeError(f"API error {resp.status_code}: {resp.text[:800]}")
    data = resp.json()
    try:
        return data["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError):
        raise RuntimeError(f"unexpected API response: {json.dumps(data)[:800]}")


# ---- local engine (faster-whisper, fully offline) ----
_LOCAL_MODELS: dict = {}


def _ensure_faster_whisper():
    """Import faster-whisper, installing it on first use."""
    try:
        import faster_whisper  # noqa: F401
        return faster_whisper
    except ImportError:
        print("installing faster-whisper (one-time, may take a minute)…",
              file=sys.stderr)
        subprocess.run([sys.executable, "-m", "pip", "install", "faster-whisper"],
                       check=True)
        import faster_whisper
        return faster_whisper


def load_local_model(name: str, quiet: bool = True):
    """Load (downloading on first use into ~/.vrec/models) a faster-whisper model."""
    model = _LOCAL_MODELS.get(name)
    if model is None:
        fw = _ensure_faster_whisper()
        MODELS_DIR.mkdir(parents=True, exist_ok=True)
        log(f"loading local model '{name}' (first use downloads it)…", quiet)
        model = fw.WhisperModel(name, device="cpu", compute_type="int8",
                                download_root=str(MODELS_DIR))
        _LOCAL_MODELS[name] = model
    return model


def _transcribe_local(cfg: dict, src: Path, quiet: bool, progress=None) -> str:
    name = cfg.get("local_model") or "small"
    model = load_local_model(name, quiet)
    log("transcribing locally (offline)…", quiet)
    segments, info = model.transcribe(
        str(src), language=cfg.get("language") or None, beam_size=5,
    )
    total = getattr(info, "duration", 0) or 0
    parts = []
    for seg in segments:
        parts.append(seg.text)
        if progress and total:
            frac = min(seg.end / total, 1.0)
            progress(frac, f"{int(frac * 100)}%")
    if progress:
        progress(1.0, "100%")
    return "".join(parts).strip()


def transcribe_file(cfg: dict, audio_path: str, quiet: bool = False,
                    engine: str | None = None, progress=None) -> str:
    src = Path(audio_path).expanduser()
    if not src.exists():
        raise FileNotFoundError(f"file not found: {src}")
    engine = (engine or cfg.get("engine") or "cloud").lower()
    if engine == "local":
        return _transcribe_local(cfg, src, quiet, progress)
    return _transcribe_cloud(cfg, src, quiet, progress)


def _transcribe_cloud(cfg: dict, src: Path, quiet: bool = False, progress=None) -> str:
    which_or_die("ffmpeg")
    with tempfile.TemporaryDirectory(prefix="vrec_") as tmp:
        normalized = os.path.join(tmp, "norm.mp3")
        _normalize_audio(str(src), normalized, cfg)
        dur = ffprobe_duration(normalized)
        size = os.path.getsize(normalized)
        if dur < 0.3:
            raise RuntimeError(
                f"audio is empty/silent ({dur:.1f}s) — nothing to transcribe. "
                "Check the input file or microphone."
            )

        if dur <= MAX_SINGLE_SECONDS and size <= MAX_SINGLE_BYTES:
            log(f"transcribing ({fmt_secs(dur)}) …", quiet)
            text = _api_transcribe(cfg, normalized).strip()
            if progress:
                progress(1.0, "")
            return text

        chunks = _segment_audio(normalized, tmp, int(cfg["chunk_seconds"]))
        n = len(chunks)
        log(f"long audio ({fmt_secs(dur)}): split into {n} chunks", quiet)
        parts = []
        for i, chunk in enumerate(chunks, 1):
            log(f"  chunk {i}/{n} …", quiet)
            parts.append(_api_transcribe(cfg, str(chunk)).strip())
            if progress:
                progress(i / n, f"{i}/{n}")
        return "\n".join(p for p in parts if p)


def _latest_recording(cfg: dict) -> str | None:
    rec_dir = Path(os.path.expanduser(cfg["recordings_dir"]))
    files = sorted(rec_dir.glob("*.mp3"), key=lambda p: p.stat().st_mtime,
                   reverse=True) if rec_dir.exists() else []
    return str(files[0]) if files else None


def _do_transcribe(cfg, audio_path, out, language, quiet, engine=None) -> int:
    if language:
        cfg = dict(cfg, language=language)
    try:
        text = transcribe_file(cfg, audio_path, quiet, engine=engine)
    except Exception as e:                      # noqa: BLE001 - surface any failure
        print(f"transcription failed: {e}", file=sys.stderr)
        return 1
    out_path = Path(out).expanduser() if out else Path(audio_path).with_suffix(".txt")
    out_path.write_text(text + "\n", encoding="utf-8")
    log(f"transcript -> {out_path}", quiet)
    print(text)
    return 0


def cmd_transcribe(args) -> int:
    cfg = load_config()
    audio_path = args.file
    if not audio_path:
        audio_path = _latest_recording(cfg)
        if not audio_path:
            print("no file given and no recordings found in "
                  f"{cfg['recordings_dir']}", file=sys.stderr)
            return 1
        print(f"transcribing most recent recording: {audio_path}", file=sys.stderr)
    return _do_transcribe(cfg, audio_path, args.out, args.language, args.quiet,
                          engine=args.engine)


# --------------------------------------------------------------------------- #
# argument parser
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="vrec",
        description="Low-profile audio recorder + transcriber for macOS.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("devices", help="list microphone input devices")

    pc = sub.add_parser("config", help="show or change configuration")
    pc.add_argument("--show", action="store_true", help="print current config")
    pc.add_argument("--set", action="append", metavar="key=value",
                    help="set a config value (repeatable)")

    pr = sub.add_parser("rec", help="record audio")
    pr.add_argument("-d", "--duration", help="record for this long (e.g. 30m, 1h30m, 90s)")
    pr.add_argument("-o", "--out", help="output file (default: timestamped .mp3)")
    pr.add_argument("--at", "--start", dest="at",
                    help="start time: 'HH:MM' or 'YYYY-MM-DD HH:MM'")
    pr.add_argument("--end", help="stop time: 'HH:MM' or 'YYYY-MM-DD HH:MM'")
    pr.add_argument("-t", "--transcribe", action="store_true",
                    help="transcribe automatically after recording")
    pr.add_argument("--language", help="transcription language hint (e.g. zh, en)")
    pr.add_argument("--engine", choices=["cloud", "local"],
                    help="engine for -t transcription (default: config 'engine')")
    pr.add_argument("--device", help="override audio device index/name")
    pr.add_argument("-q", "--quiet", action="store_true", help="suppress console logs")

    sub.add_parser("stop", help="stop the current recording")
    sub.add_parser("status", help="show whether a recording is running")

    pt = sub.add_parser("transcribe", help="transcribe an audio file")
    pt.add_argument("file", nargs="?", help="audio file (default: most recent recording)")
    pt.add_argument("-o", "--out", help="transcript output file (default: <audio>.txt)")
    pt.add_argument("--language", help="language hint (e.g. zh, en)")
    pt.add_argument("--engine", choices=["cloud", "local"],
                    help="transcription engine (default: config 'engine')")
    pt.add_argument("-q", "--quiet", action="store_true", help="suppress console logs")
    return p


HANDLERS = {
    "devices": cmd_devices,
    "config": cmd_config,
    "rec": cmd_rec,
    "stop": cmd_stop,
    "status": cmd_status,
    "transcribe": cmd_transcribe,
}


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return HANDLERS[args.command](args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
