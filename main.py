# -*- coding: utf-8 -*-
"""
Insta — ভিডিও ডাউনলোডার (Windows)
তৈরি করেছেন: MH & MIK

চালু করলে লোকাল সার্ভার শুরু হয় এবং একটি নেটিভ উইন্ডোতে (Edge WebView2) UI খোলে।
জানালা বন্ধ করলে অ্যাপ সিস্টেম ট্রে-তে চলতে থাকে (সেটিংসে বন্ধ করা যায়), যাতে Chrome এক্সটেনশন সবসময় সাড়া পায়।
"""
import os
import sys
import threading
import urllib.error
import urllib.request

import server

IS_WIN = os.name == "nt"
WEBVIEW2_URL = "https://developer.microsoft.com/microsoft-edge/webview2/"
MUTEX_NAME = "Local\\InstaSingleInstance"
SHOW_EVENT = "Local\\InstaShowWindow"


def message_box(text, title="Insta", yes_no=False):
    """Windows মেসেজ বক্স। yes_no হলে 'হ্যাঁ' চাপলে True।"""
    if not IS_WIN:
        return False
    import ctypes
    flags = 0x10024 if yes_no else 0x10040   # MB_SETFOREGROUND | (MB_YESNO|ICONQUESTION) / ICONINFORMATION
    return ctypes.windll.user32.MessageBoxW(0, text, title, flags) == 6


def webview2_installed():
    if not IS_WIN:
        return True
    import winreg
    guid = r"SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
    places = [(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node" + guid[8:]),
              (winreg.HKEY_LOCAL_MACHINE, guid),
              (winreg.HKEY_CURRENT_USER, guid)]
    for root, sub in places:
        try:
            with winreg.OpenKey(root, sub) as k:
                v, _ = winreg.QueryValueEx(k, "pv")
                if v and v != "0.0.0.0":
                    return True
        except OSError:
            continue
    return False


# ---------------------------------------------------------------- একটিমাত্র ইনস্ট্যান্স
_keep = []   # হ্যান্ডেল বেঁচে থাকার জন্য


def _kernel32():
    import ctypes
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    k.CreateMutexW.restype = ctypes.c_void_p
    k.CreateEventW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_wchar_p]
    k.CreateEventW.restype = ctypes.c_void_p
    k.OpenEventW.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_wchar_p]
    k.OpenEventW.restype = ctypes.c_void_p
    k.SetEvent.argtypes = [ctypes.c_void_p]
    k.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    k.CloseHandle.argtypes = [ctypes.c_void_p]
    return k


def single_instance():
    """প্রথম ইনস্ট্যান্স হলে True। দ্বিতীয়টি হলে প্রথমটির জানালা সামনে আনতে বলে False ফেরত দেয়।"""
    if not IS_WIN:
        return True
    import ctypes
    k = _kernel32()
    h = k.CreateMutexW(None, False, MUTEX_NAME)
    if ctypes.get_last_error() == 183:   # ERROR_ALREADY_EXISTS
        ev = k.OpenEventW(0x0002, False, SHOW_EVENT)   # EVENT_MODIFY_STATE
        if ev:
            k.SetEvent(ev)
            k.CloseHandle(ev)
        return False
    _keep.append(h)
    return True


def watch_show_event(show):
    """দ্বিতীয়বার Insta চালানো হলে জানালা দেখায়।"""
    if not IS_WIN:
        return
    k = _kernel32()
    ev = k.CreateEventW(None, False, False, SHOW_EVENT)
    if not ev:
        return
    _keep.append(ev)

    def loop():
        while True:
            if k.WaitForSingleObject(ev, 0xFFFFFFFF) != 0:
                return
            show()

    threading.Thread(target=loop, daemon=True).start()


# ---------------------------------------------------------------- সেলফটেস্ট (CI-তে ব্যবহৃত)
def selftest(outfile):
    lines, ok = [], True

    def check(name, fn):
        nonlocal ok
        try:
            lines.append(f"PASS {name}: {fn()}")
        except Exception as e:
            ok = False
            lines.append(f"FAIL {name}: {e!r}")

    def need(cond, msg):
        if not cond:
            raise AssertionError(msg)
        return True

    def http(path, method="GET", token=None, body=None):
        req = urllib.request.Request(f"http://127.0.0.1:{srv.port}{path}", method=method,
                                     data=body, headers={"Content-Type": "application/json"})
        if token:
            req.add_header("X-Token", token)
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    srv = server.start()
    try:
        if IS_WIN:
            check("import webview", lambda: __import__("webview").__name__)
            check("import clr", lambda: __import__("clr").__name__)
            check("import pystray", lambda: __import__("pystray").__name__)
            check("import PIL", lambda: __import__("PIL.Image").__name__)
            check("single-instance api", lambda: need(_kernel32() is not None, "kernel32"))
        check("bundled index.html", lambda: os.path.getsize(server.res("web/index.html")))
        check("bundled logo", lambda: os.path.getsize(server.res("web/logo.png")))
        check("bundled extension", lambda: os.path.getsize(server.res("extension/manifest.json")))
        check("GET /", lambda: need("Insta" in http("/")[1].decode("utf-8"), "page has no 'Insta'"))
        check("api without token -> 403", lambda: need(http("/api/history")[0] == 403, "expected 403"))
        check("api history", lambda: http("/api/history", token=srv.token)[1][:40])
        check("api settings", lambda: need(b'"parallel"' in http("/api/settings", token=srv.token)[1], "no settings"))
        check("api jobs", lambda: need(http("/api/jobs", token=srv.token)[0] == 200, "jobs"))
        check("api info bad url -> 400", lambda: need(http(
            "/api/info", "POST", srv.token, b'{"url":"abc"}')[0] == 400, "expected 400"))
        check("yt-dlp --version", lambda: server.tool_version(server.ytdlp_path()) or (_ for _ in ()).throw(
            AssertionError("no version")))

        def ffmpeg():
            exe = os.path.join(server.ffmpeg_dir(), "ffmpeg.exe" if IS_WIN else "ffmpeg")
            rc, out, _ = server.run_capture([exe, "-version"], timeout=60)
            if rc != 0:
                raise AssertionError(f"exit {rc}")
            return out.splitlines()[0]

        check("ffmpeg -version", ffmpeg)
        check("api library", lambda: need(b'"items"' in http("/api/library", token=srv.token)[1], "no items"))
        check("api whisper", lambda: need(http("/api/whisper", token=srv.token)[0] == 200, "whisper"))

        def whisper():
            exe = server.whisper_exe()
            if not exe:
                return "INFO: whisper ইঞ্জিন বান্ডেল নেই (ট্রান্সক্রিপ্ট বন্ধ থাকবে)"
            rc, out, err = server.run_capture([exe, "-h"], timeout=60)   # DLL না থাকলে এখানেই ধরা পড়ে
            if rc not in (0, 1) or not (out + err).strip():
                raise AssertionError(f"exit {rc}")
            return "whisper চলে"

        check("whisper engine", whisper)
        check("qrcode library", lambda: need(server._qr_svg("http://192.168.0.2:1/s/x") is not None, "qrcode নেই"))
        check("api follows", lambda: need(http("/api/follows", token=srv.token)[1].strip().startswith(b"["), "follows"))
        check("ffmpeg subtitles+silencedetect filters", lambda: need(
            all(k in server.run_capture([os.path.join(server.ffmpeg_dir(), "ffmpeg.exe" if IS_WIN else "ffmpeg"),
                                         "-hide_banner", "-filters"], timeout=60)[1] for k in (" subtitles ", " silencedetect ", " aselect ")),
            "ffmpeg-এ subtitles/silencedetect ফিল্টার নেই"))
    finally:
        srv.stop()
    with open(outfile, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return 0 if ok else 1


# ---------------------------------------------------------------- ফোল্ডার বাছাইয়ের JS API
class Api:
    def pick_folder(self):
        """সেটিংসের 'বাছুন' বাটন: Windows-এর ফোল্ডার বাছাই জানালা।"""
        try:
            import webview
            kind = webview.FileDialog.FOLDER if hasattr(webview, "FileDialog") else webview.FOLDER_DIALOG
            r = webview.windows[0].create_file_dialog(kind)
            return r[0] if r else ""
        except Exception:
            return ""


# ---------------------------------------------------------------- ট্রে
def make_tray(show, open_folder, quit_app):
    """সিস্টেম ট্রে আইকন; pystray/Pillow না থাকলে বা ব্যর্থ হলে None।"""
    try:
        import pystray
        from PIL import Image
        img = Image.open(server.res("web/logo.png")).convert("RGBA").resize((64, 64))
        menu = pystray.Menu(
            pystray.MenuItem("Insta খুলুন", lambda icon, item: show(), default=True),
            pystray.MenuItem("ডাউনলোড ফোল্ডার", lambda icon, item: open_folder()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("বের হন", lambda icon, item: quit_app()),
        )
        icon = pystray.Icon("Insta", img, "Insta — ভিডিও ডাউনলোডার", menu)
        icon.run_detached()
        return icon
    except Exception:
        return None


# ---------------------------------------------------------------- প্রধান
def main():
    if "--selftest" in sys.argv:
        i = sys.argv.index("--selftest")
        out = sys.argv[i + 1] if len(sys.argv) > i + 1 else "selftest.txt"
        os._exit(selftest(out))

    if IS_WIN and not webview2_installed():
        if message_box("Insta চালাতে Microsoft Edge WebView2 Runtime দরকার, কিন্তু এটি পাওয়া যায়নি।\n\n"
                       "ডাউনলোড পেজ খুলব? (Evergreen Bootstrapper ইনস্টল করে আবার চালু করুন)",
                       yes_no=True):
            os.startfile(WEBVIEW2_URL)   # noqa
            os._exit(0)

    if not single_instance():   # Insta আগে থেকেই চলছে (সম্ভবত ট্রে-তে), সেটির জানালা সামনে আসবে
        os._exit(0)

    srv = server.start()
    state = {"quitting": False, "hinted": False, "tray": None, "window": None}

    def show_window():
        w = state["window"]
        if w is None:
            return
        try:
            w.show()
            w.restore()
        except Exception:
            pass

    def open_folder():
        try:
            os.startfile(str(server.downloads_dir()))   # noqa
        except Exception:
            pass

    def quit_app():
        if server.pending_count() > 0 and not message_box(
                "ডাউনলোড চলছে বা লাইনে আছে। বের হলে সেগুলো থেমে যাবে। বের হবেন?", yes_no=True):
            return
        state["quitting"] = True
        if state["tray"] is not None:
            try:
                state["tray"].stop()
            except Exception:
                pass
        try:
            state["window"].destroy()
        except Exception:
            os._exit(0)

    try:
        import webview

        tray = make_tray(show_window, open_folder, quit_app) if IS_WIN else None
        state["tray"] = tray
        if tray is not None:
            def toast(title, text):
                tray.notify(text, title)
            server.NOTIFY[0] = toast

        # --minimized (Windows চালুর সময় নিজে থেকে চালু): ট্রে থাকলে জানালা লুকানো অবস্থায় শুরু
        start_hidden = "--minimized" in sys.argv and tray is not None and server.settings()["tray"]
        window = webview.create_window("Insta", srv.url, width=580, height=880, min_size=(380, 620),
                                       background_color="#EDF3EE", js_api=Api(), hidden=start_hidden)
        state["window"] = window

        def on_closing():
            # ট্রে চালু থাকলে জানালা শুধু লুকায়, অ্যাপ চলতে থাকে
            if state["quitting"] or state["tray"] is None or not server.settings()["tray"]:
                return True
            window.hide()
            if not state["hinted"]:
                state["hinted"] = True
                try:
                    state["tray"].notify("Insta ট্রে-তে চলছে। খুলতে আইকনে ডাবল-ক্লিক করুন।", "Insta")
                except Exception:
                    pass
            return False

        window.events.closing += on_closing
        watch_show_event(show_window)

        icon = server.res(os.path.join("assets", "icon.ico"))
        webview.start(gui="edgechromium" if IS_WIN else None,
                      icon=icon if os.path.exists(icon) else None)
    except Exception as e:
        message_box(f"Insta চালু করা যায়নি।\n\n{e}", "Insta — সমস্যা")
    finally:
        srv.stop()
        os._exit(0)   # ব্যাকগ্রাউন্ড থ্রেড/yt-dlp প্রসেস থাকলেও পুরোপুরি বন্ধ


if __name__ == "__main__":
    main()
