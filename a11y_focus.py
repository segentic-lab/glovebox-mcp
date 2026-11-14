#!/usr/bin/env python3
"""Ask an instance's accessibility bus (AT-SPI) which widget has keyboard focus.

Run by server.py under the SYSTEM python3 (the venv has no PyGObject), with DISPLAY=:N and
AT_SPI_BUS_ADDRESS set to that display's own bus — never the host's, whose focus would be a lie.
Prints one JSON line:
  {"ok": true, "focus": {"role", "name", "rect": [x, y, w, h], "editable", "password", "app"} | null}
  {"ok": false, "error": "..."}
"focus": null = the bus answered but no focused widget is exposed (Xlib apps, canvases, games).
Stdlib + PyGObject only; bounded by BUDGET_S and MAX_NODES so a huge tree cannot hang a fill()."""
import json
import sys
import time

BUDGET_S = 1.5
MAX_NODES = 4000
EDITABLE_ROLES = {"entry", "text", "password text", "spin button", "terminal", "paragraph",
                  "document text", "editbar", "autocomplete"}


def main() -> dict:
    try:
        import gi
        gi.require_version("Atspi", "2.0")
        from gi.repository import Atspi
    except Exception as e:   # ImportError, ValueError (typelib missing)
        return {"ok": False, "error": f"PyGObject/Atspi not importable: {e}"}
    if "--check" in sys.argv:
        return {"ok": True}
    Atspi.init()
    S = Atspi.StateType
    deadline = time.time() + BUDGET_S
    seen = [0]

    def focused_in(o, depth=0):
        """Deepest FOCUSED descendant of o (depth-first), or None."""
        if o is None or depth > 40 or seen[0] > MAX_NODES or time.time() > deadline:
            return None
        seen[0] += 1
        try:
            n = o.get_child_count()
        except Exception:
            return None
        for i in range(min(n, 500)):
            try:
                c = o.get_child_at_index(i)
            except Exception:
                continue
            hit = focused_in(c, depth + 1)
            if hit is not None:
                return hit
        try:
            return o if o.get_state_set().contains(S.FOCUSED) else None
        except Exception:
            return None

    desktop = Atspi.get_desktop(0)
    windows = []   # (active, app name, window)
    for i in range(desktop.get_child_count()):
        try:
            app = desktop.get_child_at_index(i)
            for j in range(app.get_child_count()):
                w = app.get_child_at_index(j)
                windows.append((w.get_state_set().contains(S.ACTIVE), app.get_name() or "", w))
        except Exception:
            continue
    windows.sort(key=lambda t: not t[0])    # the active window's focus is the one keys go to
    for _active, app, w in windows:
        o = focused_in(w)
        if o is None:
            continue
        st = o.get_state_set()
        role = o.get_role_name() or ""
        try:
            e = o.get_extents(Atspi.CoordType.SCREEN)
            rect = [e.x, e.y, e.width, e.height]
        except Exception:
            rect = None
        return {"ok": True, "focus": {
            "role": role, "name": (o.get_name() or "")[:80], "rect": rect,
            "editable": bool(st.contains(S.EDITABLE) or role in EDITABLE_ROLES),
            "password": role == "password text", "app": app}}
    return {"ok": True, "focus": None, "truncated": seen[0] > MAX_NODES or time.time() > deadline}


if __name__ == "__main__":
    try:
        out = main()
    except Exception as e:
        out = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    print(json.dumps(out, ensure_ascii=False))
