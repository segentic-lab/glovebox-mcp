#!/usr/bin/env python3
"""Ask an instance's accessibility bus (AT-SPI) which widget has keyboard focus.

Run by server.py under the SYSTEM python3 (the venv has no PyGObject), with DISPLAY=:N and
AT_SPI_BUS_ADDRESS set to that display's own bus — never the host's, whose focus would be a lie.
Optional arguments `X Y`: the point that was just clicked (see the tie-break in main()).
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
# roles that only say "the focus is somewhere in here" (LibreOffice reports its window frame)
CONTAINER_ROLES = {"frame", "window", "dialog", "panel", "filler", "application", "scroll pane", "split pane",
                   "layered pane", "root pane", "internal frame", "desktop frame", "viewport", "document frame",
                   "option pane", "glass pane", "redundant object", "unknown", "invalid"}
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

    def focused_in(o, found, depth=0):
        """Collect the SHOWING + FOCUSED descendants of o (depth-first, deepest first)."""
        if o is None or depth > 40 or seen[0] > MAX_NODES or time.time() > deadline or len(found) >= 8:
            return
        seen[0] += 1
        try:
            n = o.get_child_count()
        except Exception:
            return
        for i in range(min(n, 500)):
            try:
                c = o.get_child_at_index(i)
            except Exception:
                continue
            focused_in(c, found, depth + 1)
        try:
            st = o.get_state_set()
            if st.contains(S.FOCUSED) and st.contains(S.SHOWING):
                found.append(o)
        except Exception:
            pass

    def describe(o, app):
        st = o.get_state_set()
        role = o.get_role_name() or ""
        try:
            e = o.get_extents(Atspi.CoordType.SCREEN)
            rect = [e.x, e.y, e.width, e.height]
        except Exception:
            rect = None
        return {"role": role, "name": (o.get_name() or "")[:80], "rect": rect,
                "editable": bool(st.contains(S.EDITABLE) or role in EDITABLE_ROLES),
                "password": role == "password text", "app": app}

    # optional click point: toolkits sometimes leave FOCUSED set on a widget that lost the focus
    # (Qt: the current item of a combo box), so when several widgets claim it, the one under the
    # point is the one the click just focused
    pt = None
    nums = [a for a in sys.argv[1:] if a.lstrip("-").isdigit()]
    if len(nums) >= 2:
        pt = (int(nums[0]), int(nums[1]))

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
        found = []
        focused_in(w, found)
        if not found:
            continue
        cands = [describe(o, app) for o in found]

        def under(c):
            r = c["rect"]
            return bool(pt and r and r[2] > 0 and r[3] > 0 and r[0] <= pt[0] < r[0] + r[2] and r[1] <= pt[1] < r[1] + r[3])
        cands.sort(key=lambda c: (not under(c), not c["editable"]))   # stable: deepest first otherwise
        out = {"ok": True, "focus": cands[0], "candidates": len(cands)}
        if cands[0]["role"] in CONTAINER_ROLES and not cands[0]["editable"]:
            out["focus"]["container"] = True
            if pt:      # what does the app say is AT the clicked point?
                try:
                    o, last = w, None
                    for _ in range(40):
                        comp = o.get_component_iface()
                        c = comp.get_accessible_at_point(pt[0], pt[1], Atspi.CoordType.SCREEN) if comp else None
                        if c is None or c == o:
                            break
                        o = last = c
                    if last is not None:
                        out["at_point"] = describe(last, app)
                except Exception:
                    pass
        return out
    return {"ok": True, "focus": None, "truncated": seen[0] > MAX_NODES or time.time() > deadline}


if __name__ == "__main__":
    try:
        out = main()
    except Exception as e:
        out = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    print(json.dumps(out, ensure_ascii=False))
