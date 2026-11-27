"""The apps menu, the launcher and the rules that keep what an instance starts inside it."""
import json
import os
import subprocess
import sys
import xml.dom.minidom

import pytest

import appmenu
import server
from conftest import REPO


def _desktop(d, name, body):
    os.makedirs(os.path.join(d, "applications"), exist_ok=True)
    with open(os.path.join(d, "applications", name), "w") as fh:
        fh.write("[Desktop Entry]\nType=Application\n" + body)


@pytest.fixture
def apps(tmp_path, monkeypatch):
    """A machine with five desktop entries; `sh`/`true` stand in for installed programs."""
    d = str(tmp_path / "share")
    _desktop(d, "web.desktop", "Name=Web & More\nName[sl]=Splet\nExec=sh -c 'x' %U\nCategories=Network;WebBrowser;\n"
                               "\n[Desktop Action new]\nName=New\nExec=sh --new\n")
    _desktop(d, "calc.desktop", "Name=Calc\nExec=true %f\nCategories=Office;\nDBusActivatable=true\n")
    _desktop(d, "hidden.desktop", "Name=Hidden\nExec=true\nNoDisplay=true\n")
    _desktop(d, "gnome.desktop", "Name=Gnome only\nExec=true\nOnlyShowIn=GNOME;\n")
    _desktop(d, "missing.desktop", "Name=Missing\nExec=no-such-program-xyz\n")
    env = {"XDG_DATA_HOME": str(tmp_path / "home"), "XDG_DATA_DIRS": d, "LANG": "sl_SI.UTF-8"}
    monkeypatch.setattr(appmenu, "data_dirs", lambda e=None: [os.path.join(d, "applications")])
    return d, env


def test_exec_field_codes_are_removed():
    assert appmenu.exec_argv("gimp-2.10 %U") == ["gimp-2.10"]
    assert appmenu.exec_argv('"/opt/My App/run" --name=%c --pct=100%% %F', "App") == ["/opt/My App/run", "--name=", "--pct=100%"]
    assert appmenu.exec_argv("prog %c", "The Name") == ["prog", "The Name"]
    assert appmenu.exec_argv('broken "quote') == []


def test_entries_keep_only_launchable_apps(apps):
    _, env = apps
    got = appmenu.entries(env)
    assert [a["id"] for a in got] == ["calc.desktop", "web.desktop"]          # sorted by shown name: Calc, Splet
    web = got[1]
    assert web["name"] == "Splet" and web["group"] == "Internet" and web["argv"] == ["sh", "-c", "x"]
    assert [g for g, _ in appmenu.grouped(got)] == ["Internet", "Office"]


def test_openbox_menu_is_valid_xml_and_goes_through_the_launcher(apps):
    _, env = apps
    xmltext = appmenu.openbox_menu(7, "/venv/python", "/repo/server.py", appmenu.entries(env))
    doc = xml.dom.minidom.parseString(xmltext)          # '&' in a name must not break the file
    cmds = [c.firstChild.data for c in doc.getElementsByTagName("command")]
    assert cmds and all(c.startswith("/venv/python /repo/server.py run 7 -- ") for c in cmds)
    ids = [m.getAttribute("id") for m in doc.getElementsByTagName("menu")]
    assert ids[0] == "root-menu" and "client-list-combined-menu" in ids       # apps + the open windows


def test_openbox_rc_replaces_only_the_menu_files(tmp_path):
    conf = tmp_path / "openbox"
    conf.mkdir()
    (conf / "rc.xml").write_text(
        "<openbox_config>\n<keyboard><keybind key='A-space'><action name='ShowMenu'><menu>client-menu</menu></action>"
        "</keybind></keyboard>\n<menu>\n  <file>/var/lib/openbox/debian-menu.xml</file>\n  <file>menu.xml</file>\n"
        "  <hideDelay>200</hideDelay>\n</menu>\n</openbox_config>\n")
    rc = appmenu.openbox_rc("/tmp/x/menu.xml", {"XDG_CONFIG_HOME": str(tmp_path)})
    assert rc.count("<file>") == 1 and "<file>/tmp/x/menu.xml</file>" in rc
    assert "<menu>client-menu</menu>" in rc and "<hideDelay>200</hideDelay>" in rc
    xml.dom.minidom.parseString(rc)


def test_shadow_entries_wrap_every_exec_line(apps, tmp_path):
    _, env = apps
    dest = str(tmp_path / "shadow")
    assert appmenu.write_shadow(dest, 4, "/venv/python", "/repo/server.py", {"sh"}, env) == 1
    text = open(os.path.join(dest, "applications", "web.desktop")).read()
    assert "X-Glovebox-Shadow=true" in text
    execs = [l for l in text.splitlines() if l.startswith("Exec=")]
    assert execs == ["Exec=/venv/python /repo/server.py run 4 -- sh -c 'x' %U",          # %U kept for the URL
                     "Exec=/venv/python /repo/server.py run 4 -- sh --new"]
    assert not os.path.exists(os.path.join(dest, "applications", "calc.desktop"))        # not a hand-over program


def test_shadow_entries_never_shadow_themselves(apps, tmp_path, monkeypatch):
    d, env = apps
    dest = str(tmp_path / "shadow")
    appmenu.write_shadow(dest, 4, "/p", "/s", {"sh"}, env)
    monkeypatch.setattr(appmenu, "data_dirs", lambda e=None: [os.path.join(dest, "applications"), os.path.join(d, "applications")])
    assert appmenu.write_shadow(dest, 4, "/p", "/s", {"sh"}, env) == 1
    text = open(os.path.join(dest, "applications", "web.desktop")).read()
    assert text.count("run 4 --") == 2                       # wrapped once, not twice
    assert [a["argv"] for a in appmenu.entries(env) if a["id"] == "web.desktop"] == [["sh", "-c", "x"]]


@pytest.fixture
def no_shadow(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "HERE", str(tmp_path))
    monkeypatch.setattr(server, "_shadow_env", lambda n, env: None)


def test_firefox_gets_a_profile_per_instance(no_shadow, tmp_path):
    env, args, base = server._app_env_args(5, "firefox https://example.org")
    assert base == "firefox"
    i = args.index("firefox")
    assert args[i + 1:i + 3] == ["--profile", str(tmp_path / "profiles" / "firefox-5")] and args[-1] == "https://example.org"
    assert os.path.isdir(tmp_path / "profiles" / "firefox-5")
    assert "WAYLAND_DISPLAY" not in env and env["DISPLAY"] == ":5"
    _, args, _ = server._app_env_args(5, "firefox -P work")              # the caller's own choice is kept
    assert "--profile" not in args


def test_snap_programs_keep_the_session_bus(no_shadow, monkeypatch):
    monkeypatch.setattr(server.shutil, "which", lambda p: "/snap/bin/" + p if p == "snapapp" else "/usr/bin/" + p)
    assert server._app_env_args(5, "snapapp")[1] == ["snapapp"]
    assert server._app_env_args(5, "plainapp")[1][:2] == ["dbus-run-session", "--"]


def test_app_environment_puts_the_shadow_entries_first(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "TMP", str(tmp_path))
    monkeypatch.setattr(server, "_SHADOWED", set())
    monkeypatch.setattr(appmenu, "write_shadow", lambda *a, **k: 0)
    monkeypatch.setenv("XDG_DATA_DIRS", "/usr/local/share:/usr/share")
    env, _, _ = server._app_env_args(6, "true")
    assert env["XDG_DATA_DIRS"].split(":") == [str(tmp_path / "desktop-6"), "/usr/local/share", "/usr/share"]
    env2 = {**env}
    server._shadow_env(6, env2)                                          # nested launch: not added twice
    assert env2["XDG_DATA_DIRS"] == env["XDG_DATA_DIRS"]


def test_empty_command_needs_the_panel(monkeypatch):
    monkeypatch.setattr(server, "PANEL_DEFAULT", False)
    with pytest.raises(server.ToolError) as e:
        server.launch_app("")
    p = json.loads(str(e.value))
    assert p["ok"] is False and "panel=true" in p["fix"] and "nothing was started" in p["error"]


def test_run_cli_reports_bad_usage_and_a_dead_instance():
    env = {**os.environ, "GLOVEBOX_VISION": "none", "GLOVEBOX_MODE": ""}
    r = subprocess.run([sys.executable, os.path.join(REPO, "server.py"), "run"], capture_output=True, text=True, env=env)
    assert r.returncode == 2 and json.loads(r.stdout)["ok"] is False
    r = subprocess.run([sys.executable, os.path.join(REPO, "server.py"), "run", "97", "--", "true"],
                       capture_output=True, text=True, env=env)
    out = json.loads(r.stdout)
    assert r.returncode == 1 and out == {"ok": False, "instance": 97, "error": "instance 97 is not running"}


def test_panel_check_runs_under_the_system_python():
    r = subprocess.run(["/usr/bin/python3", os.path.join(REPO, "panel.py"), "--check"], capture_output=True)
    assert r.returncode in (0, 1)                # 1 = GTK 3 missing: a clean answer, never a traceback
    assert b"Traceback" not in r.stderr
