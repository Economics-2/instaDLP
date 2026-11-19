# -*- coding: utf-8 -*-
"""
Insta — লোকাল সার্ভার (Windows)
তৈরি করেছেন: MH & MIK

Android অ্যাপের LocalServer.kt-এর Python সংস্করণ। একই API endpoint রাখা হয়েছে,
তাই web/index.html প্রায় হুবহু একই থাকে।

yt-dlp ও ffmpeg আলাদা .exe হিসেবে (bin/ ফোল্ডারে) বান্ডেল করা থাকে এবং
subprocess দিয়ে চালানো হয়। এতে "yt-dlp আপডেট" বাটন exe-র ভেতরেও কাজ করে।
"""
import contextlib
import functools
import hmac
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import uuid
import zipfile
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

APP_NAME = "Insta"
VERSION = "1.6.0"
IS_WIN = os.name == "nt"
PORT = 8765
MAX_PLAYLIST = 50
MAX_HISTORY = 2000
CREATE_NO_WINDOW = 0x08000000

# শুধু এই ID-র Chrome এক্সটেনশন অ্যাপে লিংক পাঠাতে পারবে (extension/manifest.json-এর "key" থেকে নির্ধারিত)
EXT_IDS = {"nppejnkjfkkdpafpjmnodkelfdalegla"} | set(filter(None, os.environ.get("INSTA_EXT_ID", "").split(",")))

LANG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{1,14}$")
ID_RE = re.compile(r"^[0-9a-f]{10}$")
ITEM_RE = re.compile(r"Downloading item (\d+) of (\d+)")
FMT_RE = re.compile(r"Downloading \d+ format\(s\): (\S+)")
SUB_EXTS = {"vtt", "srt", "ttml", "srv1", "srv2", "srv3", "json3", "ass", "lrc"}
PROGRESS_TEMPLATE = (
    "download:INSTA|%(progress.downloaded_bytes)s|%(progress.total_bytes)s|"
    "%(progress.total_bytes_estimate)s|%(progress.speed)s|%(progress.eta)s"
)


# ---------------------------------------------------------------- পাথ ও ফোল্ডার
def res(rel):
    """PyInstaller-এর ভেতরে ও বাইরে — দু'জায়গাতেই বান্ডেল করা ফাইলের পাথ।"""
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, rel)


def data_dir():
    ov = os.environ.get("INSTA_DATA_DIR")
    if ov:
        p = Path(ov)
    elif IS_WIN:
        p = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / APP_NAME
    else:
        p = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / APP_NAME
    p.mkdir(parents=True, exist_ok=True)
    return p


def _default_downloads_dir():
    """ব্যবহারকারীর আসল Downloads ফোল্ডার (সরানো থাকলেও) + /Insta।"""
    ov = os.environ.get("INSTA_DOWNLOAD_DIR")
    base = Path(ov) if ov else None
    if base is None and IS_WIN:
        try:
            import ctypes

            class GUID(ctypes.Structure):
                _fields_ = [("a", ctypes.c_ulong), ("b", ctypes.c_ushort),
                            ("c", ctypes.c_ushort), ("d", ctypes.c_ubyte * 8)]

            # FOLDERID_Downloads
            guid = GUID(0x374DE290, 0x123F, 0x4565,
                        (ctypes.c_ubyte * 8)(0x91, 0x64, 0x39, 0xC4, 0x92, 0x5E, 0x46, 0x7B))
            ptr = ctypes.c_wchar_p()
            if ctypes.windll.shell32.SHGetKnownFolderPath(
                    ctypes.byref(guid), 0, None, ctypes.byref(ptr)) == 0:
                base = Path(ptr.value)
                ctypes.windll.ole32.CoTaskMemFree(ptr)
        except Exception:
            base = None
    if base is None:
        base = Path.home() / "Downloads"
    d = base if ov else base / APP_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


YTDLP_NAME = "yt-dlp.exe" if IS_WIN else "yt-dlp"


def bundled_ytdlp():
    return res(os.path.join("bin", YTDLP_NAME))


def user_ytdlp():
    return str(data_dir() / "bin" / YTDLP_NAME)


def ytdlp_path():
    """আপডেট-করা কপি থাকলে সেটা, নইলে বান্ডেল করা কপি।"""
    ov = os.environ.get("INSTA_YTDLP")
    if ov:
        return ov
    u = user_ytdlp()
    return u if os.path.exists(u) else bundled_ytdlp()


def ffmpeg_dir():
    return os.environ.get("INSTA_FFMPEG_DIR") or res("bin")


# ---------------------------------------------------------------- subprocess সহায়ক
_PROCS = set()
_PROCS_LOCK = threading.Lock()


def _popen(args, **kw):
    # stdin/stdout/stderr সবসময় স্পষ্ট দিতে হয়: --noconsole অ্যাপে এগুলো থাকে না।
    kw.setdefault("stdin", subprocess.DEVNULL)
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    return subprocess.Popen(args, env=env, creationflags=CREATE_NO_WINDOW if IS_WIN else 0, **kw)


def kill_tree(p):
    try:
        if IS_WIN:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW, timeout=10)
        else:
            p.kill()
    except Exception:
        try:
            p.kill()
        except Exception:
            pass


def run_capture(args, timeout):
    """চালিয়ে (returncode, stdout, stderr) ফেরত দেয়।"""
    p = _popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    with _PROCS_LOCK:
        _PROCS.add(p)
    try:
        out, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_tree(p)
        p.communicate()
        raise
    finally:
        with _PROCS_LOCK:
            _PROCS.discard(p)
    return p.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


def kill_all():
    with _PROCS_LOCK:
        procs = list(_PROCS)
    for p in procs:
        kill_tree(p)


def tool_version(exe):
    try:
        rc, out, _ = run_capture([exe, "--version"], timeout=40)
        return out.strip().splitlines()[0] if rc == 0 and out.strip() else ""
    except Exception:
        return ""


# ---------------------------------------------------------------- প্রথমবার চালু
def init_binaries():
    """বান্ডেল করা yt-dlp.exe-র একটি কপি AppData-তে রাখে, যাতে আপডেট স্থায়ী হয়।"""
    if os.environ.get("INSTA_YTDLP"):
        return
    src = Path(bundled_ytdlp())
    if not src.exists():
        return
    dst = Path(user_ytdlp())
    dst.parent.mkdir(parents=True, exist_ok=True)
    if not dst.exists():
        shutil.copy2(src, dst)
        return

    def refresh():
        # নতুন Insta ভার্সনে নতুন yt-dlp থাকলে পুরোনো কপি বদলে দেয়।
        try:
            vb, vu = tool_version(str(src)), tool_version(str(dst))
            if vb and vu and vb > vu:
                tmp = dst.with_name(dst.name + ".new")
                shutil.copy2(src, tmp)
                os.replace(tmp, dst)
        except Exception:
            pass

    threading.Thread(target=refresh, daemon=True).start()


# ---------------------------------------------------------------- ছোট সহায়ক
class ApiError(Exception):
    def __init__(self, msg, status=400):
        super().__init__(msg)
        self.msg, self.status = msg, status


def valid_url(u):
    return bool(re.match(r"^https?://", u or "", re.I))


def sstr(o, key):
    v = o.get(key) if isinstance(o, dict) else None
    return v.strip() if isinstance(v, str) else ""


def fmt_eta(sec):
    if not sec or sec <= 0:
        return ""
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def fmt_speed(bps):
    if not bps or bps <= 0:
        return ""
    if bps >= 1048576:
        return f"{bps / 1048576:.1f} MB/s"
    return f"{bps / 1024:.0f} KB/s"


def num(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def iso_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- তথ্য আনা
def api_info(url):
    if not valid_url(url):
        raise ApiError("সঠিক লিংক দিন (http বা https দিয়ে শুরু)।")

    # ভিডিও লিংকে list= থাকলে ডিফল্টভাবে শুধু ওই ভিডিও ধরা হয়
    video_in_list = "list=" in url and ("v=" in url or "youtu.be/" in url)
    args = [ytdlp_path(), "-J", "--flat-playlist", "--playlist-end", str(MAX_PLAYLIST),
            "--no-warnings", "--encoding", "utf-8"]
    if video_in_list:
        args.append("--no-playlist")
    args += ["--", url]

    try:
        rc, out, _ = run_capture(args, timeout=120)
        d = json.loads(out)
        if not isinstance(d, dict):
            raise ValueError("bad json")
    except Exception:
        raise ApiError("তথ্য আনা যায়নি। লিংকটি ঠিক আছে কিনা এবং কনটেন্ট পাবলিক কিনা দেখুন।")

    if d.get("_type") == "playlist":
        entries = d.get("entries") or []
        total = d.get("playlist_count") or len(entries)
        thumb = ""
        if entries and isinstance(entries[0], dict):
            th = entries[0].get("thumbnails") or []
            thumb = (th[-1].get("url") if th and isinstance(th[-1], dict) else None) \
                or entries[0].get("thumbnail") or ""
        return {
            "kind": "playlist",
            "title": d.get("title") or "প্লেলিস্ট",
            "uploader": d.get("uploader") or d.get("channel") or "",
            "count": total,
            "limit": min(total, MAX_PLAYLIST),
            "thumbnail": thumb,
            "heights": [1080, 720, 480, 360],
            "subs": [{"lang": l, "auto": None} for l in ("bn", "en", "hi")],
            "has_ffmpeg": True,
        }

    heights = set()
    for f in d.get("formats") or []:
        if not isinstance(f, dict):
            continue
        h = f.get("height")
        if isinstance(h, (int, float)) and h > 0 and (f.get("vcodec") or "") != "none":
            heights.add(int(h))

    manual = [k for k in (d.get("subtitles") or {}) if k != "live_chat"]
    auto = d.get("automatic_captions") or {}
    subs = [{"lang": k, "auto": False} for k in manual[:8]]
    for l in ("bn", "en", "hi", "ar", "ur"):
        if l in auto and l not in manual:
            subs.append({"lang": l, "auto": True})

    return {
        "kind": "video",
        "title": d.get("title") or "শিরোনাম নেই",
        "thumbnail": d.get("thumbnail") or "",
        "duration": int(d.get("duration") or 0),
        "uploader": d.get("uploader") or "",
        "heights": sorted(heights, reverse=True)[:6],
        "subs": subs,
        "also_playlist": video_in_list,
        "limit": MAX_PLAYLIST,
        "has_ffmpeg": True,
    }


# ---------------------------------------------------------------- সেটিংস
SETTINGS_DEFAULTS = {
    "folder": "",          # খালি = ডিফল্ট (Downloads\Insta)
    "quality": "best",     # best | 1080 | 720 | 480 | 360 | audio (এক্সটেনশন থেকে আসা ডাউনলোডে ও অ্যাপে ডিফল্ট)
    "template": "title",
    "limit": 0.0,          # MB/s, ০ = সীমাহীন
    "parallel": 2,         # একসাথে সর্বোচ্চ কয়টি ডাউনলোড
    "post": "none",        # none | mp3 | compress | vertical
    "theme": "auto",
    "tray": True,          # জানালা বন্ধ করলে ট্রে-তে চলবে
    "autostart": False,    # Windows চালু হলে নিজে থেকে চালু
    "notify": True,        # ডাউনলোড শেষ হলে নোটিফিকেশন
    "clipboard": True,     # ভিডিও লিংক কপি করলে অ্যাপ নামানোর প্রস্তাব দেবে
    "whisper": "small",    # ট্রান্সক্রিপশন মডেল: tiny | base | small
}
QUALITIES = ("best", "1080", "720", "480", "360", "audio")
TEMPLATES = {
    "title": "%(title).80s",
    "uploader_title": "%(uploader).40s - %(title).80s",
    "title_id": "%(title).70s [%(id)s]",
}
POSTS = ("none", "mp3", "compress", "vertical")
THEMES = ("auto", "light", "dark")
_SET_LOCK = threading.Lock()
_SET_CACHE = {"d": None}


def clean_settings(raw, base):
    """আসা মানগুলো যাচাই করে base-এর ওপর বসায়; ভুল মান বাদ যায়।"""
    out = dict(base)
    if raw.get("quality") in QUALITIES:
        out["quality"] = raw["quality"]
    if raw.get("template") in TEMPLATES:
        out["template"] = raw["template"]
    if raw.get("post") in POSTS:
        out["post"] = raw["post"]
    if raw.get("theme") in THEMES:
        out["theme"] = raw["theme"]
    p = raw.get("parallel")
    if isinstance(p, (int, float)) and not isinstance(p, bool) and p == p:
        out["parallel"] = max(1, min(5, int(p)))
    l = raw.get("limit")
    if isinstance(l, (int, float)) and not isinstance(l, bool) and l == l:
        out["limit"] = round(max(0.0, min(1000.0, float(l))), 1)
    if raw.get("whisper") in WHISPER_NAMES:
        out["whisper"] = raw["whisper"]
    for k in ("tray", "autostart", "notify", "clipboard"):
        if isinstance(raw.get(k), bool):
            out[k] = raw[k]
    if isinstance(raw.get("folder"), str):
        out["folder"] = raw["folder"].strip()[:500]
    return out


def settings():
    with _SET_LOCK:
        if _SET_CACHE["d"] is None:
            raw = {}
            try:
                raw = json.loads((data_dir() / "settings.json").read_text(encoding="utf-8"))
            except Exception:
                pass
            _SET_CACHE["d"] = clean_settings(raw if isinstance(raw, dict) else {}, SETTINGS_DEFAULTS)
        return dict(_SET_CACHE["d"])


def downloads_dir():
    """সেটিংসে ফোল্ডার দেওয়া থাকলে সেটা, নইলে ডিফল্ট (Downloads\\Insta)।"""
    custom = settings()["folder"]
    if custom:
        try:
            d = Path(custom)
            d.mkdir(parents=True, exist_ok=True)
            return d
        except Exception:
            pass   # ড্রাইভ খুলে নেওয়া হলে ডিফল্টে ফিরে যায়
    return _default_downloads_dir()


def check_folder(path):
    p = Path(path)
    if not p.is_absolute():
        raise ApiError("পুরো পাথ দিন (যেমন D:\\Videos\\Insta)।")
    try:
        p.mkdir(parents=True, exist_ok=True)
        t = p / (".insta-write-test-" + uuid.uuid4().hex[:6])
        t.write_text("x")
        t.unlink()
    except Exception:
        raise ApiError("এই ফোল্ডারে ফাইল সেভ করা যাচ্ছে না। অন্য ফোল্ডার বেছে নিন।")


def set_autostart(on):
    """Windows চালুর সময় Insta ট্রে-তে চালু হওয়ার ব্যবস্থা (শুধু বিল্ড করা exe-তে)।"""
    if not IS_WIN or not getattr(sys, "frozen", False):
        return False
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_SET_VALUE)
        try:
            if on:
                winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, f'"{sys.executable}" --minimized')
            else:
                try:
                    winreg.DeleteValue(key, APP_NAME)
                except FileNotFoundError:
                    pass
        finally:
            winreg.CloseKey(key)
        return True
    except Exception:
        return False


def api_settings_get():
    s = settings()
    s["default_folder"] = str(_default_downloads_dir())
    s["can_autostart"] = IS_WIN and bool(getattr(sys, "frozen", False))
    s["is_windows"] = IS_WIN
    s["version"] = VERSION
    return s


def api_settings_save(b):
    old = settings()
    new = clean_settings(b if isinstance(b, dict) else {}, old)
    if new["folder"] and new["folder"] != old["folder"]:
        check_folder(new["folder"])
    with _SET_LOCK:
        f = data_dir() / "settings.json"
        tmp = f.with_name(f.name + ".tmp")
        tmp.write_text(json.dumps(new, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, f)
        _SET_CACHE["d"] = new
    if new["autostart"] != old["autostart"]:
        set_autostart(new["autostart"])
    pump()   # একসাথে চলার সংখ্যা বাড়ালে অপেক্ষমাণ কাজ সাথে সাথে চালু হয়
    return api_settings_get()


# main.py নোটিফিকেশন দেখানোর ফাংশন এখানে বসায় (ট্রে আইকন দিয়ে)
NOTIFY = [None]


def notify(title, text):
    cb = NOTIFY[0]
    if cb and settings()["notify"]:
        try:
            cb(title, text)
        except Exception:
            pass


# ---------------------------------------------------------------- ডাউনলোড (কিউ)
TERMINAL = ("done", "error", "cancelled")
POST_LABEL = {"mp3": "MP3 বের করা হচ্ছে", "compress": "ছোট সাইজে কমপ্রেস হচ্ছে",
              "vertical": "৯:১৬ ভার্টিক্যাল তৈরি হচ্ছে"}
VIDEO_EXTS = {".mp4", ".mkv", ".webm", ".mov", ".m4v", ".avi", ".flv"}


class _Stopped(Exception):
    """থামানো বা বাতিল করা হয়েছে।"""


class Job:
    def __init__(self, url, kind, height, playlist, subs, title, source="app", cookies=None, post="none", section=""):
        self.section = section    # শুধু একটি অংশ নামানো (yt-dlp --download-sections)
        self.url, self.kind, self.height = url, kind, height
        self.playlist, self.subs, self.title = playlist, subs, title
        self.source = source      # "app" = অ্যাপের নিজের UI, "ext" = ব্রাউজার এক্সটেনশন
        self.cookies, self.post = cookies, post
        self.seq = 0
        self.status = "queued"    # queued | downloading | processing | paused | done | error | cancelled
        self.running = False      # থ্রেড চলছে কিনা
        self.flag = ""            # "pause" | "cancel" (চলন্ত কাজকে থামানোর নির্দেশ)
        self.proc = None
        self.dismissed = False
        self.percent = 0.0
        self.speed = ""
        self.eta = ""
        self.index = 1
        self.total = 1
        self.error = ""
        self.warning = ""
        self.stage = ""
        self.streams = 1   # একটি ভিডিওর জন্য কয়টি স্ট্রিম (ভিডিও+অডিও = ২)
        self.stream = 0    # এই পর্যন্ত কয়টি স্ট্রিম শুরু হয়েছে
        self.in_sub = False

    def to_dict(self):
        return {"status": self.status, "percent": round(self.percent, 1), "speed": self.speed,
                "eta": self.eta, "index": self.index, "total": self.total, "error": self.error,
                "warning": self.warning, "stage": self.stage}


JOBS = {}
_Q_LOCK = threading.RLock()
_SEQ = [0]
_HIST_LOCK = threading.Lock()


def active_count():
    with _Q_LOCK:
        return sum(1 for j in JOBS.values() if j.running)


def pending_count():
    """চলছে বা লাইনে অপেক্ষা করছে এমন কাজ।"""
    with _Q_LOCK:
        return sum(1 for j in JOBS.values() if j.running or j.status == "queued")


def pump():
    """কিউ থেকে পরের কাজ চালু করে (একসাথে সর্বোচ্চ 'parallel'টি)।"""
    with _Q_LOCK:
        limit = settings()["parallel"]
        running = sum(1 for j in JOBS.values() if j.running)
        for jid, j in sorted(JOBS.items(), key=lambda kv: kv[1].seq):
            if running >= limit:
                break
            if j.status == "queued" and not j.running:
                j.running = True
                j.flag = ""
                j.status = "downloading"
                threading.Thread(target=run_download, args=(jid,), daemon=True).start()
                running += 1


def pick_format(kind, height):
    if kind == "audio":
        return "bestaudio/best"
    h = f"[height<={height}]" if height else ""
    return f"bestvideo{h}+bestaudio/best{h}/best"


def out_template(playlist):
    base = TEMPLATES.get(settings()["template"], TEMPLATES["title"])
    return ("%(playlist_index)03d_" if playlist else "") + base + ".%(ext)s"


def on_line(job, line):
    m = ITEM_RE.search(line)
    if m:
        job.index = int(m.group(1))
        job.total = min(int(m.group(2)), MAX_PLAYLIST)
        job.stream = 0
        return
    m = FMT_RE.search(line)
    if m:
        job.streams = max(1, m.group(1).count("+") + 1)
        return
    if line.startswith("[download] Destination:"):
        ext = line.rsplit(".", 1)[-1].strip().lower()
        job.in_sub = ext in SUB_EXTS
        if not job.in_sub:
            job.stream += 1
        return
    if not line.startswith("INSTA|") or job.in_sub:
        return
    p = line.split("|")
    if len(p) < 6:
        return
    done, total = num(p[1]), num(p[2]) or num(p[3])
    pct = max(0.0, min(100.0, done / total * 100)) if done is not None and total else 0.0
    job.speed = fmt_speed(num(p[4]))
    job.eta = fmt_eta(num(p[5]))
    within = (max(job.stream, 1) - 1 + pct / 100) / max(job.streams, 1)
    job.percent = ((job.index - 1) + min(within, 1.0)) / max(job.total, 1) * 100


def run_tracked(job, args):
    """ffmpeg-র মতো সহায়ক প্রসেস চালায়; থামানো/বাতিল হলে _Stopped ছোড়ে।"""
    p = _popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    job.proc = p
    with _PROCS_LOCK:
        _PROCS.add(p)
    try:
        if job.flag:
            kill_tree(p)
        rc = p.wait()
    finally:
        with _PROCS_LOCK:
            _PROCS.discard(p)
        job.proc = None
    if job.flag:
        raise _Stopped()
    return rc


def post_process(job, tmp):
    """ডাউনলোড শেষে ffmpeg দিয়ে অটো কনভার্ট: mp3 | compress | vertical।"""
    if job.post == "none" or job.kind == "audio":
        return
    ff = os.path.join(ffmpeg_dir(), "ffmpeg.exe" if IS_WIN else "ffmpeg")
    if not os.path.exists(ff):
        job.warning = "ffmpeg পাওয়া যায়নি, তাই কনভার্ট করা হয়নি।"
        return
    vids = sorted(f for f in tmp.iterdir() if f.is_file() and f.suffix.lower() in VIDEO_EXTS)
    fails = 0
    for i, f in enumerate(vids, 1):
        if job.flag:
            raise _Stopped()
        job.stage = POST_LABEL[job.post] + (f" ({i}/{len(vids)})" if len(vids) > 1 else "") + "…"
        replace = False
        if job.post == "mp3":
            out = f.with_suffix(".mp3")
            cmd = [ff, "-y", "-loglevel", "error", "-i", str(f), "-vn",
                   "-c:a", "libmp3lame", "-b:a", "192k", str(out)]
        elif job.post == "compress":
            out = f.with_name(f.stem + " (ছোট).mp4")
            replace = True   # আসল ফাইলের বদলে ছোট ফাইলটিই রাখা হয়
            cmd = [ff, "-y", "-loglevel", "error", "-i", str(f),
                   "-vf", "scale=-2:'min(720,ih)'", "-c:v", "libx264", "-preset", "veryfast",
                   "-crf", "28", "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(out)]
        else:   # vertical: ঝাপসা ব্যাকগ্রাউন্ডসহ ১০৮০×১৯২০ (পুরো ছবি ঠিক থাকে, কাটা পড়ে না)
            out = f.with_name(f.stem + " (৯x১৬).mp4")
            fg = ("split[a][b];[a]scale=1080:1920:force_original_aspect_ratio=increase,"
                  "crop=1080:1920,boxblur=24:2[bg];[b]scale=1080:1920:force_original_aspect_ratio=decrease[fg];"
                  "[bg][fg]overlay=(W-w)/2:(H-h)/2")
            cmd = [ff, "-y", "-loglevel", "error", "-i", str(f), "-filter_complex", fg,
                   "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-c:a", "aac",
                   "-b:a", "128k", "-movflags", "+faststart", str(out)]
        try:
            rc = run_tracked(job, cmd)
        except _Stopped:
            raise
        except Exception:
            rc = -1
        if rc == 0 and out.exists() and out.stat().st_size > 0:
            if replace:
                f.unlink()
        else:
            fails += 1
            try:
                out.unlink()
            except OSError:
                pass
    job.stage = ""
    if fails:
        job.warning = "কিছু ফাইল কনভার্ট করা যায়নি, আসল ফাইল রাখা হয়েছে।"


def collect_output(tmp, title, zip_it):
    """একাধিক ফাইল হলে (প্লেলিস্ট/সাবটাইটেল) জিপ করে একটি ফাইল; নইলে ফাইলগুলো আলাদাই থাকে।"""
    files = sorted((f for f in tmp.iterdir()
                    if f.is_file() and f.suffix.lower() not in (".part", ".ytdl", ".temp")),
                   key=lambda f: f.name)
    if not files:
        return []
    if len(files) == 1 or not zip_it:
        return files
    safe = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", title).strip(" .")[:80] or "download"
    zp = tmp / f"{safe}.zip"
    with zipfile.ZipFile(zp, "w", zipfile.ZIP_STORED) as z:  # ভিডিও আগেই কমপ্রেসড
        for f in files:
            z.write(f, f.name)
    for f in files:
        f.unlink()
    return [zp]


def move_to_downloads(src):
    d = downloads_dir()
    dst = d / src.name
    n = 1
    while dst.exists():
        dst = d / f"{src.stem} ({n}){src.suffix}"
        n += 1
    shutil.move(str(src), str(dst))
    return dst


def write_error_log(lines):
    try:
        logs = data_dir() / "logs"
        logs.mkdir(exist_ok=True)
        (logs / "last-error.txt").write_text("\n".join(lines), encoding="utf-8")
    except Exception:
        pass


def run_download(job_id):
    job = JOBS[job_id]
    tmp = data_dir() / "tmp" / job_id
    tmp.mkdir(parents=True, exist_ok=True)
    proc = None
    tail = []
    cookie_file = None
    keep_tmp = False
    try:
        if job.flag:
            raise _Stopped()
        job.stream, job.in_sub = 0, False
        job.stage = job.warning = job.error = ""
        s = settings()
        args = [ytdlp_path(), "--newline", "--no-colors", "--encoding", "utf-8",
                "--windows-filenames", "--no-mtime", "-N", "4",
                "--progress-template", PROGRESS_TEMPLATE]
        ff = ffmpeg_dir()
        if os.path.isdir(ff):
            args += ["--ffmpeg-location", ff]
        if s["limit"] > 0:
            args += ["--limit-rate", f"{s['limit']:g}M"]
        if job.cookies:
            cookie_file = write_cookie_file(job_id, job.cookies)
            args += ["--cookies", str(cookie_file)]
        if job.playlist:
            args += ["--yes-playlist", "--playlist-end", str(MAX_PLAYLIST),
                     "--ignore-errors"]  # একটি ভিডিও ব্যর্থ হলেও বাকিগুলো চলবে
        else:
            args += ["--no-playlist"]

        args += ["-f", pick_format(job.kind, job.height)]
        if job.kind == "audio":
            args += ["-x", "--audio-format", "mp3", "--audio-quality", "192K"]
        else:
            args += ["-S", "ext:mp4:m4a", "--merge-output-format", "mp4"]

        subs = job.subs
        if subs:
            if any(a is not True for _, a in subs):
                args.append("--write-subs")
            if any(a is not False for _, a in subs):
                args.append("--write-auto-subs")
            args += ["--sub-langs", ",".join(l for l, _ in subs),
                     "--sub-format", "srt/vtt/best", "--convert-subs", "srt"]

        if job.section and not job.playlist:
            args += ["--download-sections", job.section, "--force-keyframes-at-cuts"]

        args += ["-o", str(tmp / out_template(job.playlist)), "--", job.url]

        proc = _popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                      text=True, encoding="utf-8", errors="replace", bufsize=1)
        job.proc = proc
        with _PROCS_LOCK:
            _PROCS.add(proc)
        if job.flag:
            kill_tree(proc)
        for raw in proc.stdout:
            line = raw.rstrip("\r\n")
            tail.append(line)
            del tail[:-60]
            on_line(job, line)
        rc = proc.wait()
        if job.flag:
            raise _Stopped()

        has_files = any(f.is_file() and f.suffix.lower() not in (".part", ".ytdl", ".temp")
                        for f in tmp.iterdir())
        # প্লেলিস্টে কিছু ভিডিও নামলে সেগুলো রাখা হয়, কিছুই না নামলে ব্যর্থ ধরা হয়
        if rc != 0 and (not job.playlist or not has_files):
            raise RuntimeError(f"yt-dlp exit code {rc}")

        job.status = "processing"
        job.percent = 100.0
        job.speed = job.eta = ""
        post_process(job, tmp)
        outs = collect_output(tmp, job.title, job.playlist or bool(subs))
        if not outs:
            raise RuntimeError("no output files")
        main_src = max(outs, key=lambda f: f.stat().st_size)
        finals = [move_to_downloads(f) for f in outs]
        main = finals[outs.index(main_src)]
        title = job.title
        if job.source in ("ext", "batch", "follow") and main_src.suffix.lower() != ".zip":
            title = main_src.stem   # এক্সটেনশন/ব্যাচ থেকে এলে আসল শিরোনাম ফাইলের নাম থেকে (" (১)" যোগ হওয়ার আগের নাম)
        job.title = title

        add_history({
            "id": job_id, "title": title, "url": job.url,
            "kind": "playlist" if job.playlist else job.kind,
            "quality": "audio" if job.kind == "audio" else (f"{job.height}p" if job.height else "best"),
            "subs": [l for l, _ in subs], "name": main.name, "path": str(main),
            "size": main.stat().st_size, "time": iso_now(),
        })
        job.stage = ""
        job.status = "done"
        notify("ডাউনলোড সম্পূর্ণ", title)
    except _Stopped:
        if job.flag == "pause":
            job.status = "paused"
            keep_tmp = True   # আংশিক ফাইল (.part) থাকে, চালু করলে সেখান থেকেই চলবে
        else:
            job.status = "cancelled"
        job.speed = job.eta = job.stage = ""
    except Exception as e:
        job.status = "error"
        job.stage = ""
        job.error = "ডাউনলোড ব্যর্থ হয়েছে। অন্য কোয়ালিটি বেছে আবার চেষ্টা করুন।"
        write_error_log([f"url: {job.url}", f"error: {e!r}", "--- yt-dlp output (শেষ অংশ) ---"] + tail)
        notify("ডাউনলোড ব্যর্থ", job.title)
    finally:
        if proc is not None:
            with _PROCS_LOCK:
                _PROCS.discard(proc)
        if not keep_tmp:
            shutil.rmtree(tmp, ignore_errors=True)
        if cookie_file is not None:
            try:
                cookie_file.unlink()
            except OSError:
                pass
        with _Q_LOCK:
            job.running = False
            job.proc = None
            job.flag = ""
        pump()


def start_job(url, kind, height, playlist, subs, title, source="app", cookies=None, post="none", section=""):
    job_id = uuid.uuid4().hex[:10]
    with _Q_LOCK:
        for k in [k for k, j in JOBS.items() if j.dismissed and j.status in TERMINAL]:
            del JOBS[k]   # সরিয়ে দেওয়া তালিকা মেমরি থেকে মুছে ফেলা
        _SEQ[0] += 1
        j = Job(url, kind, height, playlist, subs, title, source, cookies, post, section)
        j.seq = _SEQ[0]
        JOBS[job_id] = j
    pump()
    return job_id


def api_download(b):
    url = sstr(b, "url")
    if not valid_url(url):
        raise ApiError("সঠিক লিংক দিন।")
    kind = "audio" if sstr(b, "kind") == "audio" else "video"
    h = b.get("height")
    height = int(h) if (isinstance(h, (str, int)) and str(h).isdigit() and 0 < int(h) < 10000) else None
    playlist = bool(b.get("playlist"))
    title = sstr(b, "title") or "download"
    post = sstr(b, "post")
    post = post if post in POSTS else "none"
    section = "" if playlist else parse_section(sstr(b, "start"), sstr(b, "end"))

    subs = []
    arr = b.get("subs") if isinstance(b.get("subs"), list) else []
    for o in arr[:10]:
        if not isinstance(o, dict):
            continue
        lang = sstr(o, "lang")
        if not LANG_RE.match(lang):
            continue
        a = o.get("auto")
        subs.append((lang, a if isinstance(a, bool) else None))

    return {"job": start_job(url, kind, height, playlist, subs, title, "app", None, post, section)}


def api_status(job_id):
    job = JOBS.get(job_id)
    if job is None:
        raise ApiError("কাজটি পাওয়া যায়নি।", 404)
    return job.to_dict()


# ---------------------------------------------------------------- অংশ কাটা (শুরু/শেষ সময়)
_BN_DIGITS = str.maketrans("০১২৩৪৫৬৭৮৯", "0123456789")
TIME_RE = re.compile(r"^\d{1,5}(?::\d{1,2}){0,2}(?:\.\d{1,3})?$")


def parse_time(text):
    """'90', '1:30', '1:02:03', '১:৩০' (বাংলা অঙ্কও চলে) -> সেকেন্ড। খালি হলে None।"""
    t = (text or "").strip().translate(_BN_DIGITS).replace("।", ".")
    if not t:
        return None
    if not TIME_RE.match(t):
        raise ApiError("সময় এভাবে লিখুন: ৯০ (সেকেন্ড), ১:৩০ বা ১:০২:০৩।")
    secs = 0.0
    for part in t.split(":"):
        secs = secs * 60 + float(part)
    return secs


def parse_section(start, end):
    """yt-dlp-র --download-sections মান ('*90-150'); দুটোই খালি হলে ''।"""
    a, b = parse_time(start), parse_time(end)
    if a is None and b is None:
        return ""
    a = a or 0.0
    if b is not None and b <= a:
        raise ApiError("শেষের সময় শুরুর সময়ের পরে হতে হবে।")
    return f"*{a:g}-{'inf' if b is None else format(b, 'g')}"


# ---------------------------------------------------------------- ব্যাচ ডাউনলোড
def api_batch(b):
    """একসাথে অনেক লিংক (প্রতি লাইনে একটি) কিউতে যোগ করে।"""
    raw = b.get("urls")
    if isinstance(raw, str):
        raw = raw.splitlines()
    if not isinstance(raw, list):
        raise ApiError("লিংকের তালিকা দিন।")
    urls, seen, invalid = [], set(), 0
    for x in raw:
        if not isinstance(x, str) or not x.strip():
            continue
        u = x.strip()
        if not valid_url(u) or len(u) > 2000 or re.search(r"\s", u):
            invalid += 1
            continue
        if u not in seen:
            seen.add(u)
            urls.append(u)
    if not urls:
        raise ApiError("কোনো সঠিক লিংক পাওয়া যায়নি। প্রতি লাইনে একটি করে লিংক দিন।")
    if len(urls) > 50:
        raise ApiError("একসাথে সর্বোচ্চ ৫০টি লিংক দেওয়া যাবে।")
    s = settings()
    q = b.get("quality") if b.get("quality") in QUALITIES else s["quality"]
    post = b.get("post") if b.get("post") in POSTS else s["post"]
    kind = "audio" if q == "audio" else "video"
    height = int(q) if q.isdigit() else None
    playlist = bool(b.get("playlist"))
    ids = [start_job(u, kind, height, playlist, [], u[:90], "batch", None, post) for u in urls]
    return {"added": len(ids), "invalid": invalid, "jobs": ids}


# ---------------------------------------------------------------- ক্লিপবোর্ডে ভিডিও লিংক ধরা
CLIP_RE = re.compile(
    r"^https?://(?:[\w-]+\.)*(?:"
    r"youtube\.com/(?:watch\?|shorts/|live/|playlist\?)|youtu\.be/|"
    r"facebook\.com/(?:watch|reel|share/[vr]/|video\.php|[^/\s]+/videos/)|fb\.watch/|"
    r"instagram\.com/(?:[^/\s]+/)?(?:reel|reels|p|tv)/)", re.I)
_CLIP = {"url": None}


def clip_offer(text):
    """কপি করা লেখা সমর্থিত ভিডিও লিংক হলে ব্যানারে দেখানোর জন্য রাখে। অন্য কিছু মনে রাখা হয় না।"""
    if not settings()["clipboard"]:
        return False
    t = (text or "").strip()
    if not t or len(t) > 500 or re.search(r"\s", t) or not CLIP_RE.match(t):
        return False
    with _Q_LOCK:
        if any(j.url == t and j.status in ("queued", "downloading", "processing", "paused")
               for j in JOBS.values()):
            return False   # এটি আগেই নামছে
    _CLIP["url"] = t
    return True


def api_clipboard_get():
    return {"url": _CLIP["url"] if settings()["clipboard"] else None}


def api_clipboard_clear():
    _CLIP["url"] = None
    return {"ok": True}


def _clip_text():
    """Windows ক্লিপবোর্ডের লেখা (ctypes দিয়ে)।"""
    import ctypes
    u, k = ctypes.windll.user32, ctypes.windll.kernel32
    u.OpenClipboard.argtypes = [ctypes.c_void_p]
    u.GetClipboardData.argtypes = [ctypes.c_uint]
    u.GetClipboardData.restype = ctypes.c_void_p
    k.GlobalLock.argtypes = [ctypes.c_void_p]
    k.GlobalLock.restype = ctypes.c_void_p
    k.GlobalUnlock.argtypes = [ctypes.c_void_p]
    if not u.OpenClipboard(None):
        return None
    try:
        h = u.GetClipboardData(13)   # CF_UNICODETEXT
        if not h:
            return None
        p = k.GlobalLock(h)
        if not p:
            return None
        try:
            return ctypes.wstring_at(p)
        finally:
            k.GlobalUnlock(h)
    finally:
        u.CloseClipboard()


def _clip_loop():
    import ctypes
    u = ctypes.windll.user32
    u.GetClipboardSequenceNumber.restype = ctypes.c_uint32
    seq = u.GetClipboardSequenceNumber()   # অ্যাপ চালুর সময়ের লেখা ধরা হয় না
    while True:
        time.sleep(0.8)
        try:
            n = u.GetClipboardSequenceNumber()
            if n != seq:
                seq = n
                if settings()["clipboard"]:
                    clip_offer(_clip_text())
        except Exception:
            pass


def api_jobs():
    with _Q_LOCK:
        items = [(j.seq, dict(j.to_dict(), id=jid, title=j.title, source=j.source,
                              kind=j.kind, post=j.post, clip=bool(j.section)))
                 for jid, j in JOBS.items() if not j.dismissed]
    items.sort(key=lambda x: -x[0])
    return [d for _, d in items[:30]]


def dismiss_job(job_id):
    with _Q_LOCK:
        j = JOBS.get(job_id)
        if j is not None and j.status in TERMINAL:
            j.dismissed = True


def api_job_action(job_id, action):
    to_kill = None
    with _Q_LOCK:
        j = JOBS.get(job_id)
        if j is None:
            raise ApiError("কাজটি পাওয়া যায়নি।", 404)
        if action == "pause":
            if j.status == "queued":
                j.status = "paused"            # এখনো শুরু হয়নি, শুধু ধরে রাখা
            elif j.status == "downloading" and j.running:
                j.flag, to_kill = "pause", j.proc
            else:
                raise ApiError("এখন থামানো যাবে না।")
        elif action == "resume":
            if j.status != "paused":
                raise ApiError("কাজটি থামানো নেই।")
            j.status = "queued"
        elif action == "cancel":
            if j.running:
                j.flag, to_kill = "cancel", j.proc
            elif j.status in ("queued", "paused"):
                j.status = "cancelled"
                shutil.rmtree(data_dir() / "tmp" / job_id, ignore_errors=True)
            else:
                raise ApiError("এই কাজ বাতিল করা যাবে না।")
        elif action == "retry":
            if j.status not in ("error", "cancelled"):
                raise ApiError("আবার চেষ্টা করা যাবে না।")
            _SEQ[0] += 1
            j.seq = _SEQ[0]
            j.status, j.percent, j.speed, j.eta = "queued", 0.0, "", ""
            j.index, j.total, j.error, j.warning, j.stage = 1, 1, "", "", ""
        else:
            raise ApiError("ভুল অনুরোধ।")
    if to_kill is not None:
        kill_tree(to_kill)
    pump()
    return {"ok": True}


def api_ext_send(b):
    """এক্সটেনশন থেকে লিংক এলে সেটিংসের ডিফল্ট কোয়ালিটিতে (সাধারণত সেরা) সাথে সাথে কিউতে ঢোকে।"""
    url = sstr(b, "url")
    if not valid_url(url) or len(url) > 2000:
        raise ApiError("সঠিক লিংক নয়।")
    with _Q_LOCK:
        for jid, j in JOBS.items():   # একই লিংকে দু'বার চাপলে নতুন কাজ হবে না
            if (j.source == "ext" and j.url == url and not j.dismissed
                    and j.status in ("queued", "downloading", "processing", "paused")):
                return {"ok": True, "job": jid, "duplicate": True}
    s = settings()
    q = sstr(b, "quality")
    if q not in QUALITIES:   # এক্সটেনশনের মেনু থেকে কোয়ালিটি না এলে সেটিংসের ডিফল্ট
        q = s["quality"]
    kind = "audio" if q == "audio" else "video"
    height = int(q) if q.isdigit() else None
    title = sstr(b, "title")[:150] or "Insta ডাউনলোড"
    cookies = clean_cookies(b.get("cookies"))
    return {"ok": True, "job": start_job(url, kind, height, False, [], title, "ext", cookies, s["post"])}


# ---------------------------------------------------------------- ব্রাউজার এক্সটেনশন
def clean_cookies(raw):
    """এক্সটেনশন থেকে আসা কুকি যাচাই করে (ঐচ্ছিক, লগইন-লাগা ভিডিওর জন্য)।"""
    out = []
    if not isinstance(raw, list):
        return out
    bad = ("\t", "\r", "\n", "\x00")
    for c in raw[:400]:
        if not isinstance(c, dict):
            continue
        name, value, domain, path = (c.get("name"), c.get("value"), c.get("domain"), c.get("path"))
        if not all(isinstance(x, str) for x in (name, value, domain, path)):
            continue
        if not name or not re.match(r"^\.?[A-Za-z0-9.-]+$", domain) or not path.startswith("/"):
            continue
        if any(ch in x for x in (name, value, path) for ch in bad):
            continue
        exp = c.get("expirationDate")
        out.append((domain, path, bool(c.get("secure")), bool(c.get("httpOnly")),
                    int(exp) if isinstance(exp, (int, float)) and exp > 0 else 0, name, value))
    return out


def write_cookie_file(job_id, cookies):
    p = data_dir() / "tmp" / f"{job_id}.cookies.txt"
    p.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# Netscape HTTP Cookie File"]
    for domain, path, secure, httponly, exp, name, value in cookies:
        lines.append("\t".join([("#HttpOnly_" if httponly else "") + domain,
                                "TRUE" if domain.startswith(".") else "FALSE", path,
                                "TRUE" if secure else "FALSE", str(exp), name, value]))
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def api_extension():
    """বান্ডেল করা এক্সটেনশন AppData-তে কপি করে ফোল্ডার খোলে।"""
    src = Path(res("extension"))
    if not src.is_dir():
        raise ApiError("এক্সটেনশন ফোল্ডার পাওয়া যায়নি।", 404)
    dst = data_dir() / "extension"
    shutil.copytree(src, dst, dirs_exist_ok=True)
    try:
        if IS_WIN:
            os.startfile(str(dst))  # noqa
        else:
            _popen(["xdg-open", str(dst)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass
    return {"path": str(dst)}


def ext_origin_ok(origin):
    pre = "chrome-extension://"
    return origin.startswith(pre) and origin[len(pre):].rstrip("/") in EXT_IDS


# ---------------------------------------------------------------- yt-dlp আপডেট
def api_update():
    if pending_count() > 0:
        raise ApiError("ডাউনলোড চলাকালীন আপডেট করা যাবে না। শেষ হলে আবার চেষ্টা করুন।")
    exe = ytdlp_path()
    before = tool_version(exe)
    try:
        rc, out, _ = run_capture([exe, "-U", "--encoding", "utf-8"], timeout=300)
    except Exception:
        raise ApiError("আপডেট করা যায়নি। ইন্টারনেট সংযোগ দেখুন।")
    after = tool_version(exe)
    if rc != 0 and after == before:
        raise ApiError("আপডেট করা যায়নি। ইন্টারনেট সংযোগ দেখুন।")
    changed = bool(after) and after != before
    return {"status": "DONE" if changed or "Updated yt-dlp" in out else "ALREADY_UP_TO_DATE",
            "version": after or before}


# ---------------------------------------------------------------- ইতিহাস
def _hist_file():
    return data_dir() / "history.json"


def _read_history():
    try:
        d = json.loads(_hist_file().read_text(encoding="utf-8"))
        return d if isinstance(d, list) else []
    except Exception:
        return []


def _write_history(items):
    f = _hist_file()
    tmp = f.with_name(f.name + ".tmp")
    tmp.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, f)


def api_history():
    with _HIST_LOCK:
        items = _read_history()
    for it in items:
        p = it.get("path")
        it["exists"] = bool(p) and os.path.exists(p)
    return items


def add_history(item):
    with _HIST_LOCK:
        _write_history(([item] + _read_history())[:MAX_HISTORY])


def remove_history(item_id):
    with _HIST_LOCK:
        if item_id is None:
            _write_history([])
        else:
            _write_history([x for x in _read_history() if x.get("id") != item_id])


def api_reveal(b):
    item_id = sstr(b, "id")
    if not ID_RE.match(item_id):
        raise ApiError("ভুল আইডি।")
    with _HIST_LOCK:
        item = next((x for x in _read_history() if x.get("id") == item_id), None)
    path = (item or {}).get("path")
    if not path:
        raise ApiError("ফাইলটি পাওয়া যায়নি।", 404)
    # পাথ আসে ইতিহাস ফাইল থেকে (অনুরোধ থেকে নয়), তাই নিরাপদ; ফোল্ডার বদলালেও পুরোনো ফাইল দেখানো যায়
    root = str(downloads_dir())
    try:
        if IS_WIN:
            if os.path.exists(path):
                subprocess.Popen(f'explorer /select,"{path}"', creationflags=CREATE_NO_WINDOW,
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
            else:
                os.startfile(root)  # noqa
        else:
            _popen(["xdg-open", root], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        raise ApiError("ফোল্ডার খোলা যায়নি।")
    return {"ok": True}


# ---------------------------------------------------------------- লাইব্রেরি ও সার্চ
AUDIO_EXTS = {".mp3", ".m4a", ".opus", ".ogg", ".wav", ".aac", ".flac"}
MEDIA_EXTS = VIDEO_EXTS | AUDIO_EXTS | {".zip"}
LIB_KINDS = ("all", "video", "audio", "playlist", "tx", "missing")
LIB_SORTS = ("new", "old", "size", "name")


def _find_item(item_id):
    if not isinstance(item_id, str) or not ID_RE.match(item_id):
        raise ApiError("ভুল আইডি।")
    with _HIST_LOCK:
        item = next((x for x in _read_history() if x.get("id") == item_id), None)
    if not item:
        raise ApiError("তালিকায় আইটেমটি পাওয়া যায়নি।", 404)
    return item


def _tx_file(it, what="txt"):
    p = (it.get("tx") or {}).get(what)
    return p if p and os.path.isfile(p) else ""


def _tx_text(it):
    p = _tx_file(it, "txt")
    if not p:
        return ""
    try:
        return Path(p).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _snippet(text, q):
    i = text.lower().find(q)
    if i < 0:
        return ""
    a, b = max(0, i - 40), min(len(text), i + len(q) + 70)
    return ("…" if a else "") + " ".join(text[a:b].split()) + ("…" if b < len(text) else "")


def api_library(params):
    """সার্চ (শিরোনাম, ফাইলের নাম, লিংক ও ট্রান্সক্রিপ্টের ভেতরের লেখা), ফিল্টার ও সাজানো।"""
    def one(k, default=""):
        v = params.get(k) or [default]
        return v[0] if v else default
    q = one("q").strip().lower()[:100]
    kind = one("kind", "all")
    sort = one("sort", "new")
    kind = kind if kind in LIB_KINDS else "all"
    sort = sort if sort in LIB_SORTS else "new"
    with _HIST_LOCK:
        items = _read_history()
    out, total_size = [], 0
    for it in items:
        p = it.get("path") or ""
        it["exists"] = bool(p) and os.path.exists(p)
        has_tx = bool(_tx_file(it, "txt") or _tx_file(it, "srt"))
        it["has_tx"] = has_tx
        it["has_thumbs"] = bool(_thumb_names(it))
        it.pop("path", None)
        if kind == "missing" and it["exists"]:
            continue
        if kind == "tx" and not has_tx:
            continue
        if kind in ("video", "audio", "playlist") and it.get("kind") != kind:
            continue
        if q:
            hay = " ".join(str(it.get(k) or "") for k in ("title", "name", "url")).lower()
            if q in hay:
                it["match"] = "title"
            else:
                snip = _snippet(_tx_text(it), q) if has_tx else ""
                if not snip:
                    continue
                it["match"], it["snippet"] = "tx", snip
        if it["exists"]:
            total_size += int(it.get("size") or 0)
        out.append(it)
    if sort == "old":
        out.sort(key=lambda x: x.get("time") or "")
    elif sort == "size":
        out.sort(key=lambda x: int(x.get("size") or 0), reverse=True)
    elif sort == "name":
        out.sort(key=lambda x: (x.get("title") or "").lower())
    else:
        out.sort(key=lambda x: x.get("time") or "", reverse=True)
    return {"items": out, "shown": len(out), "total": len(items), "size": total_size}


def _update_item(item_id, **fields):
    with _HIST_LOCK:
        items = _read_history()
        for x in items:
            if x.get("id") == item_id:
                x.update(fields)
        _write_history(items)


def api_open(b):
    """ফাইল (বা তার ট্রান্সক্রিপ্ট) ডিফল্ট প্রোগ্রামে খোলে। পাথ আসে ইতিহাস থেকে, অনুরোধ থেকে নয়।"""
    it = _find_item(sstr(b, "id"))
    if b.get("what") == "thumbs":
        d = _thumb_dir(it)
        if not d:
            raise ApiError("ছবির ফোল্ডার পাওয়া যায়নি।", 404)
        try:
            os.startfile(str(d)) if IS_WIN else _popen(["xdg-open", str(d)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)  # noqa
        except Exception:
            raise ApiError("ফোল্ডারটি খোলা যায়নি।")
        return {"ok": True}
    what = b.get("what") if b.get("what") in ("file", "txt", "srt") else "file"
    path = it.get("path") if what == "file" else _tx_file(it, what)
    if not path or not os.path.isfile(path):
        raise ApiError("ফাইলটি পাওয়া যায়নি।", 404)
    if what == "file" and Path(path).suffix.lower() not in MEDIA_EXTS:
        raise ApiError("এই ধরনের ফাইল খোলা যায় না।")
    try:
        if IS_WIN:
            os.startfile(path)  # noqa
        else:
            _popen(["xdg-open", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        raise ApiError("ফাইলটি খোলা যায়নি।")
    return {"ok": True}


def api_library_delete(item_id, with_file):
    it = _find_item(item_id)
    deleted = 0
    if with_file:
        cands = [it.get("path")] + [_tx_file(it, "txt"), _tx_file(it, "srt")]
        for i, p in enumerate(cands):
            if not p or not os.path.isfile(p):
                continue
            if i == 0 and Path(p).suffix.lower() not in MEDIA_EXTS:
                continue
            try:
                os.remove(p)
                deleted += 1
            except OSError:
                raise ApiError("ফাইলটি মোছা যায়নি। অন্য প্রোগ্রামে খোলা থাকলে বন্ধ করে আবার চেষ্টা করুন।")
    if with_file and _thumb_dir(it):
        d = _thumb_dir(it)
        for n in _thumb_names(it):
            try:
                (d / n).unlink()
                deleted += 1
            except OSError:
                pass
        try:
            d.rmdir()   # খালি না হলে (অন্য ফাইল থাকলে) রেখে দেয়
        except OSError:
            pass
    remove_history(item_id)
    return {"ok": True, "deleted": deleted}


def api_library_scan():
    """ডাউনলোড ফোল্ডারের যে মিডিয়া ফাইল তালিকায় নেই সেগুলো যোগ করে।"""
    root = downloads_dir()
    added = 0
    with _HIST_LOCK:
        items = _read_history()
        known = {os.path.normcase(os.path.abspath(x.get("path") or "")) for x in items}
        try:
            files = [f for f in root.iterdir() if f.is_file() and f.suffix.lower() in MEDIA_EXTS]
        except OSError:
            raise ApiError("ডাউনলোড ফোল্ডার পড়া যায়নি।")
        for f in sorted(files, key=lambda f: f.stat().st_mtime):
            if os.path.normcase(os.path.abspath(str(f))) in known:
                continue
            st = f.stat()
            sfx = f.suffix.lower()
            items.insert(0, {
                "id": uuid.uuid4().hex[:10], "title": f.stem, "url": "",
                "kind": "playlist" if sfx == ".zip" else ("audio" if sfx in AUDIO_EXTS else "video"),
                "quality": "", "subs": [], "name": f.name, "path": str(f), "size": st.st_size,
                "time": datetime.fromtimestamp(st.st_mtime, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "imported": True,
            })
            added += 1
        if added:
            _write_history(items[:MAX_HISTORY])
    return {"added": added}


# ---------------------------------------------------------------- অফলাইন ট্রান্সক্রিপশন (whisper.cpp)
WHISPER_NAMES = ("tiny", "base", "small")
WHISPER_MIN = {"tiny": 60_000_000, "base": 120_000_000, "small": 400_000_000}
WHISPER_URL = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main"
TX_LANGS = ("auto", "bn", "en", "hi", "ur", "ar", "ne", "id", "tr", "fa", "es", "fr", "de", "ru", "zh", "ja", "ko")
TX_TERMINAL = ("done", "error", "cancelled")
TX_LABEL = {"queued": "অপেক্ষায়", "model": "মডেল নামানো হচ্ছে (একবারই লাগে)", "extracting": "অডিও বের করা হচ্ছে",
            "transcribing": "কথা লেখা হচ্ছে", "working": "প্রক্রিয়া চলছে", "done": "সম্পন্ন", "error": "ব্যর্থ", "cancelled": "বাতিল"}


def whisper_exe():
    ov = os.environ.get("INSTA_WHISPER")
    if ov:
        return ov if os.path.exists(ov) else ""
    for n in (("whisper-cli.exe", "main.exe") if IS_WIN else ("whisper-cli", "main")):
        p = res(os.path.join("bin", "whisper", n))
        if os.path.exists(p):
            return p
    return ""


def _ffmpeg_exe():
    p = os.path.join(ffmpeg_dir(), "ffmpeg.exe" if IS_WIN else "ffmpeg")
    return p if os.path.exists(p) else ""


def model_path(name):
    return data_dir() / "models" / f"ggml-{name}.bin"


def _model_min(name):
    try:
        return int(os.environ["INSTA_WHISPER_MIN"])
    except (KeyError, ValueError):
        return WHISPER_MIN[name]


def model_ready(name):
    p = model_path(name)
    try:
        return p.is_file() and p.stat().st_size >= _model_min(name)
    except OSError:
        return False


def api_whisper():
    s = settings()
    return {"available": bool(whisper_exe()), "ffmpeg": bool(_ffmpeg_exe()), "model": s["whisper"],
            "ready": {n: model_ready(n) for n in WHISPER_NAMES}}


class Task:
    def __init__(self, item, lang="auto", translate=False, kind="tx"):
        self.kind, self.note, self.detail = kind, "", ""
        self.id = uuid.uuid4().hex[:10]
        self.item_id, self.title = item["id"], item.get("title") or item.get("name") or ""
        self.lang, self.translate = lang, translate
        self.status, self.pct, self.error, self.warning = "queued", 0, "", ""
        self.flag, self.proc = "", None
        self.t0 = time.time()


TASKS = {}
_T_LOCK = threading.Lock()
_TX_SEM = threading.Semaphore(1)   # একসময়ে একটিই (CPU ভারী কাজ)


def _download_model(task, name):
    import urllib.request
    base = os.environ.get("INSTA_WHISPER_URL", WHISPER_URL).rstrip("/")
    dst = model_path(name)
    dst.parent.mkdir(parents=True, exist_ok=True)
    part = dst.with_name(dst.name + ".part")
    have = part.stat().st_size if part.exists() else 0
    req = urllib.request.Request(f"{base}/ggml-{name}.bin", headers={"User-Agent": "Insta"})
    if have:
        req.add_header("Range", f"bytes={have}-")
    with urllib.request.urlopen(req, timeout=30) as r:
        if have and r.status != 206:
            have = 0
        total = have + int(r.headers.get("Content-Length") or 0)
        done = have
        with open(part, "ab" if have else "wb") as f:
            while True:
                if task.flag:
                    raise _Stopped()
                chunk = r.read(262144)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if total:
                    task.pct = min(99, int(done * 100 / total))
    with open(part, "rb") as f:
        magic = f.read(4)
    if magic != b"lmgg" or part.stat().st_size < _model_min(name):
        part.unlink(missing_ok=True)
        raise RuntimeError("model download corrupt")
    os.replace(part, dst)


def _run_whisper(task, args, tail):
    p = _popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    task.proc = p
    with _PROCS_LOCK:
        _PROCS.add(p)
    try:
        for raw in iter(p.stdout.readline, b""):
            line = raw.decode("utf-8", "replace").rstrip()
            m = re.search(r"progress\s*=\s*(\d+)\s*%", line)
            if m:
                task.pct = max(task.pct, min(99, int(m.group(1))))
            elif line:
                tail.append(line)
                del tail[:-40]
            if task.flag:
                kill_tree(p)
        rc = p.wait()
    finally:
        with _PROCS_LOCK:
            _PROCS.discard(p)
        task.proc = None
    if task.flag:
        raise _Stopped()
    return rc


def run_transcribe(task):
    item = _find_item(task.item_id)
    tmp = data_dir() / "tmp" / ("tx" + task.id)
    got = False
    tail = []
    try:
        while not _TX_SEM.acquire(timeout=0.4):
            if task.flag:
                raise _Stopped()
        got = True
        if task.flag:
            raise _Stopped()
        name = settings()["whisper"]
        if not model_ready(name):
            task.status, task.pct = "model", 0
            _download_model(task, name)
        task.status, task.pct = "extracting", 0
        tmp.mkdir(parents=True, exist_ok=True)
        wav = tmp / "a.wav"
        rc = run_tracked(task, [_ffmpeg_exe(), "-y", "-loglevel", "error", "-i", item["path"], "-vn",
                                "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav)])
        if rc != 0 or not wav.exists():
            raise RuntimeError("audio extract failed")
        task.status, task.pct = "transcribing", 0
        args = [whisper_exe(), "-m", str(model_path(name)), "-f", str(wav), "-l", task.lang,
                "-osrt", "-otxt", "-of", str(tmp / "out"), "-pp", "-t", str(max(2, min(8, os.cpu_count() or 4)))]
        if task.translate:
            args.append("-tr")
        rc = _run_whisper(task, args, tail)
        if rc != 0 or not (tmp / "out.txt").exists():
            raise RuntimeError(f"whisper rc={rc}")
        src = Path(item["path"])
        sfx = ".en" if task.translate else ""
        txt = src.with_name(src.stem + sfx + ".txt")
        srt = src.with_name(src.stem + sfx + ".srt")
        shutil.copyfile(tmp / "out.txt", txt)
        if (tmp / "out.srt").exists():
            shutil.copyfile(tmp / "out.srt", srt)
        if not txt.read_text(encoding="utf-8", errors="replace").strip():
            task.warning = "কোনো কথা শনাক্ত হয়নি। ভাষা ঠিক আছে কিনা দেখুন।"
        _update_item(task.item_id, tx={"txt": str(txt), "srt": str(srt) if srt.exists() else "",
                                       "lang": task.lang, "translate": task.translate, "time": iso_now()})
        task.pct, task.status = 100, "done"
        notify("ট্রান্সক্রিপ্ট তৈরি হয়েছে", task.title)
    except _Stopped:
        task.status = "cancelled"
    except Exception as e:
        in_model = task.status == "model"
        task.status = "error"
        task.error = ("মডেল নামানো যায়নি। ইন্টারনেট ঠিক আছে কিনা দেখে আবার চেষ্টা করুন।" if in_model
                      else "ট্রান্সক্রিপ্ট তৈরি করা যায়নি। ফাইলে কথা আছে কিনা দেখুন বা আবার চেষ্টা করুন।")
        write_error_log([f"transcribe: {task.title}", f"error: {e!r}", "--- whisper output ---"] + tail)
        notify("ট্রান্সক্রিপ্ট ব্যর্থ", task.title)
    finally:
        if got:
            _TX_SEM.release()
        task.proc = None
        shutil.rmtree(tmp, ignore_errors=True)


def _register_task(it, kind, lang="auto", translate=False):
    with _T_LOCK:
        if any(t.item_id == it["id"] and t.status not in TX_TERMINAL for t in TASKS.values()):
            raise ApiError("এই ফাইলে আগেই একটি কাজ চলছে।")
        if sum(t.status not in TX_TERMINAL for t in TASKS.values()) >= 20:
            raise ApiError("অনেক কাজ জমে আছে, কিছুটা অপেক্ষা করুন।")
        task = Task(it, lang, translate, kind)
        TASKS[task.id] = task
    return task


def api_transcribe(b):
    it = _find_item(sstr(b, "id"))
    path = it.get("path") or ""
    if not os.path.isfile(path):
        raise ApiError("ফাইলটি আর নেই, তাই ট্রান্সক্রিপ্ট করা যাবে না।")
    if Path(path).suffix.lower() not in (VIDEO_EXTS | AUDIO_EXTS):
        raise ApiError("শুধু ভিডিও ও অডিও ফাইলের ট্রান্সক্রিপ্ট হয়।")
    if not whisper_exe():
        raise ApiError("এই বিল্ডে ট্রান্সক্রিপশন ইঞ্জিন (whisper) নেই। README-র নির্দেশ দেখুন।")
    if not _ffmpeg_exe():
        raise ApiError("ffmpeg পাওয়া যায়নি।")
    lang = b.get("lang") if b.get("lang") in TX_LANGS else "auto"
    translate = b.get("translate") is True
    task = _register_task(it, "tx", lang, translate)
    threading.Thread(target=run_transcribe, args=(task,), daemon=True).start()
    return {"task": task.id}


def api_tasks():
    with _T_LOCK:
        ts = sorted(TASKS.values(), key=lambda t: t.t0, reverse=True)
        return [{"id": t.id, "item": t.item_id, "title": t.title, "status": t.status,
                 "label": (t.detail if t.status == "working" and t.detail else TX_LABEL[t.status]), "pct": t.pct,
                 "error": t.error, "warning": t.warning, "note": t.note, "kind": t.kind,
                 "lang": t.lang, "translate": t.translate} for t in ts]


def api_task_action(tid, action):
    with _T_LOCK:
        t = TASKS.get(tid)
        if not t:
            raise ApiError("কাজটি পাওয়া যায়নি।", 404)
        if action == "dismiss":
            if t.status in TX_TERMINAL:
                TASKS.pop(tid, None)
            return {"ok": True}
        if t.status not in TX_TERMINAL:
            t.flag = "cancel"
            if t.proc is not None:
                kill_tree(t.proc)
    return {"ok": True}


def api_transcript(item_id, what="txt"):
    it = _find_item(item_id)
    text = _tx_text(it)
    if not _tx_file(it, "txt"):
        raise ApiError("এই ফাইলের ট্রান্সক্রিপ্ট নেই।", 404)
    cut = len(text) > 400_000
    tx = it.get("tx") or {}
    return {"title": it.get("title") or "", "text": text[:400_000], "truncated": cut,
            "lang": tx.get("lang"), "translate": bool(tx.get("translate")), "has_srt": bool(_tx_file(it, "srt"))}


# ---------------------------------------------------------------- কাজের সাধারণ কাঠামো (ffmpeg অপারেশন)
class _OpError(Exception):
    """ব্যবহারকারীকে সরাসরি দেখানোর মতো বার্তাসহ ব্যর্থতা।"""


@contextlib.contextmanager
def _heavy_slot(task):
    while not _TX_SEM.acquire(timeout=0.4):
        if task.flag:
            raise _Stopped()
    try:
        if task.flag:
            raise _Stopped()
        yield
    finally:
        _TX_SEM.release()


_BN_TR = str.maketrans("0123456789", "০১২৩৪৫৬৭৮৯")


def _bn(x):
    return str(x).translate(_BN_TR)


def _fmt_dur(sec):
    sec = int(round(sec))
    m, s = divmod(sec, 60)
    h, m = divmod(m, 60)
    parts = ([f"{_bn(h)} ঘণ্টা"] if h else []) + ([f"{_bn(m)} মিনিট"] if m else []) + ([f"{_bn(s)} সেকেন্ড"] if s or not (h or m) else [])
    return " ".join(parts)


def _dur_of(path):
    try:
        rc, out, err = run_capture([_ffmpeg_exe(), "-hide_banner", "-i", path], timeout=60)
    except Exception:
        return 0.0
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", (out or "") + (err or ""))
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)) if m else 0.0


def _run_ff(task, args, total=0.0, lo=0, hi=100, cwd=None, on_line=None, tail=None):
    """ffmpeg চালায় (-progress pipe:1 সহ); শতাংশ আপডেট করে; বাতিল হলে _Stopped ছোড়ে।"""
    p = _popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=cwd)
    task.proc = p
    with _PROCS_LOCK:
        _PROCS.add(p)
    try:
        for raw in iter(p.stdout.readline, b""):
            line = raw.decode("utf-8", "replace").rstrip()
            m = re.match(r"out_time_(?:us|ms)=(\d+)$", line)
            if m:
                if total > 0:
                    secs = int(m.group(1)) / 1e6
                    task.pct = max(task.pct, min(hi - 1, lo + int((hi - lo) * secs / total)))
            else:
                if on_line:
                    on_line(line)
                if tail is not None and line and "=" not in line.split(" ")[0]:
                    tail.append(line)
                    del tail[:-40]
            if task.flag:
                kill_tree(p)
        rc = p.wait()
    finally:
        with _PROCS_LOCK:
            _PROCS.discard(p)
        task.proc = None
    if task.flag:
        raise _Stopped()
    return rc


def _out_path(src, suffix, ext):
    p = src.with_name(f"{src.stem} ({suffix}){ext}")
    n = 1
    while p.exists():
        p = src.with_name(f"{src.stem} ({suffix} {n}){ext}")
        n += 1
    return p


def _add_output(src_item, path, suffix, kind):
    add_history({
        "id": uuid.uuid4().hex[:10], "title": f"{src_item.get('title') or path.stem} ({suffix})",
        "url": src_item.get("url") or "", "kind": kind, "quality": src_item.get("quality") or "", "subs": [],
        "name": path.name, "path": str(path), "size": path.stat().st_size, "time": iso_now(), "derived": True,
    })


def _run_op(task, fn, tag, done_title):
    tmp = data_dir() / "tmp" / (tag + task.id)
    tail = []
    try:
        with _heavy_slot(task):
            task.status = "working"
            tmp.mkdir(parents=True, exist_ok=True)
            fn(task, _find_item(task.item_id), tmp, tail)
        task.pct, task.status, task.detail = 100, "done", ""
        notify(done_title, task.title)
    except _Stopped:
        task.status = "cancelled"
    except _OpError as e:
        task.status, task.error = "error", str(e)
        notify("কাজ ব্যর্থ", task.title)
    except Exception as e:
        task.status = "error"
        task.error = "কাজটি করা যায়নি। ফাইলটি ঠিক আছে কিনা দেখে আবার চেষ্টা করুন।"
        write_error_log([f"{tag}: {task.title}", f"error: {e!r}", "--- ffmpeg output ---"] + tail)
        notify("কাজ ব্যর্থ", task.title)
    finally:
        task.proc = None
        shutil.rmtree(tmp, ignore_errors=True)


def _need_media(it, exts):
    path = it.get("path") or ""
    if not os.path.isfile(path):
        raise ApiError("ফাইলটি আর নেই।")
    if Path(path).suffix.lower() not in exts:
        raise ApiError("এই ধরনের ফাইলে কাজটি করা যাবে না।")
    if not _ffmpeg_exe():
        raise ApiError("ffmpeg পাওয়া যায়নি।")
    return path


# ---------------------------------------------------------------- সাবটাইটেল ভিডিওতে বসানো (hardsub)
SUB_SIZES = {"small": 16, "medium": 22, "large": 30}
SUB_POS = {"bottom": 2, "middle": 10, "top": 6}   # SSA (পুরোনো) সংখ্যা: ffmpeg force_style এভাবেই পড়ে
SUB_COLORS = {"white": "&H00FFFFFF&", "yellow": "&H0000FFFF&"}
_FONT_CANDIDATES = (("Nirmala.ttf", "Nirmala UI"), ("Nirmala.ttc", "Nirmala UI"), ("vrinda.ttf", "Vrinda"))


def _prepare_fonts(tmp):
    """libass যেন বাংলা হরফ পায়: বান্ডেল করা ফন্ট, না থাকলে Windows-এর Nirmala UI/Vrinda কপি করে দেয়।"""
    fd = tmp / "fonts"
    fd.mkdir(exist_ok=True)
    family = ""
    bundled = Path(res(os.path.join("bin", "fonts")))
    if bundled.is_dir():
        for f in bundled.iterdir():
            if f.suffix.lower() in (".ttf", ".otf"):
                shutil.copyfile(f, fd / f.name)
                family = "Noto Sans Bengali"
    if not family and IS_WIN:
        wf = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
        for name, fam in _FONT_CANDIDATES:
            if (wf / name).is_file():
                shutil.copyfile(wf / name, fd / name)
                family = family or fam
    return family


def _sub_style(opts, family):
    st = [f"FontSize={SUB_SIZES[opts['size']]}", f"Alignment={SUB_POS[opts['pos']]}",
          f"PrimaryColour={SUB_COLORS[opts['color']]}", "MarginV=22"]
    if family:
        st.insert(0, f"FontName={family}")
    st += ["BorderStyle=3", "Outline=2", "OutlineColour=&H80000000&", "BackColour=&H80000000&", "Shadow=0"] if opts["box"] \
        else ["BorderStyle=1", "Outline=2", "OutlineColour=&H00000000&", "Shadow=0"]
    return ",".join(st)


def _burn(task, item, tmp, tail, opts):
    srt = _tx_file(item, "srt")
    if not srt:
        raise _OpError("সাবটাইটেল (SRT) ফাইল পাওয়া যায়নি। আগে ট্রান্সক্রিপ্ট করুন।")
    shutil.copyfile(srt, tmp / "s.srt")      # ASCII নামে কপি, যাতে ffmpeg ফিল্টারে পাথ-এস্কেপের ঝামেলা না হয়
    family = _prepare_fonts(tmp)
    vf = f"subtitles=s.srt:fontsdir=fonts:force_style='{_sub_style(opts, family)}'"
    dur = _dur_of(item["path"])
    task.detail = "সাবটাইটেল বসানো হচ্ছে"
    out = tmp / "o.mp4"
    rc = _run_ff(task, [_ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error", "-nostats", "-progress", "pipe:1",
                        "-i", item["path"], "-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(out)],
                 total=dur, cwd=str(tmp), tail=tail)
    if rc != 0 or not out.exists():
        raise RuntimeError(f"ffmpeg rc={rc}")
    final = _out_path(Path(item["path"]), "সাবটাইটেল", ".mp4")
    shutil.move(str(out), str(final))
    _add_output(item, final, "সাবটাইটেল", "video")
    task.note = f"তৈরি হয়েছে: {final.name}"


def api_burn(b):
    it = _find_item(sstr(b, "id"))
    _need_media(it, VIDEO_EXTS)
    if not _tx_file(it, "srt"):
        raise ApiError("আগে এই ভিডিওর ট্রান্সক্রিপ্ট করুন, তাহলেই সাবটাইটেল (SRT) পাওয়া যাবে।")
    opts = {"size": b.get("size") if b.get("size") in SUB_SIZES else "medium",
            "pos": b.get("pos") if b.get("pos") in SUB_POS else "bottom",
            "color": b.get("color") if b.get("color") in SUB_COLORS else "white",
            "box": b.get("box") is True}
    task = _register_task(it, "burn")
    threading.Thread(target=_run_op, args=(task, functools.partial(_burn, opts=opts), "bn", "সাবটাইটেলসহ ভিডিও তৈরি"),
                     daemon=True).start()
    return {"task": task.id}


# ---------------------------------------------------------------- নীরব অংশ বাদ দেওয়া
SIL_LEVELS = {"gentle": (-40, 1.0, 0.25), "normal": (-35, 0.6, 0.15), "strong": (-30, 0.35, 0.10)}


def _keep_segments(silences, dur, pad, min_cut):
    """নীরব অংশগুলো থেকে (কাটা, রাখা) অংশ বের করে। প্রতি নীরবতার দুই পাশে pad সেকেন্ড রেখে দেয়।"""
    cuts = []
    for s, e in sorted(silences):
        a, b = max(s, 0.0) + pad, min(e, dur) - pad
        if b - a >= min_cut:
            cuts.append((a, b))
    keep, pos = [], 0.0
    for a, b in cuts:
        if a - pos >= 0.04:
            keep.append((pos, a))
        pos = max(pos, b)
    if dur - pos >= 0.04:
        keep.append((pos, dur))
    return cuts, keep


def _silence(task, item, tmp, tail, level):
    src = item["path"]
    is_audio = Path(src).suffix.lower() in AUDIO_EXTS
    dur = _dur_of(src)
    if dur <= 0:
        raise _OpError("ফাইলের দৈর্ঘ্য জানা যায়নি।")
    db, mind, pad = SIL_LEVELS[level]
    sil, cur = [], [None]

    def on_line(line):
        m = re.search(r"silence_start:\s*(-?[\d.]+)", line)
        if m:
            cur[0] = float(m.group(1))
            return
        m = re.search(r"silence_end:\s*(-?[\d.]+)", line)
        if m and cur[0] is not None:
            sil.append((cur[0], float(m.group(1))))
            cur[0] = None

    task.detail = "নীরব অংশ খোঁজা হচ্ছে"
    rc = _run_ff(task, [_ffmpeg_exe(), "-hide_banner", "-nostats", "-progress", "pipe:1", "-i", src, "-vn",
                        "-af", f"silencedetect=noise={db}dB:d={mind}", "-f", "null", "-"],
                 total=dur, lo=0, hi=40, on_line=on_line, tail=tail)
    if rc != 0:
        raise _OpError("অডিও পড়া যায়নি। ফাইলে শব্দ (অডিও ট্র্যাক) আছে কিনা দেখুন।")
    if cur[0] is not None:
        sil.append((cur[0], dur))
    cuts, keep = _keep_segments(sil, dur, pad, 0.05)
    if len(keep) > 1200:   # ffmpeg এক্সপ্রেশন যেন বেশি লম্বা না হয়: ছোট কাটা বাদ দিয়ে শুধু বড়গুলো রাখি
        cuts, keep = _keep_segments(sil, dur, pad, 1.5)
    removed = sum(b - a for a, b in cuts)
    if not cuts or removed < 0.5:
        task.warning = "উল্লেখযোগ্য কোনো নীরব অংশ পাওয়া যায়নি, তাই নতুন ফাইল বানানো হয়নি। “কড়া” মাত্রায় চেষ্টা করতে পারেন।"
        return
    if sum(b - a for a, b in keep) < 0.5:
        raise _OpError("পুরো ফাইলই নীরব মনে হচ্ছে।")
    expr = "+".join(f"gte(t,{a:.3f})*lt(t,{b:.3f})" for a, b in keep)
    (tmp / "a.txt").write_text(f"aselect='{expr}',asetpts=N/SR/TB", encoding="utf-8")
    total_out = sum(b - a for a, b in keep)
    task.detail = "নীরব অংশ কাটা হচ্ছে"
    if is_audio:
        out = tmp / "o.mp3"
        cmd = [_ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error", "-nostats", "-progress", "pipe:1", "-i", src,
               "-filter_script:a", str(tmp / "a.txt"), "-c:a", "libmp3lame", "-q:a", "2", str(out)]
    else:
        (tmp / "v.txt").write_text(f"select='{expr}',setpts=N/FRAME_RATE/TB", encoding="utf-8")
        out = tmp / "o.mp4"
        cmd = [_ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error", "-nostats", "-progress", "pipe:1", "-i", src,
               "-filter_script:v", str(tmp / "v.txt"), "-filter_script:a", str(tmp / "a.txt"),
               "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
               "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(out)]
    rc = _run_ff(task, cmd, total=total_out, lo=40, hi=100, tail=tail)
    if rc != 0 or not out.exists():
        raise RuntimeError(f"ffmpeg rc={rc}")
    final = _out_path(Path(src), "নীরবতা বাদ", ".mp3" if is_audio else ".mp4")
    shutil.move(str(out), str(final))
    _add_output(item, final, "নীরবতা বাদ", "audio" if is_audio else "video")
    task.note = f"{_fmt_dur(removed)} নীরবতা বাদ গেছে ({_bn(int(round(removed * 100 / dur)))}% ছোট)। নতুন ফাইল: {final.name}"


def api_silence(b):
    it = _find_item(sstr(b, "id"))
    _need_media(it, VIDEO_EXTS | AUDIO_EXTS)
    level = b.get("level") if b.get("level") in SIL_LEVELS else "normal"
    task = _register_task(it, "silence")
    threading.Thread(target=_run_op, args=(task, functools.partial(_silence, level=level), "sl", "নীরবতা বাদ দেওয়া হয়েছে"),
                     daemon=True).start()
    return {"task": task.id}


# ---------------------------------------------------------------- চ্যানেল/প্লেলিস্ট ফলো
FOLLOW_INTERVALS = (1, 3, 6, 12, 24)
MAX_FOLLOWS = 30
_F_LOCK = threading.RLock()


def _follows_file():
    return data_dir() / "follows.json"


def _read_follows():
    try:
        d = json.loads(_follows_file().read_text(encoding="utf-8"))
        return d if isinstance(d, list) else []
    except (OSError, ValueError):
        return []


def _write_follows(items):
    f = _follows_file()
    tmp = f.with_name(f.name + ".tmp")
    tmp.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, f)


def _flatten_feed(d, out, depth=0):
    for e in d.get("entries") or []:
        if not isinstance(e, dict):
            continue
        if e.get("entries") is not None and depth < 3:
            _flatten_feed(e, out, depth + 1)
            continue
        vid = e.get("id")
        url = e.get("url") or e.get("webpage_url") or ""
        if not url.startswith("http") and vid and "youtube" in str(e.get("ie_key") or e.get("extractor") or "").lower():
            url = f"https://www.youtube.com/watch?v={vid}"
        if not url.startswith("http") or not valid_url(url):
            continue
        out.append((str(vid or url), url, e.get("title") or ""))


def _list_feed(url):
    """(ফিডের নাম, [(কী, লিংক, শিরোনাম) নতুন থেকে পুরোনো]) ফেরত দেয়।"""
    try:
        rc, out, err = run_capture([ytdlp_path(), "-J", "--flat-playlist", "--playlist-end", "20", "--no-warnings",
                                    "--encoding", "utf-8", "--", url], timeout=180)
    except subprocess.TimeoutExpired:
        raise ApiError("সময় শেষ: ফিড দেখা যায়নি। পরে আবার চেষ্টা করুন।")
    if rc != 0 or not out.strip():
        raise ApiError("ফিড পড়া যায়নি। লিংকটি চ্যানেল বা প্লেলিস্টের কিনা এবং ইন্টারনেট ঠিক আছে কিনা দেখুন।")
    try:
        d = json.loads(out)
    except ValueError:
        raise ApiError("ফিড পড়া যায়নি।")
    items, seen = [], set()
    _flatten_feed(d, items)
    uniq = []
    for k, u, t in items:
        if k not in seen:
            seen.add(k)
            uniq.append((k, u, t))
    return (d.get("title") or d.get("uploader") or d.get("channel") or url)[:120], uniq


def _queue_from_follow(f, entries):
    s = settings()
    q = f.get("quality") if f.get("quality") in QUALITIES else s["quality"]
    kind = "audio" if q == "audio" else "video"
    height = int(q) if q.isdigit() else None
    post = f.get("post") if f.get("post") in POSTS else s["post"]
    return [start_job(u, kind, height, False, [], (t or u)[:90], "follow", None, post) for _, u, t in entries]


def _public_follow(f):
    d = {k: v for k, v in f.items() if k != "seen"}
    d["seen_count"] = len(f.get("seen") or [])
    return d


def api_follows():
    with _F_LOCK:
        return [_public_follow(f) for f in _read_follows()]


def api_follow_add(b):
    url = sstr(b, "url").strip()
    if not valid_url(url) or len(url) > 2000 or re.search(r"\s", url):
        raise ApiError("সঠিক চ্যানেল বা প্লেলিস্টের লিংক দিন।")
    s = settings()
    q = b.get("quality") if b.get("quality") in QUALITIES else s["quality"]
    interval = b.get("interval") if b.get("interval") in FOLLOW_INTERVALS else 6
    try:
        backlog = max(0, min(10, int(b.get("backlog") or 0)))
    except (TypeError, ValueError):
        backlog = 0
    with _F_LOCK:
        items = _read_follows()
        if len(items) >= MAX_FOLLOWS:
            raise ApiError(f"সর্বোচ্চ {_bn(MAX_FOLLOWS)}টি ফলো রাখা যায়। কিছু মুছে আবার চেষ্টা করুন।")
        if any(f.get("url") == url for f in items):
            raise ApiError("এটি আগেই ফলো করা আছে।")
    title, feed = _list_feed(url)
    if not feed:
        raise ApiError("এই লিংকে কোনো ভিডিও পাওয়া যায়নি। চ্যানেল বা প্লেলিস্টের লিংক দিন।")
    f = {"id": uuid.uuid4().hex[:10], "url": url, "title": title, "quality": q, "post": b.get("post") if b.get("post") in POSTS else s["post"],
         "interval": interval, "enabled": True, "added": iso_now(), "last_check": iso_now(), "last_ts": time.time(),
         "last_new": 0, "total": 0, "error": "", "seen": [k for k, _, _ in feed][:600]}
    queued = _queue_from_follow(f, list(reversed(feed[:backlog]))) if backlog else []
    f["last_new"], f["total"] = len(queued), len(queued)
    with _F_LOCK:
        items = _read_follows()
        items.append(f)
        _write_follows(items)
    return {"follow": _public_follow(f), "queued": len(queued)}


def check_follow(fid):
    with _F_LOCK:
        f = next((x for x in _read_follows() if x.get("id") == fid), None)
    if not f:
        raise ApiError("ফলো আইটেমটি পাওয়া যায়নি।", 404)
    err, queued = "", []
    try:
        title, feed = _list_feed(f["url"])
        seen = list(f.get("seen") or [])
        known = set(seen)
        new = [e for e in feed if e[0] not in known]
        queued = _queue_from_follow(f, list(reversed(new[:10])))
        seen += [e[0] for e in new]
        f["seen"] = seen[-600:]
        f["title"] = title or f.get("title")
        if queued:
            notify("নতুন ভিডিও পেয়েছে", f"{f['title']}: {_bn(len(queued))}টি")
    except ApiError as e:
        err = str(e)
    with _F_LOCK:
        items = _read_follows()
        for x in items:
            if x.get("id") == fid:
                x.update(last_check=iso_now(), last_ts=time.time(), error=err, title=f.get("title"))
                if not err:
                    x["seen"] = f["seen"]
                    x["last_new"] = len(queued)
                    x["total"] = int(x.get("total") or 0) + len(queued)
        _write_follows(items)
    return {"new": len(queued), "error": err}


def api_follow_update(fid, b):
    with _F_LOCK:
        items = _read_follows()
        f = next((x for x in items if x.get("id") == fid), None)
        if not f:
            raise ApiError("ফলো আইটেমটি পাওয়া যায়নি।", 404)
        if b.get("quality") in QUALITIES:
            f["quality"] = b["quality"]
        if b.get("interval") in FOLLOW_INTERVALS:
            f["interval"] = b["interval"]
        if isinstance(b.get("enabled"), bool):
            f["enabled"] = b["enabled"]
        _write_follows(items)
        return _public_follow(f)


def api_follow_delete(fid):
    with _F_LOCK:
        items = _read_follows()
        if not any(x.get("id") == fid for x in items):
            raise ApiError("ফলো আইটেমটি পাওয়া যায়নি।", 404)
        _write_follows([x for x in items if x.get("id") != fid])
    return {"ok": True}


def follow_tick():
    """একবার দেখে নেয় কোন ফলো-র সময় হয়েছে; হলে নতুন ভিডিও খুঁজে কিউতে দেয়।"""
    now = time.time()
    with _F_LOCK:
        due = [f["id"] for f in _read_follows()
               if f.get("enabled", True) and now - float(f.get("last_ts") or 0) >= int(f.get("interval") or 6) * 3600]
    for fid in due:
        try:
            check_follow(fid)
        except Exception:
            pass
    return len(due)


def _follow_loop():
    time.sleep(20)   # অ্যাপ পুরোপুরি চালু হওয়ার পর
    while True:
        try:
            follow_tick()
        except Exception:
            pass
        time.sleep(60)


# ---------------------------------------------------------------- থাম্বনেইল / সেরা মুহূর্তের ছবি
THUMB_COUNTS = (6, 12, 24)


def _thumb_dir(it):
    d = (it.get("thumbs") or {}).get("dir")
    return Path(d) if d and os.path.isdir(d) else None


def _thumb_names(it):
    d = _thumb_dir(it)
    if not d:
        return []
    return [n for n in (it.get("thumbs") or {}).get("files", []) if isinstance(n, str) and (d / n).is_file()]


def _thumbs(task, item, tmp, tail, count):
    src = Path(item["path"])
    dur = _dur_of(str(src))
    if dur <= 0:
        raise _OpError("ভিডিওর দৈর্ঘ্য জানা যায়নি।")
    old = _thumb_dir(item)
    if old and old.name.endswith(" (ছবি)"):
        for n in _thumb_names(item):
            (old / n).unlink(missing_ok=True)
        outdir = old
    else:
        outdir = src.with_name(f"{src.stem} (ছবি)")
        k = 1
        while outdir.exists():
            outdir = src.with_name(f"{src.stem} (ছবি {k})")
            k += 1
        outdir.mkdir()
    seg, names = dur / count, []
    task.detail = "ছবি বের করা হচ্ছে"
    for i in range(count):
        if task.flag:
            raise _Stopped()
        out = tmp / f"{i + 1:02d}.jpg"
        # প্রতি অংশ থেকে সবচেয়ে "প্রতিনিধিত্বকারী" ফ্রেম (thumbnail ফিল্টার ৬০ ফ্রেম দেখে বাছে), কালো/ঝাপসা এড়াতে
        rc = _run_ff(task, [_ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{i * seg:.3f}", "-t", f"{seg:.3f}",
                            "-i", str(src), "-vf", "thumbnail=60,scale='min(1280,iw)':-2", "-frames:v", "1", "-q:v", "2", str(out)], tail=tail)
        task.pct = min(99, int((i + 1) * 100 / count))
        if rc == 0 and out.exists() and out.stat().st_size > 0:
            shutil.move(str(out), str(outdir / out.name))
            names.append(out.name)
    if not names:
        raise RuntimeError("no frames")
    _update_item(item["id"], thumbs={"dir": str(outdir), "files": names, "time": iso_now()})
    task.note = f"{_bn(len(names))}টি ছবি তৈরি হয়েছে ({outdir.name} ফোল্ডারে)"


def api_thumbs(b):
    it = _find_item(sstr(b, "id"))
    _need_media(it, VIDEO_EXTS)
    count = b.get("count") if b.get("count") in THUMB_COUNTS else 12
    task = _register_task(it, "thumbs")
    threading.Thread(target=_run_op, args=(task, functools.partial(_thumbs, count=count), "th", "থাম্বনেইল তৈরি হয়েছে"),
                     daemon=True).start()
    return {"task": task.id}


def api_thumbs_list(item_id):
    return {"count": len(_thumb_names(_find_item(item_id)))}


def api_thumb_file(item_id, n):
    it = _find_item(item_id)
    names = _thumb_names(it)
    if not 1 <= n <= len(names):
        raise ApiError("ছবিটি পাওয়া যায়নি।", 404)
    return (_thumb_dir(it) / names[n - 1]).read_bytes()


# ---------------------------------------------------------------- ফোনে পাঠানো (একই Wi-Fi-তে QR লিংক)
SHARE_MAX = 10
_SHARES = {}
_SH_LOCK = threading.RLock()
_SHARE_SRV = [None]


def _share_ttl():
    try:
        return int(os.environ["INSTA_SHARE_TTL"])
    except (KeyError, ValueError):
        return 1800


def _lan_ip():
    ov = os.environ.get("INSTA_LAN_IP")
    if ov:
        return ov
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("10.255.255.255", 1))   # প্যাকেট যায় না; শুধু কোন ইন্টারফেস ব্যবহার হবে তা জানতে
            ip = s.getsockname()[0]
        finally:
            s.close()
        return "" if ip.startswith("127.") else ip
    except OSError:
        return ""


def _is_lan(addr):
    import ipaddress
    try:
        ip = ipaddress.ip_address(addr.split("%")[0])
        if getattr(ip, "ipv4_mapped", None):
            ip = ip.ipv4_mapped
        return ip.is_private or ip.is_loopback or ip.is_link_local
    except ValueError:
        return False


def _qr_svg(text):
    try:
        import base64
        import io
        import qrcode
        import qrcode.image.svg
        q = qrcode.QRCode(border=2, error_correction=qrcode.constants.ERROR_CORRECT_M)
        q.add_data(text)
        q.make(fit=True)
        buf = io.BytesIO()
        q.make_image(image_factory=qrcode.image.svg.SvgPathImage).save(buf)
        return "data:image/svg+xml;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception:
        return None


def _share_get(token, hit=False):
    with _SH_LOCK:
        sh = _SHARES.get(token)
        if sh and sh["exp"] <= time.time():
            _SHARES.pop(token, None)
            sh = None
        if sh and hit:
            sh["hits"] += 1
        return sh


def _share_gc_loop():
    while True:
        time.sleep(5)
        with _SH_LOCK:
            now = time.time()
            for k in [k for k, v in _SHARES.items() if v["exp"] <= now]:
                _SHARES.pop(k, None)
            if not _SHARES and _SHARE_SRV[0]:
                srv, _SHARE_SRV[0] = _SHARE_SRV[0], None
                threading.Thread(target=srv.shutdown, daemon=True).start()
                srv.server_close()
                return


def _page(name, size, token, kind):
    import html
    n = html.escape(name)
    media = (f'<video controls playsinline preload="metadata" src="/s/{token}/d?inline=1"></video>' if kind == "video"
             else f'<audio controls src="/s/{token}/d?inline=1"></audio>' if kind == "audio" else "")
    return ("<!doctype html><html lang=bn><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            "<title>Insta</title><style>body{font-family:system-ui,sans-serif;margin:0;padding:18px;background:#eef3f0;color:#17301f}"
            "h1{font-size:1.05rem;overflow-wrap:anywhere}video,audio{width:100%;border-radius:12px;background:#000;margin:8px 0}"
            "a.b{display:block;text-align:center;background:#0d5c42;color:#fff;border-radius:14px;padding:15px;text-decoration:none;font-weight:700;margin-top:10px}"
            "p{color:#4a6454}</style>"
            f"<h1>{n}</h1><p>{_bn(round(size / 1048576, 1))} MB</p>{media}"
            f'<a class=b href="/s/{token}/d">ডাউনলোড করুন</a><p>Insta (MH &amp; MIK) থেকে পাঠানো। লিংকটির মেয়াদ সীমিত।</p></html>').encode("utf-8")


class ShareHandler(BaseHTTPRequestHandler):
    server_version = "Insta"

    def log_message(self, *a):
        pass

    def _plain(self, code, text):
        body = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        if not _is_lan(self.client_address[0]):
            return self._plain(403, "forbidden")
        u = urlparse(self.path)
        m = re.match(r"^/s/([A-Za-z0-9_-]{16,64})(/d)?$", u.path)
        sh = _share_get(m.group(1), hit=bool(m and m.group(2) and self.command == "GET")) if m else None
        if not sh or not os.path.isfile(sh["path"]):
            return self._plain(404, "লিংকটি ভুল অথবা মেয়াদ শেষ।")
        if not m.group(2):
            body = _page(sh["name"], sh["size"], m.group(1), sh["kind"])
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
            return
        from urllib.parse import quote
        size = os.path.getsize(sh["path"])
        a, b, code = 0, size - 1, 200
        rg = re.match(r"^bytes=(\d*)-(\d*)$", self.headers.get("Range") or "")
        if rg and (rg.group(1) or rg.group(2)):
            if rg.group(1):
                a = int(rg.group(1))
                b = int(rg.group(2)) if rg.group(2) else size - 1
            else:
                a = max(0, size - int(rg.group(2)))
            b = min(b, size - 1)
            if a > b or a >= size:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            code = 206
        inline = "inline=1" in (u.query or "")
        ext = Path(sh["path"]).suffix.lower()
        ctype = {".mp4": "video/mp4", ".webm": "video/webm", ".mkv": "video/x-matroska", ".mov": "video/quicktime",
                 ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".opus": "audio/ogg", ".ogg": "audio/ogg", ".wav": "audio/wav",
                 ".aac": "audio/aac", ".flac": "audio/flac", ".zip": "application/zip"}.get(ext, "application/octet-stream")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(b - a + 1))
        if code == 206:
            self.send_header("Content-Range", f"bytes {a}-{b}/{size}")
        self.send_header("Content-Disposition",
                         ("inline" if inline else "attachment") + f"; filename=\"file{ext}\"; filename*=UTF-8''{quote(sh['name'])}")
        self.end_headers()
        if self.command == "HEAD":
            return
        try:
            with open(sh["path"], "rb") as f:
                f.seek(a)
                left = b - a + 1
                while left > 0:
                    chunk = f.read(min(262144, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass


def _ensure_share_server():
    with _SH_LOCK:
        if _SHARE_SRV[0] is None:
            srv = ThreadingHTTPServer(("0.0.0.0", 0), ShareHandler)
            srv.daemon_threads = True
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            threading.Thread(target=_share_gc_loop, daemon=True).start()
            _SHARE_SRV[0] = srv
        return _SHARE_SRV[0].server_address[1]


def _share_public(token, sh, ip, port):
    url = f"http://{ip}:{port}/s/{token}"
    return {"token": token, "url": url, "qr": _qr_svg(url), "name": sh["name"], "left": max(0, int(sh["exp"] - time.time())),
            "hits": sh["hits"]}


def api_share(b):
    it = _find_item(sstr(b, "id"))
    path = it.get("path") or ""
    if not os.path.isfile(path) or Path(path).suffix.lower() not in MEDIA_EXTS:
        raise ApiError("ফাইলটি পাওয়া যায়নি বা পাঠানো যাবে না।")
    ip = _lan_ip()
    if not ip:
        raise ApiError("নেটওয়ার্ক পাওয়া যায়নি। কম্পিউটার Wi-Fi বা ল্যানে যুক্ত আছে কিনা দেখুন।")
    with _SH_LOCK:
        for k, v in list(_SHARES.items()):
            if v["exp"] > time.time() and v["item"] == it["id"]:
                return _share_public(k, v, ip, _ensure_share_server())
        if sum(v["exp"] > time.time() for v in _SHARES.values()) >= SHARE_MAX:
            raise ApiError(f"একসাথে সর্বোচ্চ {_bn(SHARE_MAX)}টি শেয়ার চলতে পারে। কিছু বন্ধ করুন।")
        token = secrets.token_urlsafe(18)
        ext = Path(path).suffix.lower()
        _SHARES[token] = {"item": it["id"], "path": path, "name": Path(path).name, "size": os.path.getsize(path), "hits": 0,
                          "exp": time.time() + _share_ttl(), "kind": "video" if ext in VIDEO_EXTS else "audio" if ext in AUDIO_EXTS else "file"}
        port = _ensure_share_server()
        return _share_public(token, _SHARES[token], ip, port)


def api_shares():
    ip = _lan_ip()
    with _SH_LOCK:
        port = _SHARE_SRV[0].server_address[1] if _SHARE_SRV[0] else 0
        return [_share_public(k, v, ip, port) for k, v in _SHARES.items() if v["exp"] > time.time()]


def api_share_stop(token):
    with _SH_LOCK:
        if _SHARES.pop(token, None) is None:
            raise ApiError("শেয়ারটি পাওয়া যায়নি।", 404)
    return {"ok": True}


# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "Insta"
    token = ""
    _cors = None

    def log_message(self, *a):   # --noconsole-এ stderr নেই, তাই লগ বন্ধ
        pass

    def _send(self, code, body, ctype):
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            if self._cors:
                self.send_header("Access-Control-Allow-Origin", self._cors)
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")
                self.send_header("Access-Control-Max-Age", "600")
                self.send_header("Vary", "Origin")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _file(self, rel, ctype):
        try:
            with open(res(rel), "rb") as f:
                self._send(200, f.read(), ctype)
        except OSError:
            self._json({"error": "পাওয়া যায়নি।"}, 404)

    def _body(self):
        try:
            n = min(int(self.headers.get("Content-Length") or 0), 1 << 20)
            d = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
            return d if isinstance(d, dict) else {}
        except Exception:
            return {}

    def _route(self, method):
        path = urlparse(self.path).path
        try:
            if method == "GET" and path in ("/", "/index.html"):
                return self._file("web/index.html", "text/html; charset=utf-8")
            if method == "GET" and path in ("/logo.png", "/favicon.ico"):
                return self._file("web/logo.png", "image/png")
            if path.startswith("/ext/"):
                origin = self.headers.get("Origin") or ""
                if not ext_origin_ok(origin):
                    return self._json({"error": "অনুমতি নেই।"}, 403)
                self._cors = origin
                if method == "OPTIONS":
                    return self._send(204, b"", "text/plain")
                if method in ("GET", "POST") and path == "/ext/ping":
                    return self._json({"ok": True, "app": APP_NAME, "version": VERSION})
                if method == "POST" and path == "/ext/send":
                    return self._json(api_ext_send(self._body()))
                return self._json({"error": "পাওয়া যায়নি।"}, 404)
            if not path.startswith("/api/"):
                return self._json({"error": "পাওয়া যায়নি।"}, 404)
            if not hmac.compare_digest(self.headers.get("X-Token") or "", self.token):
                return self._json({"error": "অনুমতি নেই।"}, 403)

            if method == "POST" and path == "/api/info":
                return self._json(api_info(sstr(self._body(), "url")))
            if method == "POST" and path == "/api/download":
                return self._json(api_download(self._body()))
            if method == "POST" and path == "/api/update":
                return self._json(api_update())
            if method == "POST" and path == "/api/extension":
                return self._json(api_extension())
            if method == "GET" and path == "/api/jobs":
                return self._json(api_jobs())
            if method == "DELETE" and path.startswith("/api/jobs/"):
                jid = path[len("/api/jobs/"):]
                if not ID_RE.match(jid):
                    raise ApiError("ভুল আইডি।")
                dismiss_job(jid)
                return self._json({"ok": True})
            if method == "POST" and path == "/api/batch":
                return self._json(api_batch(self._body()))
            if method == "GET" and path == "/api/clipboard":
                return self._json(api_clipboard_get())
            if method == "DELETE" and path == "/api/clipboard":
                return self._json(api_clipboard_clear())
            if method == "GET" and path == "/api/settings":
                return self._json(api_settings_get())
            if method == "POST" and path == "/api/settings":
                return self._json(api_settings_save(self._body()))
            m = re.match(r"^/api/jobs/([0-9a-f]{10})/(pause|resume|cancel|retry)$", path)
            if method == "POST" and m:
                return self._json(api_job_action(m.group(1), m.group(2)))
            if method == "POST" and path == "/api/reveal":
                return self._json(api_reveal(self._body()))
            if method == "GET" and path.startswith("/api/status/"):
                return self._json(api_status(path[len("/api/status/"):]))
            if method == "GET" and path == "/api/library":
                return self._json(api_library(parse_qs(urlparse(self.path).query)))
            if method == "POST" and path == "/api/library/scan":
                return self._json(api_library_scan())
            if method == "POST" and path == "/api/open":
                return self._json(api_open(self._body()))
            if method == "DELETE" and path.startswith("/api/library/"):
                hid = path[len("/api/library/"):]
                if not ID_RE.match(hid):
                    raise ApiError("ভুল আইডি।")
                return self._json(api_library_delete(hid, "file=1" in (urlparse(self.path).query or "")))
            if method == "POST" and path == "/api/burn":
                return self._json(api_burn(self._body()))
            if method == "POST" and path == "/api/silence":
                return self._json(api_silence(self._body()))
            if method == "GET" and path == "/api/follows":
                return self._json(api_follows())
            if method == "POST" and path == "/api/follows":
                return self._json(api_follow_add(self._body()))
            m = re.match(r"^/api/follows/([0-9a-f]{10})(/check)?$", path)
            if m and method == "POST":
                return self._json(check_follow(m.group(1)) if m.group(2) else api_follow_update(m.group(1), self._body()))
            if m and method == "DELETE" and not m.group(2):
                return self._json(api_follow_delete(m.group(1)))
            if method == "POST" and path == "/api/thumbs":
                return self._json(api_thumbs(self._body()))
            m = re.match(r"^/api/thumbs/([0-9a-f]{10})(?:/(\d{1,3}))?$", path)
            if method == "GET" and m:
                if m.group(2):
                    return self._send(200, api_thumb_file(m.group(1), int(m.group(2))), "image/jpeg")
                return self._json(api_thumbs_list(m.group(1)))
            if method == "POST" and path == "/api/share":
                return self._json(api_share(self._body()))
            if method == "GET" and path == "/api/share":
                return self._json(api_shares())
            m = re.match(r"^/api/share/([A-Za-z0-9_-]{16,64})$", path)
            if method == "DELETE" and m:
                return self._json(api_share_stop(m.group(1)))
            if method == "GET" and path == "/api/whisper":
                return self._json(api_whisper())
            if method == "POST" and path == "/api/transcribe":
                return self._json(api_transcribe(self._body()))
            if method == "GET" and path == "/api/transcribe":
                return self._json(api_tasks())
            m = re.match(r"^/api/transcribe/([0-9a-f]{10})/(cancel|dismiss)$", path)
            if method == "POST" and m:
                return self._json(api_task_action(m.group(1), m.group(2)))
            m = re.match(r"^/api/transcript/([0-9a-f]{10})$", path)
            if method == "GET" and m:
                return self._json(api_transcript(m.group(1)))
            if method == "GET" and path == "/api/history":
                return self._json(api_history())
            if method == "DELETE" and path == "/api/history":
                remove_history(None)
                return self._json({"ok": True})
            if method == "DELETE" and path.startswith("/api/history/"):
                hid = path[len("/api/history/"):]
                if not ID_RE.match(hid):
                    raise ApiError("ভুল আইডি।")
                remove_history(hid)
                return self._json({"ok": True})
            return self._json({"error": "পাওয়া যায়নি।"}, 404)
        except ApiError as e:
            self._json({"error": e.msg}, e.status)
        except Exception:
            self._json({"error": "সার্ভারে সমস্যা হয়েছে।"}, 500)

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def do_DELETE(self):
        self._route("DELETE")

    def do_OPTIONS(self):
        self._route("OPTIONS")


class InstaServer:
    def __init__(self):
        self.token = secrets.token_hex(16)
        Handler.token = self.token
        try:
            self.httpd = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
        except OSError:   # ৮৭৬৫ ব্যস্ত থাকলে যেকোনো ফাঁকা পোর্ট
            self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}/?t={self.token}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def stop(self):
        kill_all()
        try:
            self.httpd.shutdown()
        except Exception:
            pass
        shutil.rmtree(data_dir() / "tmp", ignore_errors=True)


def start():
    shutil.rmtree(data_dir() / "tmp", ignore_errors=True)   # আগের বারের অসমাপ্ত ফাইল
    init_binaries()
    if IS_WIN:
        threading.Thread(target=_clip_loop, daemon=True).start()
    threading.Thread(target=_follow_loop, daemon=True).start()
    return InstaServer()
