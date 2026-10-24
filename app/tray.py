"""Nightingale in the background: a tray icon that hosts the server and shows Windows notifications.

    pythonw -m app.tray          no console window (what "Start with Windows" uses)
    python  -m app.tray          same, with a console for troubleshooting

The icon turns pink when something needs you. Its menu opens the dashboard, runs a GPU check-up,
switches light/serious mode, and turns alerts and start-with-Windows on or off. If Nightingale is
already running, this just opens the dashboard.
"""
from __future__ import annotations

import logging
import os
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PORT = int(os.getenv("NIGHTINGALE_PORT", "8000"))
URL = f"http://127.0.0.1:{PORT}/"
LOG = ROOT / "data" / "nightingale.log"


def already_running() -> bool:
    try:
        with urllib.request.urlopen(URL + "api/health", timeout=2) as r:
            return b'"ok":true' in r.read().replace(b" ", b"")
    except OSError:
        return False


def make_icon(alert: bool = False, serious: bool = False):
    """The brand mark drawn with Pillow: a GPU die with a lit cross, pink when something needs you."""
    from PIL import Image, ImageDraw  # noqa: PLC0415

    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((6, 6, 58, 58), radius=12, fill=(18, 23, 27, 255), outline=(58, 64, 71, 255), width=2)
    lit = (255, 122, 198, 255) if alert else (255, 77, 94, 255) if serious else (255, 181, 71, 255)
    dim = (38, 49, 58, 255)
    for r in range(3):
        for c in range(3):
            on = (r == 1) or (c == 1)
            x, y = 13 + c * 14, 13 + r * 14
            d.rounded_rectangle((x, y, x + 11, y + 11), radius=3, fill=lit if on else dim)
    return img


def setup_logging() -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    if sys.stdout is None or sys.stderr is None:  # pythonw has no console
        stream = open(LOG, "a", encoding="utf-8", buffering=1)  # noqa: SIM115 - lives as long as the app
        sys.stdout = sys.stdout or stream
        sys.stderr = sys.stderr or stream
    logging.basicConfig(filename=str(LOG), level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")


def main() -> int:
    os.chdir(ROOT)
    setup_logging()
    if already_running():
        webbrowser.open(URL)
        return 0

    import pystray  # noqa: PLC0415 - Windows-only dependency
    import uvicorn  # noqa: PLC0415

    from app import autostart  # noqa: PLC0415
    from app.alerts import MESSAGE_MAX, TITLE_MAX, clip  # noqa: PLC0415
    from app import main as web  # noqa: PLC0415

    web.TRAY["running"] = True
    server = uvicorn.Server(uvicorn.Config(web.app, host="127.0.0.1", port=PORT, log_config=None, access_log=False))
    threading.Thread(target=server.run, name="nightingale-server", daemon=True).start()

    def needs_you() -> int:
        return sum(1 for i in web.agent.incidents if i["target"] == "laptop"
                   and i["status"] in ("awaiting_approval", "needs_attention"))

    def open_dashboard(_icon=None, _item=None):
        webbrowser.open(URL)

    def checkup(_icon=None, _item=None):
        try:
            req = urllib.request.Request(URL + "api/pc/checkup", method="POST")
            urllib.request.urlopen(req, timeout=5).close()
        except OSError:
            pass
        webbrowser.open(URL + "#/pc")

    def set_mode(mode):
        def run(_icon, _item):
            web.modes.set(mode)
            web.cluster._log("laptop" if web.cluster.local is not None else "fleet", "nightingale", "info",
                             web.modes.status()["note"])
        return run

    def toggle_alerts(_icon, _item):
        web.alerts.enabled = not web.alerts.enabled

    def toggle_start(_icon, _item):
        ok, msg = autostart.set_enabled(not autostart.is_enabled())
        icon.notify(msg, "Nightingale")

    def quit_app(_icon, _item):
        server.should_exit = True
        icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem("Open Nightingale", open_dashboard, default=True),
        pystray.MenuItem("Check my GPU with Nemotron", checkup),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Light mode", set_mode("light"), checked=lambda _i: web.modes.mode == "light", radio=True),
        pystray.MenuItem("Serious mode (30 min)", set_mode("serious"), checked=lambda _i: web.modes.mode == "serious",
                         radio=True),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Alerts", toggle_alerts, checked=lambda _i: web.alerts.enabled),
        pystray.MenuItem("Start with Windows", toggle_start, checked=lambda _i: autostart.is_enabled()),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit Nightingale", quit_app),
    )
    icon = pystray.Icon("nightingale", make_icon(), "Nightingale", menu)

    def watch(icon_):
        icon_.visible = True
        icon_.notify("Watching your GPU. Right-click this icon for options.", "Nightingale is running")
        last_look = None
        while icon_.visible:
            try:
                for a in web.alerts.take():
                    try:
                        icon_.notify(clip(a["message"], MESSAGE_MAX), clip(a["title"], TITLE_MAX))
                    except Exception:  # one bad notification must not drop the rest
                        logging.getLogger("nightingale.tray").exception("notification")
                    time.sleep(4)  # one at a time, so none is lost
                n = needs_you()
                look = (n > 0, web.modes.mode)
                if look != last_look:
                    icon_.icon = make_icon(alert=n > 0, serious=web.modes.mode == "serious")
                    icon_.title = clip(f"Nightingale: {n} thing{'s' if n != 1 else ''} need you" if n else
                                       f"Nightingale: all good ({web.modes.mode} mode)", 127)
                    last_look = look
            except Exception:  # never let the tray loop die
                logging.getLogger("nightingale.tray").exception("tray loop")
            time.sleep(2)

    icon.run(setup=watch)
    return 0


if __name__ == "__main__":
    sys.exit(main())
