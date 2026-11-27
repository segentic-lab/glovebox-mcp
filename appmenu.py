#!/usr/bin/env python3
"""The applications menu of a glovebox instance — stdlib only, shared by the server (venv python)
and the panel (system python, GTK).

It reads the machine's .desktop entries and produces three things:

  entries()        the launchable GUI apps, grouped the way a desktop menu groups them
  openbox_menu()   the instance's right-click desktop menu (apps by group + the open windows)
  write_shadow()   overriding .desktop files for apps that hand a launch over to a copy that is
                   already running (browsers, Firefox, LibreOffice)

Every command these produce starts the app through `server.py run N -- …`, the same launcher
launch_app uses — that is what keeps a menu click, or a link clicked inside an app, on the
instance's display instead of in a window on the host desktop.
"""
import os
import re
import shlex
import shutil
from xml.sax.saxutils import escape, quoteattr

SHADOW_KEY = "X-Glovebox-Shadow"
GROUPS = (("Network", "Internet"), ("Office", "Office"), ("Graphics", "Graphics"),
          ("AudioVideo", "Multimedia"), ("Audio", "Multimedia"), ("Video", "Multimedia"),
          ("Development", "Development"), ("Education", "Education"), ("Science", "Science"),
          ("Game", "Games"), ("Settings", "Settings"), ("System", "System"), ("Utility", "Accessories"))
GROUP_ORDER = ("Internet", "Office", "Graphics", "Multimedia", "Development", "Education", "Science",
               "Games", "Accessories", "System", "Settings", "Other")
MENU_MAX = 400     # entries in one menu; a machine with more is cut (alphabetically) rather than unusable
_FIELD = re.compile(r"%[fFuUdDnNickvm]")


def data_dirs(env=None) -> list:
    env = os.environ if env is None else env
    home = env.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    dirs = [home] + [d for d in (env.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share").split(":") if d]
    dirs += ["/usr/local/share", "/usr/share", "/var/lib/snapd/desktop",
             "/var/lib/flatpak/exports/share", os.path.join(home, "flatpak/exports/share")]
    return list(dict.fromkeys(os.path.join(d, "applications") for d in dirs))


def parse(path: str) -> dict:
    """The [Desktop Entry] group of a .desktop file as a dict ({} when unreadable)."""
    out, inside = {}, False
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("["):
                    inside = line == "[Desktop Entry]"
                elif inside and "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    out[k.strip()] = v.strip()
    except OSError:
        return {}
    return out


def exec_argv(exec_line: str, name: str = "") -> list:
    """argv of an Exec= line with the field codes removed (%f %U … stand for files the menu does
    not have; %c is the app's name, %% a literal percent). [] when it cannot be parsed."""
    try:
        toks = shlex.split(exec_line)
    except ValueError:
        return []
    out = []
    for t in toks:
        if _FIELD.fullmatch(t):
            if t == "%c" and name:
                out.append(name)
            continue
        out.append(_FIELD.sub("", t).replace("%%", "%"))
    return out


def group_of(categories: str) -> str:
    cats = [c for c in categories.split(";") if c]
    for key, label in GROUPS:
        if key in cats:
            return label
    return "Other"


def _lang(env) -> list:
    loc = (env.get("LC_MESSAGES") or env.get("LC_ALL") or env.get("LANG") or "").split(".")[0]
    return [x for x in dict.fromkeys((loc, loc.split("_")[0])) if x and x != "C"]


def entries(env=None) -> list:
    """Launchable GUI apps: [{id, name, argv, group, path}], sorted by name. Skipped: hidden and
    NoDisplay entries, entries tied to one desktop environment (OnlyShowIn — their settings
    panels need that desktop's session), terminal programs when no xterm is installed, and
    entries whose program is not installed. The first directory that has an id wins, as in the
    XDG spec; glovebox's own shadow entries are passed over so the real one is read."""
    env = os.environ if env is None else env
    langs, seen, out = _lang(env), set(), []
    xterm = shutil.which("xterm")
    for d in data_dirs(env):
        try:
            names = sorted(os.listdir(d))
        except OSError:
            continue
        for fn in names:
            if not fn.endswith(".desktop") or fn in seen:
                continue
            e = parse(os.path.join(d, fn))
            if not e or e.get(SHADOW_KEY) == "true":
                continue
            seen.add(fn)
            if (e.get("Type", "Application") != "Application" or e.get("NoDisplay") == "true"
                    or e.get("Hidden") == "true" or e.get("OnlyShowIn")):
                continue
            name = next((e[f"Name[{l}]"] for l in langs if e.get(f"Name[{l}]")), e.get("Name") or fn[:-8])
            argv = exec_argv(e.get("Exec", ""), name)
            if not argv or not shutil.which(argv[0]) or (e.get("TryExec") and not shutil.which(e["TryExec"])):
                continue
            if e.get("Terminal") == "true":
                if not xterm:
                    continue
                argv = ["xterm", "-T", name, "-e"] + argv
            out.append({"id": fn, "name": name, "argv": argv, "group": group_of(e.get("Categories", "")),
                        "path": os.path.join(d, fn)})
    out.sort(key=lambda a: a["name"].casefold())
    return out[:MENU_MAX]


def grouped(apps: list) -> list:
    """[(group label, [apps])] in menu order, empty groups left out."""
    by = {}
    for a in apps:
        by.setdefault(a["group"], []).append(a)
    return [(g, by[g]) for g in GROUP_ORDER if g in by]


def run_argv(py: str, server: str, n: int, argv: list, wait_window: int = 0) -> list:
    """The command that starts `argv` INSIDE instance n (see server.py `run`)."""
    return [py, server, "run", str(int(n))] + (["--wait-window", str(wait_window)] if wait_window else []) + ["--"] + argv


def openbox_menu(n: int, py: str, server: str, apps: list = None) -> str:
    """menu.xml for instance n's window manager: right-click on the empty desktop lists the apps
    by group and the open windows. Every item starts through `server.py run`."""
    apps = entries() if apps is None else apps
    rows = ['<?xml version="1.0" encoding="UTF-8"?>', '<openbox_menu xmlns="http://openbox.org/3.4/menu">',
            f'<menu id="root-menu" label={quoteattr(f"Glovebox :{int(n)}")}>']
    for i, (group, items) in enumerate(grouped(apps)):
        rows.append(f'  <menu id="glovebox-apps-{i}" label={quoteattr(group)}>')
        for a in items:
            cmd = shlex.join(run_argv(py, server, n, a["argv"]))
            rows.append(f'    <item label={quoteattr(a["name"].replace("_", "__"))}><action name="Execute">'
                        f'<command>{escape(cmd)}</command></action></item>')
        rows.append("  </menu>")
    rows += ["  <separator/>", '  <menu id="client-list-combined-menu"/>', "</menu>", "</openbox_menu>"]
    return "\n".join(rows) + "\n"


def openbox_rc(menu_path: str, env=None) -> str:
    """The person's (or the system's) openbox rc.xml with only the menu file replaced — keys,
    theme and mouse bindings stay what they were. '' when no rc.xml is installed (openbox then
    runs on its built-in defaults, which have no desktop menu to replace)."""
    env = os.environ if env is None else env
    conf = env.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    for p in (os.path.join(conf, "openbox", "rc.xml"), "/etc/xdg/openbox/rc.xml"):
        try:
            with open(p, encoding="utf-8", errors="replace") as fh:
                rc = fh.read()
        except OSError:
            continue
        # the settings block, not the <menu>client-menu</menu> inside a key or mouse binding
        m = next((x for x in re.finditer(r"<menu>.*?</menu>", rc, re.S) if "<file>" in x.group(0)), None)
        if not m:
            continue
        files = list(re.finditer(r"[ \t]*<file>[^<]*</file>[ \t]*\n?", m.group(0)))
        block = m.group(0)
        new = block[:files[0].start()] + f"    <file>{escape(menu_path)}</file>\n" + block[files[-1].end():]
        return rc[:m.start()] + new + rc[m.end():]
    return ""


def _q(arg: str) -> str:
    """One argument in .desktop Exec quoting."""
    if re.fullmatch(r"[A-Za-z0-9_@%+=:,./-]+", arg):
        return arg
    return '"' + re.sub(r'(["`$\\])', r"\\\1", arg) + '"'


def write_shadow(dest: str, n: int, py: str, server: str, programs, env=None) -> int:
    """Write overriding .desktop files into dest/applications for every installed entry whose
    program is in `programs`; returns how many. With `dest` first in XDG_DATA_DIRS, a default
    handler looked up BY AN APP (a link clicked in a mail client, xdg-open, gio) resolves to the
    override, whose Exec goes through `server.py run` — so the browser opens on this instance
    with the instance's profile instead of handing the URL to the browser on the host desktop.
    An entry in the person's own ~/.local/share/applications still comes first (XDG order)."""
    appdir = os.path.join(dest, "applications")
    os.makedirs(appdir, exist_ok=True)
    prefix = " ".join(_q(a) for a in (py, server, "run", str(int(n)), "--")) + " "
    seen, count = set(), 0
    for d in data_dirs(env):
        if os.path.abspath(d) == os.path.abspath(appdir):
            continue
        try:
            names = sorted(os.listdir(d))
        except OSError:
            continue
        for fn in names:
            if not fn.endswith(".desktop") or fn in seen:
                continue
            path = os.path.join(d, fn)
            e = parse(path)
            if not e or e.get(SHADOW_KEY) == "true":
                continue
            seen.add(fn)
            argv = exec_argv(e.get("Exec", ""))
            if not argv or os.path.basename(argv[0]) not in programs:
                continue
            try:
                with open(path, encoding="utf-8", errors="replace") as fh:
                    lines = fh.read().splitlines()
            except OSError:
                continue
            out = []
            for line in lines:
                if line.startswith("Exec="):
                    line = "Exec=" + prefix + line[5:]
                elif line.startswith("DBusActivatable="):
                    line = "DBusActivatable=false"      # bus activation would skip Exec altogether
                out.append(line)
                if line.strip() == "[Desktop Entry]":
                    out.append(f"{SHADOW_KEY}=true")
            with open(os.path.join(appdir, fn), "w", encoding="utf-8") as fh:
                fh.write("\n".join(out) + "\n")
            count += 1
    return count


if __name__ == "__main__":
    import json
    import sys
    if sys.argv[1:2] == ["list"]:
        print(json.dumps([{k: a[k] for k in ("name", "group", "argv")} for a in entries()], ensure_ascii=False, indent=1))
    else:
        print("usage: appmenu.py list", file=sys.stderr)
        sys.exit(2)
