#!/usr/bin/env python3
"""Read and operate an instance's native accessibility tree (AT-SPI).

Run by a11y.py under the SYSTEM python3 (the venv has no PyGObject), with DISPLAY=:N and
AT_SPI_BUS_ADDRESS set to that display's own bus — see a11y_focus.py. Commands:
  dump                                   → {"ok", "elements": [...], "truncated", "apps": [...]}
  locate PATH ROLE NAME                  → {"ok", "center", "box", "states", "value", "showing"}
  act    PATH ROLE NAME ACTION [VALUE]   → {"ok", "method", "before", "after", ...}
PATH is the JSON list of child indexes from the desktop; ROLE and NAME are what the element was
when it was listed — a mismatch answers {"stale": why} instead of acting on a different widget.
Prints one JSON line. Stdlib + PyGObject only; bounded by BUDGET_S / MAX_NODES / MAX_ELEMENTS so
a huge tree (a spreadsheet, a long document) cannot hang a call."""
import json
import sys
import time
import warnings

warnings.simplefilter("ignore")   # PyGObject deprecation notes would only pollute stderr

BUDGET_S = 4.0
MAX_NODES = 8000
MAX_ELEMENTS = 1500
ROLE_TYPE = {
    "push button": "button", "button": "button", "toggle button": "button", "push button menu": "button",
    "check box": "checkbox", "radio button": "radio", "toggle switch": "switch", "switch": "switch",
    "entry": "input", "password text": "input", "spin button": "input", "editbar": "input",
    "terminal": "input", "autocomplete": "input", "date editor": "input",
    "combo box": "select", "list box": "select", "slider": "slider", "page tab": "tab", "link": "link",
    "menu": "item", "menu item": "item", "check menu item": "item", "radio menu item": "item",
    "list item": "item", "tree item": "item", "table cell": "item", "icon": "item",
    "column header": "item", "table column header": "item", "row header": "item",
    "label": "text", "static": "text", "heading": "text", "caption": "text", "status bar": "text",
    "info bar": "text", "notification": "text", "alert": "text", "tool tip": "text",
    "image": "image",
}
TEXTUAL = {"text", "paragraph", "document text", "document web", "section", "block quote"}   # input when editable, else text
PRESS_NAMES = ("click", "press", "activate", "toggle", "jump", "open", "select", "expand or contract", "do default")


def _atspi():
    import gi
    gi.require_version("Atspi", "2.0")
    from gi.repository import Atspi
    Atspi.init()
    return Atspi


def _states(Atspi, o):
    S = Atspi.StateType
    st = o.get_state_set()
    out = []
    for flag, name in ((S.FOCUSED, "focused"), (S.CHECKED, "checked"), (S.INDETERMINATE, "mixed"),
                       (S.PRESSED, "pressed"), (S.EXPANDED, "expanded"), (S.SELECTED, "selected"),
                       (S.REQUIRED, "required")):
        if st.contains(flag):
            out.append(name)
    if st.contains(S.EXPANDABLE) and not st.contains(S.EXPANDED):
        out.append("collapsed")
    if not (st.contains(S.SENSITIVE) and st.contains(S.ENABLED)):
        out.append("disabled")
    if (o.get_role_name() or "") == "password text":
        out.append("password")
    return out, st


def _text(o, cap=200):
    try:
        from gi.repository import Atspi
        t = o.get_text_iface()
        if t is None:
            return None
        n = Atspi.Text.get_character_count(t)
        # through the interface class: on the object itself, get_text resolves to the deprecated
        # zero-argument Accessible.get_text and raises
        return Atspi.Text.get_text(t, 0, min(n, cap)) if n > 0 else ""
    except Exception:
        return None


def _value(Atspi, o, role, editable):
    if role == "password text":
        return None
    if editable or role in ("entry", "spin button", "editbar", "autocomplete"):
        t = _text(o)
        if t is not None:
            return t
    try:
        v = o.get_value_iface()
        if v is not None:
            return v.get_current_value()
    except Exception:
        pass
    return None


def _extents(Atspi, o):
    try:
        e = o.get_extents(Atspi.CoordType.SCREEN)
        return e.x, e.y, e.width, e.height
    except Exception:
        return None


def _actions(o):
    try:
        a = o.get_action_iface()
        if a is None:
            return []
        return [(a.get_action_name(i) or "").lower() for i in range(min(a.get_n_actions(), 6))]
    except Exception:
        return []


def _describe(Atspi, o):
    """type/role/label/… of one accessible, or None when it is not something to list."""
    role = o.get_role_name() or ""
    states, st = _states(Atspi, o)
    S = Atspi.StateType
    editable = st.contains(S.EDITABLE)
    if role in TEXTUAL:
        typ = "input" if editable else "text"
    else:
        typ = ROLE_TYPE.get(role)
    if not typ:
        return None
    name = (o.get_name() or "").strip() or (o.get_description() or "").strip()
    value = _value(Atspi, o, role, editable) if typ in ("input", "slider", "select") else None
    if not name and typ == "text":
        name = (_text(o, 160) or "").strip()
    if not name and typ in ("text", "image"):
        return None
    if editable and "editable" not in states:
        states.append("editable")
    d = {"type": typ, "role": role, "label": " ".join(name.split())[:160],
         "interactive": typ not in ("text", "image") and "disabled" not in states}
    if states:
        d["states"] = states
    if value is not None:
        d["value"] = str(value)[:200]
    return d


def dump(Atspi):
    S = Atspi.StateType
    deadline = time.time() + BUDGET_S
    seen = [0]
    out, apps = [], []
    truncated = [False]

    def walk(o, path, depth):
        if depth > 60:
            return
        if seen[0] >= MAX_NODES or len(out) >= MAX_ELEMENTS or time.time() > deadline:
            truncated[0] = True
            return
        seen[0] += 1
        try:
            st = o.get_state_set()
        except Exception:
            return
        showing = st.contains(S.SHOWING)
        if depth > 1 and not (showing or st.contains(S.VISIBLE)):
            return                                   # a closed menu, a hidden tab page
        if showing:
            try:
                d = _describe(Atspi, o)
            except Exception:
                d = None
            ext = _extents(Atspi, o) if d else None
            if d and ext and ext[2] > 0 and ext[3] > 0:
                x, y, w, h = ext
                d.update(path=path, center=[x + w // 2, y + h // 2], box=[x, y, x + w, y + h], onscreen=True)
                if d["interactive"]:
                    acts = _actions(o)
                    if acts:
                        d["actions"] = acts
                out.append(d)
        if st.contains(S.MANAGES_DESCENDANTS):
            return                                   # a spreadsheet: millions of virtual cells
        if (o.get_role_name() or "") == "combo box" and not st.contains(S.EXPANDED):
            return                                   # a closed drop-down still calls its items "showing"
        try:
            n = o.get_child_count()
        except Exception:
            return
        for i in range(min(n, 400)):
            try:
                c = o.get_child_at_index(i)
            except Exception:
                continue
            if c is not None:
                walk(c, path + [i], depth + 1)

    desktop = Atspi.get_desktop(0)
    wins = []
    for i in range(desktop.get_child_count()):
        try:
            app = desktop.get_child_at_index(i)
            name = app.get_name() or ""
            for j in range(app.get_child_count()):
                w = app.get_child_at_index(j)
                st = w.get_state_set()
                if st.contains(S.SHOWING) or st.contains(S.VISIBLE):
                    wins.append((not st.contains(S.ACTIVE), i, j, name, w))
        except Exception:
            continue
    wins.sort(key=lambda t: t[:3])                   # the active window first
    for _inactive, i, j, name, w in wins:
        if name not in apps:
            apps.append(name)
        walk(w, [i, j], 1)
    for k, d in enumerate(out):
        d["id"] = k
    return {"ok": True, "elements": out, "truncated": truncated[0], "apps": apps, "nodes": seen[0]}


def _resolve(Atspi, path, role, name):
    o = Atspi.get_desktop(0)
    try:
        for i in path:
            o = o.get_child_at_index(int(i))
            if o is None:
                raise IndexError
    except Exception:
        return None, "the element is no longer in the app's accessibility tree"
    if (o.get_role_name() or "") != role:
        return None, f"the element at that position is now a {o.get_role_name()!r}, not the {role!r} that was listed"
    now = " ".join(((o.get_name() or "").strip() or (o.get_description() or "").strip()).split())[:160]
    if name and now and now != name:
        return None, f"the element at that position is now named {now[:40]!r}, not {name[:40]!r}"
    return o, ""


def _snap(Atspi, o):
    S = Atspi.StateType
    role = o.get_role_name() or ""
    states, st = _states(Atspi, o)
    out = {"states": states, "showing": st.contains(S.SHOWING)}
    v = _value(Atspi, o, role, st.contains(S.EDITABLE))
    if v is not None:
        out["value"] = str(v)[:200]
    return out


def locate(Atspi, o):
    out = {"ok": True, **_snap(Atspi, o)}
    ext = _extents(Atspi, o)
    if ext and ext[2] > 0 and ext[3] > 0 and out["showing"]:
        x, y, w, h = ext
        out.update(center=[x + w // 2, y + h // 2], box=[x, y, x + w, y + h])
    return out


def act(Atspi, o, action, value):
    before = _snap(Atspi, o)
    out = {"ok": True, "method": "at-spi", "before": {k: before.get(k) for k in ("states", "value")}}
    role = o.get_role_name() or ""
    if action in ("press", "set_value") and "disabled" in before["states"]:
        return {"ok": False, "error": "the element is disabled"}
    if action == "press":
        a = o.get_action_iface()
        names = _actions(o)
        if a is None or not names:
            return {"ok": False, "error": "the app exposes no action for this element", "no_action": True}
        idx = next((names.index(n) for n in PRESS_NAMES if n in names), 0)
        if not a.do_action(idx):
            return {"ok": False, "error": f"the app refused the '{names[idx]}' action"}
        out["at_spi_action"] = names[idx]
    elif action == "focus":
        c = o.get_component_iface()
        if c is None or not c.grab_focus():
            return {"ok": False, "error": "the element cannot take the focus"}
    elif action == "scroll_into_view":
        c = o.get_component_iface()
        try:
            ok = bool(c and c.scroll_to(Atspi.ScrollType.ANYWHERE))
        except Exception:
            ok = False
        if not ok:
            return {"ok": False, "error": "this toolkit cannot scroll an element into view over AT-SPI"}
    elif action == "set_value":
        if role in ("check box", "radio button", "toggle button", "check menu item", "toggle switch", "switch"):
            want = value.strip().lower()
            if want not in ("true", "false", "1", "0", "on", "off", "yes", "no", "checked", "unchecked"):
                return {"ok": False, "error": f"a {role} takes value 'true' or 'false'"}
            on = want in ("true", "1", "on", "yes", "checked")
            if ("checked" in before["states"] or "pressed" in before["states"]) != on:
                a = o.get_action_iface()
                if a is None or a.get_n_actions() < 1 or not a.do_action(0):
                    return {"ok": False, "error": "the app exposes no toggle action for this element", "no_action": True}
        else:
            done = False
            try:
                e = o.get_editable_text_iface()
                if e is not None:
                    done = bool(e.set_text_contents(value))
            except Exception:
                done = False
            if not done:
                try:
                    v = o.get_value_iface()
                    if v is not None:
                        v.set_current_value(float(value.replace(",", ".")))
                        done = True
                except Exception:
                    done = False
            if not done:
                how = (" — press it (act) and pick the item from the list that opens" if role in ("combo box", "list box")
                       else "")
                return {"ok": False, "error": f"this {role} does not accept a value over AT-SPI{how}", "no_action": True}
    else:
        return {"ok": False, "error": f"unknown action '{action}'"}
    time.sleep(0.15)                                 # let the toolkit update the states
    after = locate(Atspi, o)
    out["after"] = {k: after.get(k) for k in ("states", "value")}
    if after.get("center"):
        out["center"] = after["center"]
    return out


def main(argv):
    try:
        Atspi = _atspi()
    except Exception as e:   # ImportError, ValueError (typelib missing)
        return {"ok": False, "error": f"PyGObject/Atspi not importable: {e}"}
    if not argv or argv[0] == "dump":
        return dump(Atspi)
    if argv[0] in ("locate", "act") and len(argv) >= 4:
        o, why = _resolve(Atspi, json.loads(argv[1]), argv[2], argv[3])
        if o is None:
            return {"ok": False, "stale": why}
        if argv[0] == "locate":
            return locate(Atspi, o)
        return act(Atspi, o, argv[4] if len(argv) > 4 else "press", argv[5] if len(argv) > 5 else "")
    return {"ok": False, "error": "usage: a11y_tree.py dump | locate PATH ROLE NAME | act PATH ROLE NAME ACTION [VALUE]"}


if __name__ == "__main__":
    try:
        out = main(sys.argv[1:])
    except Exception as e:
        out = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    print(json.dumps(out, ensure_ascii=False))
