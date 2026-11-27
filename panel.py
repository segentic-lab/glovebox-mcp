#!/usr/bin/env python3
"""The taskbar of a glovebox instance: an Applications menu, the open windows, a clock.

Runs under the SYSTEM python3 (GTK 3 through PyGObject — the server's venv has neither), one
process per instance, on that instance's display:

    panel.py N --py <server python> --server <server.py>
    panel.py --check          exit 0 when GTK 3 can be imported

It is for a PERSON working in the window. Everything it starts goes through `server.py run N`,
the launcher launch_app uses, so the app opens inside this instance; when a program quits
without showing a window here (it handed the request to a copy running outside the sandbox),
the panel says so instead of leaving a click that did nothing.
"""
import json
import os
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

HEIGHT = 32
TITLE = "Glovebox panel"
TASK_MAX = 12         # window buttons shown; more are reachable through the desktop menu's window list
TITLE_CHARS = 22

if "--check" in sys.argv[1:]:
    try:
        import gi
        gi.require_version("Gtk", "3.0")
        gi.require_version("GdkX11", "3.0")
        from gi.repository import Gtk, GdkX11  # noqa: F401
        sys.exit(0)
    except Exception as e:   # noqa: BLE001
        print(e, file=sys.stderr)
        sys.exit(1)

import gi  # noqa: E402
gi.require_version("Gtk", "3.0")
gi.require_version("GdkX11", "3.0")
from gi.repository import Gdk, GdkX11, GLib, Gtk  # noqa: E402,F401

import appmenu  # noqa: E402


def _out(cmd, timeout=3) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except Exception:   # noqa: BLE001
        return ""


class Panel(Gtk.Window):
    def __init__(self, n, py, server):
        super().__init__(title=TITLE)
        self.n, self.py, self.server = n, py, server
        self.set_type_hint(Gdk.WindowTypeHint.DOCK)
        self.set_decorated(False)
        self.set_resizable(False)
        self.set_keep_above(True)
        self.stick()
        self.tasks = {}        # window id -> button
        self.shown = None      # last (ids+titles, active) drawn — redraw only on change

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        box.set_border_width(2)
        self.add(box)
        apps = Gtk.Button(label="Applications")
        apps.connect("clicked", self.on_apps)
        box.pack_start(apps, False, False, 0)
        run = Gtk.Button(label="Run…")
        run.connect("clicked", self.on_run)
        box.pack_start(run, False, False, 0)
        self.taskbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        box.pack_start(self.taskbox, True, True, 0)
        self.clock = Gtk.Label()
        box.pack_end(self.clock, False, False, 6)
        box.pack_end(Gtk.Label(label=f":{n}"), False, False, 6)

        screen = self.get_screen()
        screen.connect("size-changed", lambda *_: self.place())
        self.connect("realize", lambda *_: GLib.idle_add(self.place))
        self.connect("delete-event", lambda *_: True)      # a taskbar is not closed by Alt+F4
        GLib.timeout_add(1000, self.tick)
        self.show_all()
        self.tick()

    # ----- geometry: the bottom edge, reserved through a strut so maximized apps stop above it
    def place(self):
        screen = self.get_screen()
        w, h = screen.get_width(), screen.get_height()
        self.set_size_request(w, HEIGHT)
        self.resize(w, HEIGHT)
        self.move(0, h - HEIGHT)
        gdkwin = self.get_window()
        if gdkwin:
            xid = str(gdkwin.get_xid())
            subprocess.run(["xprop", "-id", xid, "-f", "_NET_WM_STRUT", "32c", "-set", "_NET_WM_STRUT",
                            f"0, 0, 0, {HEIGHT}"], capture_output=True)
            subprocess.run(["xprop", "-id", xid, "-f", "_NET_WM_STRUT_PARTIAL", "32c", "-set", "_NET_WM_STRUT_PARTIAL",
                            f"0, 0, 0, {HEIGHT}, 0, 0, 0, 0, 0, 0, 0, {max(0, w - 1)}"], capture_output=True)
        return False

    # ----- the open windows
    def tick(self):
        self.clock.set_text(time.strftime("%H:%M"))
        rows = []
        for line in _out(["wmctrl", "-l"]).splitlines():
            parts = line.split(None, 3)
            if len(parts) < 3 or parts[1] == "-1":        # desktop -1 = sticky: docks, this panel
                continue
            title = parts[3] if len(parts) > 3 else ""
            if title != TITLE:
                rows.append((int(parts[0], 16), title))
        try:
            active = int(_out(["xdotool", "getactivewindow"]).strip() or 0)
        except ValueError:
            active = 0
        state = (tuple(rows), active)
        if state != self.shown:
            self.shown = state
            for child in self.taskbox.get_children():
                self.taskbox.remove(child)
            for wid, title in rows[:TASK_MAX]:
                short = title if len(title) <= TITLE_CHARS else title[:TITLE_CHARS - 1] + "…"
                b = Gtk.Button(label=("● " if wid == active else "") + (short or "(untitled)"))
                b.set_tooltip_text(title)
                b.set_relief(Gtk.ReliefStyle.NORMAL if wid == active else Gtk.ReliefStyle.NONE)
                b.connect("clicked", lambda _b, w=wid: subprocess.run(["wmctrl", "-i", "-a", hex(w)], capture_output=True))
                self.taskbox.pack_start(b, False, False, 0)
            if len(rows) > TASK_MAX:
                self.taskbox.pack_start(Gtk.Label(label=f"+{len(rows) - TASK_MAX}"), False, False, 4)
            self.taskbox.show_all()
        return True

    # ----- starting apps
    def on_apps(self, button):
        menu = Gtk.Menu()
        groups = appmenu.grouped(appmenu.entries())
        if not groups:
            item = Gtk.MenuItem(label="(no applications found)")
            item.set_sensitive(False)
            menu.append(item)
        for group, items in groups:
            sub = Gtk.Menu()
            for a in items:
                item = Gtk.MenuItem(label=a["name"])
                item.connect("activate", lambda _i, a=a: self.launch(a["argv"], a["name"]))
                sub.append(item)
            head = Gtk.MenuItem(label=group)
            head.set_submenu(sub)
            menu.append(head)
        menu.show_all()
        menu.popup_at_widget(button, Gdk.Gravity.NORTH_WEST, Gdk.Gravity.SOUTH_WEST, None)
        self._menu = menu      # keep a reference while it is open

    def on_run(self, _button):
        dlg = Gtk.Dialog(title=f"Run in glovebox :{self.n}", transient_for=self, modal=True)
        dlg.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Run", Gtk.ResponseType.OK)
        dlg.set_default_response(Gtk.ResponseType.OK)
        entry = Gtk.Entry()
        entry.set_width_chars(48)
        entry.set_activates_default(True)
        dlg.get_content_area().pack_start(entry, True, True, 8)
        dlg.show_all()
        ok = dlg.run() == Gtk.ResponseType.OK
        text = entry.get_text().strip()
        dlg.destroy()
        if ok and text:
            import shlex
            try:
                argv = shlex.split(text)
            except ValueError as e:
                self.notice(f"Cannot parse the command: {e}")
                return
            self.launch(argv, argv[0])

    def launch(self, argv, name):
        def work():
            try:
                r = subprocess.run(appmenu.run_argv(self.py, self.server, self.n, argv, wait_window=10),
                                   capture_output=True, text=True, timeout=90)
                res = json.loads(r.stdout.strip().splitlines()[-1])
            except Exception as e:   # noqa: BLE001
                res = {"ok": False, "error": f"the launcher failed: {e!r}"}
            if not res.get("ok"):
                GLib.idle_add(self.notice, f"{name} was not started: {res.get('error', 'unknown error')}")
            elif res.get("window") is False and res.get("exited") == 0:
                GLib.idle_add(self.notice,
                              f"{name} quit without opening a window here.\n\nIt most likely handed the request to a "
                              f"copy of itself that is already running outside this glovebox (programs that allow "
                              f"only one running copy do that). Close that copy, or start {name} here first.")
            elif res.get("exited"):
                GLib.idle_add(self.notice, f"{name} exited with code {res['exited']}. "
                                           f"Its error output is in the instance's app log.")
        threading.Thread(target=work, daemon=True).start()

    def notice(self, text):
        dlg = Gtk.MessageDialog(transient_for=self, modal=False, message_type=Gtk.MessageType.WARNING,
                                buttons=Gtk.ButtonsType.CLOSE, text=text)
        dlg.set_title(f"Glovebox :{self.n}")
        dlg.connect("response", lambda d, _r: d.destroy())
        dlg.show_all()
        return False


def main():
    args = sys.argv[1:]
    try:
        n = int(args[0])
        py = args[args.index("--py") + 1]
        server = args[args.index("--server") + 1]
    except (IndexError, ValueError):
        print("usage: panel.py N --py PYTHON --server SERVER_PY | --check", file=sys.stderr)
        return 2
    Panel(n, py, server)
    Gtk.main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
