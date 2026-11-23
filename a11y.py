"""Accessibility grounding: the elements an app itself says it has, instead of pixels.

Two sources, one element shape ({id, type, role, label, interactive, center, onscreen, states, value?}):
  browser-dom  a Chromium-family browser over CDP: an in-page walk of the DOM (roles, accessible
               names, states), open shadow roots and same-origin iframes included. Chrome's own
               accessibility tree (Accessibility.getFullAXTree) is exact but takes ~4 s to build
               and ~7 s to measure on a long article; this walk takes tens of milliseconds and
               can also act on what it found.
  at-spi       native toolkits (GTK, Qt, LibreOffice) through the display's own accessibility bus,
               read by a11y_tree.py under the system python3 (see a11y_focus.py for why).
server.py owns the tools; this module owns the reading, re-locating and acting.
"""
import json
import os
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
TREE_HELPER = os.path.join(HERE, "a11y_tree.py")
MAX_ELEMENTS = 2000          # per snapshot: this many in view, and this many outside it
ACTIONS = ("press", "focus", "set_value", "scroll_into_view")

# Shared by the snapshot, locate and act scripts. Coordinates leave the page as the pixels
# click()/screenshot() use: screenX/Y + browser chrome + CSS px * devicePixelRatio.
_LIB_JS = r"""
var z = window.devicePixelRatio || 1;
var side = Math.max(0, (window.outerWidth - window.innerWidth * z) / 2);
var topb = Math.max(0, window.outerHeight - window.innerHeight * z - side);
var OX = window.screenX + side, OY = window.screenY + topb;
function frameOffset(doc) {           // viewport offset of a (same-origin) frame's document
  var x = 0, y = 0, w = doc.defaultView;
  while (w && w !== window && w.frameElement) {
    var r = w.frameElement.getBoundingClientRect();
    x += r.left + w.frameElement.clientLeft; y += r.top + w.frameElement.clientTop;
    w = w.parent;
  }
  return [x, y];
}
function composedParent(n) {
  if (n.parentNode) return n.parentNode.nodeType === 11 ? n.parentNode.host : n.parentNode;
  if (n.nodeType === 9 && n.defaultView && n.defaultView.frameElement) return n.defaultView.frameElement;
  return null;
}
function inside(a, b) { for (var n = a; n; n = composedParent(n)) if (n === b) return true; return false; }
function deepAt(x, y) {               // the innermost element at a top-viewport point
  var doc = document, e = null;
  for (var i = 0; i < 20; i++) {
    var c = doc.elementFromPoint(x, y);
    while (c && c.shadowRoot) { var s = c.shadowRoot.elementFromPoint(x, y); if (!s || s === c) break; c = s; }
    if (!c) break;
    e = c;
    if ((c.tagName === "IFRAME" || c.tagName === "FRAME")) {
      var d = null; try { d = c.contentDocument; } catch (err) {}
      if (!d) break;
      var r = c.getBoundingClientRect();
      x -= r.left + c.clientLeft; y -= r.top + c.clientTop; doc = d;
    } else break;
  }
  return e;
}
function place(el, node) {            // where the element is: top-viewport rect, visibility, hittability
  var r;
  if (node && node.isConnected) { var g = el.ownerDocument.createRange(); g.selectNodeContents(node); r = g.getBoundingClientRect(); }
  else r = el.getBoundingClientRect();
  var o = frameOffset(el.ownerDocument);
  var x = r.left + o[0], y = r.top + o[1], w = r.width, h = r.height;
  var vis = w > 0 && h > 0 && (!el.checkVisibility || el.checkVisibility({checkVisibilityCSS: true}));
  var x1 = Math.max(x, 0), y1 = Math.max(y, 0), x2 = Math.min(x + w, window.innerWidth), y2 = Math.min(y + h, window.innerHeight);
  var p = {visible: vis, inview: vis && x2 - x1 >= 1 && y2 - y1 >= 1, covered: false, css: null};
  if (p.inview) {
    var cx = (x1 + x2) / 2, cy = (y1 + y2) / 2, d = deepAt(cx, cy);
    var lab = d && d.closest ? d.closest("label") : null;
    p.covered = !(d && (inside(d, el) || inside(el, d) || (lab && lab.control === el)));
    if (p.covered && d) p.by = (d.tagName || "").toLowerCase() + (d.id ? "#" + d.id : "");
    p.css = [cx, cy];
    p.center = [Math.round(OX + cx * z), Math.round(OY + cy * z)];
    p.box = [Math.round(OX + x1 * z), Math.round(OY + y1 * z), Math.round(OX + x2 * z), Math.round(OY + y2 * z)];
  }
  return p;
}
function txt(s) { return (s || "").replace(/\s+/g, " ").trim(); }
function statesOf(el) {
  var s = [], a = function (k) { return el.getAttribute("aria-" + k); }, ty = (el.type || "").toLowerCase();
  if (el.disabled === true || a("disabled") === "true") s.push("disabled");
  if ((ty === "checkbox" || ty === "radio") ? el.checked : a("checked") === "true") s.push("checked");
  if (el.indeterminate === true || a("checked") === "mixed") s.push("mixed");
  if (a("pressed") === "true") s.push("pressed");
  if (a("expanded") === "true") s.push("expanded"); else if (a("expanded") === "false") s.push("collapsed");
  if (a("selected") === "true" || el.selected === true) s.push("selected");
  if (el.ownerDocument.activeElement === el) s.push("focused");
  if (el.required === true || a("required") === "true") s.push("required");
  if (el.readOnly === true || a("readonly") === "true") s.push("readonly");
  if (ty === "password") s.push("password");
  return s;
}
function valueOf(el) {
  var tag = el.tagName, ty = (el.type || "").toLowerCase();
  if (ty === "password") return undefined;
  if (tag === "SELECT") return txt(Array.prototype.map.call(el.selectedOptions || [], function (o) { return o.text; }).join(", "));
  if (tag === "INPUT" && (ty === "checkbox" || ty === "radio" || ty === "button" || ty === "submit" || ty === "reset" || ty === "file" || ty === "image")) return undefined;
  if (tag === "INPUT" || tag === "TEXTAREA") return String(el.value);
  if (el.isContentEditable) return String(el.innerText || "").replace(/\n$/, "");
  var v = el.getAttribute("aria-valuetext") || el.getAttribute("aria-valuenow");
  return v === null ? undefined : v;
}
"""

_SNAPSHOT_JS = r"""(function (SNAP, MAX) {
""" + _LIB_JS + r"""
var ROLE_TYPE = {button: "button", link: "link", checkbox: "checkbox", radio: "radio", "switch": "switch", tab: "tab",
  textbox: "input", searchbox: "input", spinbutton: "input", combobox: "select", listbox: "select", slider: "slider",
  menuitem: "item", menuitemcheckbox: "item", menuitemradio: "item", option: "item", treeitem: "item", gridcell: "item",
  heading: "text", img: "image"};
var SEL = 'a[href],button,summary,select,textarea,input,[role],[contenteditable=""],[contenteditable="true"],' +
          '[tabindex]:not([tabindex="-1"]),[onclick],img[alt],h1,h2,h3,h4,h5,h6';
var SKIP = {SCRIPT: 1, STYLE: 1, NOSCRIPT: 1, TEMPLATE: 1, HEAD: 1, TITLE: 1, OPTION: 1, OPTGROUP: 1};
function roleOf(el) {
  var ex = txt(el.getAttribute("role")).split(" ")[0];
  if (ex && ex !== "presentation" && ex !== "none") return ex;
  var tag = el.tagName, ty = (el.type || "").toLowerCase();
  if (tag === "A") return "link";
  if (tag === "BUTTON" || tag === "SUMMARY") return "button";
  if (tag === "SELECT") return (el.multiple || el.size > 1) ? "listbox" : "combobox";
  if (tag === "TEXTAREA") return "textbox";
  if (tag === "IMG") return "img";
  if (/^H[1-6]$/.test(tag)) return "heading";
  if (tag === "INPUT") {
    if (ty === "hidden") return "";
    if (ty === "button" || ty === "submit" || ty === "reset" || ty === "image" || ty === "file") return "button";
    if (ty === "checkbox" || ty === "radio") return ty;
    if (ty === "range") return "slider";
    if (ty === "number") return "spinbutton";
    if (ty === "search") return "searchbox";
    return el.getAttribute("list") ? "combobox" : "textbox";
  }
  if (el.isContentEditable) return "textbox";
  return "generic";                    // focusable or clickable without a role
}
function nameOf(el) {
  var lb = el.getAttribute("aria-labelledby"), t;
  if (lb) {
    t = txt(lb.split(/\s+/).map(function (id) { var e = el.ownerDocument.getElementById(id); return e ? (e.innerText || e.textContent) : ""; }).join(" "));
    if (t) return t;
  }
  t = txt(el.getAttribute("aria-label")); if (t) return t;
  var tag = el.tagName, ty = (el.type || "").toLowerCase();
  if (tag === "INPUT" || tag === "SELECT" || tag === "TEXTAREA") {
    if (tag === "INPUT" && (ty === "button" || ty === "submit" || ty === "reset"))
      return txt(el.value) || (ty === "submit" ? "Submit" : ty === "reset" ? "Reset" : "");
    if (el.labels && el.labels.length) {
      t = txt(Array.prototype.map.call(el.labels, function (l) { return l.innerText || l.textContent; }).join(" "));
      if (t) return t;
    }
    return txt(el.getAttribute("placeholder")) || txt(el.title) || txt(el.alt) || txt(el.name);
  }
  if (tag === "IMG") return txt(el.alt) || txt(el.title);
  t = txt(el.innerText);
  if (!t) t = txt(Array.prototype.map.call(el.querySelectorAll("img[alt],[aria-label]"), function (c) { return c.getAttribute("alt") || c.getAttribute("aria-label") || ""; }).join(" "));
  if (!t) { var st = el.querySelector("svg title"); if (st) t = txt(st.textContent); }
  return t || txt(el.title);
}
var recs = [], els = [], nodes = [], claimed = new WeakSet(), texts = new Map(), skipped = 0, truncated = false;
var nIn = 0, nOff = 0, DROPPED = {};
function add(rec, el, node) {
  // what is in view is never the part that gets cut: on a long page scrolled far down, the
  // first MAX elements in document order are all above the viewport
  rec.p = place(el, node);
  if (rec.p.inview ? nIn >= MAX : nOff >= MAX) { truncated = true; return false; }
  if (rec.p.inview) nIn++; else nOff++;
  recs.push(rec); els.push(el); nodes.push(node || null); return true;
}
function walk(root) {
  var doc = root.ownerDocument || root;
  var w = doc.createTreeWalker(root, 5, {acceptNode: function (n) { return n.nodeType === 1 && SKIP[n.tagName] ? 2 : 1; }});
  for (var n = w.nextNode(); n; n = w.nextNode()) {
    if (n.nodeType === 3) {
      if (!/\S/.test(n.nodeValue)) continue;
      var p = n.parentElement, own = false;
      if (!p) continue;
      for (var a = p; a && a.nodeType === 1; a = a.parentNode) if (claimed.has(a)) { own = true; break; }
      if (own) continue;                       // the text is an interactive element's name
      var rec = texts.get(p);
      if (!rec) {
        rec = {type: "text", role: p.tagName === "LABEL" ? "label" : "text", label: ""};
        if (!add(rec, p, n)) rec = DROPPED;
        texts.set(p, rec);
      }
      if (rec !== DROPPED && rec.label.length < 200) rec.label = txt(rec.label + " " + n.nodeValue);
      continue;
    }
    if (n.tagName === "IFRAME" || n.tagName === "FRAME") {
      var d = null; try { d = n.contentDocument; } catch (e) {}
      if (d && d.body) walk(d.body); else skipped++;
      continue;
    }
    if (n.matches(SEL)) {
      var role = roleOf(n);
      var type = ROLE_TYPE[role] || (role === "generic" ? "item" : "");
      if (type) {
        var r = {type: type, role: role, label: nameOf(n), el: 1};
        if (type !== "image" || r.label) { add(r, n); claimed.add(n); }   // an unnamed image is decoration
      }
    }
    if (n.shadowRoot) walk(n.shadowRoot);
  }
}
if (document.body) walk(document.body);
var out = [];
for (var i = 0; i < recs.length; i++) {
  var r = recs[i], el = els[i], p = r.p;
  if (!p.visible || !r.label && r.type === "text") { out.push(null); continue; }
  var st = r.el ? statesOf(el) : [];
  var o = {id: i, type: r.type, role: r.role, label: r.label.slice(0, 160),
           interactive: r.type !== "text" && r.type !== "image" && st.indexOf("disabled") < 0,
           onscreen: p.inview && !p.covered, center: p.inview && !p.covered ? p.center : null};
  if (p.inview && !p.covered) o.box = p.box;
  if (p.inview && p.covered) o.covered = true;
  if (r.el) {
    if (st.length) o.states = st;
    var v = valueOf(el); if (v !== undefined) o.value = String(v).slice(0, 200);
    if (el.tagName === "SELECT") o.options = Array.prototype.slice.call(el.options, 0, 30).map(function (x) { return txt(x.text); });
  }
  out.push(o);
}
window.__glovebox = {snap: SNAP, els: els, nodes: nodes.map(function (n, i) { return recs[i].el ? null : n; })};
return {elements: out.filter(Boolean), truncated: truncated, frames_skipped: skipped,
        url: location.href, title: document.title, viewport: [window.innerWidth, window.innerHeight]};
})"""

_LOCATE_JS = r"""(function (SNAP, I, SCROLL) {
""" + _LIB_JS + r"""
var g = window.__glovebox;
if (!g || g.snap !== SNAP) return {stale: "the page was reloaded or navigated since the elements were listed"};
var el = g.els[I];
if (!el || !el.isConnected) return {stale: "the element is no longer in the page"};
var p = place(el, g.nodes[I]), scrolled = false;
if (SCROLL && p.visible && !p.inview) {
  el.scrollIntoView({block: "center", inline: "center", behavior: "instant"});
  scrolled = true; p = place(el, g.nodes[I]);
}
p.scrolled = scrolled; p.states = statesOf(el); p.value = valueOf(el);
p.tag = el.tagName.toLowerCase(); p.editable = !!(el.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName));
return p;
})"""

# focus/set_value run in the page; `press` normally goes through Input.dispatchMouseEvent (a real,
# trusted click that does not move the X pointer) and only falls back to el.click() when covered.
_ACT_JS = r"""(function (SNAP, I, ACTION, VALUE) {
""" + _LIB_JS + r"""
var g = window.__glovebox;
if (!g || g.snap !== SNAP) return {stale: "the page was reloaded or navigated since the elements were listed"};
var el = g.els[I];
if (!el || !el.isConnected) return {stale: "the element is no longer in the page"};
function fire(t) { el.dispatchEvent(new Event(t, {bubbles: true})); }
var tag = el.tagName, ty = (el.type || "").toLowerCase();
if (ACTION === "click") { el.click(); return {done: "dom-click"}; }
if (ACTION === "focus") { el.focus({preventScroll: false}); return {done: "focus", focused: el.ownerDocument.activeElement === el}; }
if (ACTION === "select_all") {       // prepare a text field for Input.insertText
  el.focus();
  if (el.ownerDocument.activeElement !== el) return {error: "the element refused the focus"};
  if (el.select) el.select(); else el.ownerDocument.execCommand("selectAll");
  return {done: "selected"};
}
if (ACTION === "delete") { el.ownerDocument.execCommand("delete"); return {done: "deleted"}; }
if (ACTION === "set") {              // non-text widgets, and the fallback for text
  if (tag === "SELECT") {
    var want = txt(VALUE).toLowerCase(), hit = null;
    for (var i = 0; i < el.options.length; i++) {
      var o = el.options[i];
      if (txt(o.text).toLowerCase() === want || String(o.value).toLowerCase() === want) { hit = o; break; }
    }
    if (!hit) return {error: "no option '" + VALUE + "'", options: Array.prototype.slice.call(el.options, 0, 30).map(function (x) { return txt(x.text); })};
    el.value = hit.value; fire("input"); fire("change");
    return {done: "dom-set"};
  }
  if (ty === "checkbox" || ty === "radio") {
    var on = /^(1|true|on|yes|checked)$/i.test(VALUE);
    if (!on && !/^(0|false|off|no|unchecked)$/i.test(VALUE)) return {error: "a " + ty + " takes value 'true' or 'false'"};
    if (el.checked !== on) el.click();
    return {done: "dom-click", checked: el.checked};
  }
  if (tag === "INPUT" || tag === "TEXTAREA") {
    var d = Object.getOwnPropertyDescriptor(tag === "INPUT" ? HTMLInputElement.prototype : HTMLTextAreaElement.prototype, "value");
    d.set.call(el, VALUE); fire("input"); fire("change");
    return {done: "dom-set"};
  }
  if (el.isContentEditable) { el.focus(); el.ownerDocument.execCommand("selectAll"); el.ownerDocument.execCommand("insertText", false, VALUE); return {done: "dom-set"}; }
  return {error: "this element has no settable value"};
}
return {error: "unknown action"};
})"""


class Stale(RuntimeError):
    """The element id no longer points at a live element (navigation, re-render, closed window)."""


def _eval(cmd, js, *args, wait=None):
    expr = f"{js}({','.join(json.dumps(a) for a in args)})"
    r = cmd("Runtime.evaluate", {"expression": expr, "returnByValue": True}, wait=wait)
    if r.get("exceptionDetails"):
        ex = r["exceptionDetails"]
        raise RuntimeError((ex.get("exception") or {}).get("description") or ex.get("text") or "script error")
    return r.get("result", {}).get("value")


# ---------- browser-dom ----------
def browser_snapshot(cmd, snap: int, wait: float = 8) -> dict:
    """{elements, truncated, frames_skipped, url, title} of the active page; registers the element
    handles in the page under this snapshot id."""
    d = _eval(cmd, _SNAPSHOT_JS, snap, MAX_ELEMENTS, wait=wait)
    if not isinstance(d, dict):
        raise RuntimeError("the page returned no element list")
    return d


def browser_locate(cmd, snap: int, i: int, scroll: bool = True) -> dict:
    """Where element i is NOW: {center, box, inview, covered, by?, scrolled, states, value, tag, editable}.
    Raises Stale when the id no longer points at a live element."""
    p = _eval(cmd, _LOCATE_JS, snap, i, bool(scroll), wait=5)
    if not isinstance(p, dict):
        raise RuntimeError("the page did not answer the element lookup")
    if p.get("stale"):
        raise Stale(p["stale"])
    return p


def browser_act(cmd, snap: int, i: int, action: str, value: str = "") -> dict:
    """Perform `action` on element i without the X pointer or keyboard. Returns {method, …,
    before, after: {states, value}}; raises Stale / RuntimeError(reason)."""
    def js(act, val=""):
        r = _eval(cmd, _ACT_JS, snap, i, act, val, wait=5)
        if not isinstance(r, dict):
            raise RuntimeError("the page did not answer")
        if r.get("stale"):
            raise Stale(r["stale"])
        return r

    before = browser_locate(cmd, snap, i, scroll=action in ("press", "scroll_into_view"))
    out = {"before": {"states": before.get("states") or [], "value": before.get("value")}}
    if before.get("scrolled"):
        out["scrolled"] = True
    if action == "scroll_into_view":
        out["method"] = "dom"
    elif action == "focus":
        out.update(method="dom", focused=bool(js("focus").get("focused")))
    elif action == "press":
        if "disabled" in (before.get("states") or []):
            raise RuntimeError("the element is disabled")
        if before.get("inview") and not before.get("covered"):
            x, y = before["css"]
            for kind in ("mousePressed", "mouseReleased"):
                cmd("Input.dispatchMouseEvent", {"type": kind, "x": x, "y": y, "button": "left", "clickCount": 1})
            out["method"] = "cdp-input"
        else:
            js("click")
            out.update(method="dom-click", synthetic=True,
                       note="the element is " + (f"covered by <{before.get('by', '?')}>" if before.get("covered")
                                                 else "not in view") + ", so its click handler was invoked "
                            "directly (an untrusted event: some pages ignore those)")
    elif action == "set_value":
        r = None
        states = before.get("states") or []
        if "disabled" in states or "readonly" in states:
            raise RuntimeError("the element is " + ("disabled" if "disabled" in states else "read-only"))
        # text fields report a value (a password field hides it); checkboxes, buttons, selects do not type
        text_like = "password" in states or (before.get("value") is not None and before.get("tag") != "select"
                                             and before.get("editable"))
        if text_like:
            sel = js("select_all")
            if sel.get("error"):
                raise RuntimeError(sel["error"])
            if value == "":
                js("delete")
            else:
                cmd("Input.insertText", {"text": value})      # trusted input events, no key events
            out["method"] = "cdp-insert-text"
            got = browser_locate(cmd, snap, i, scroll=False).get("value")
            if got is not None and got != value:
                r = js("set", value)                         # masked/controlled inputs: set + events
                if not r.get("error"):
                    out.update(method="dom-set", synthetic=True)
        else:
            r = js("set", value)
            if r.get("error"):
                e = RuntimeError(r["error"])
                e.options = r.get("options")
                raise e
            out["method"] = r["done"]
    else:
        raise RuntimeError(f"unknown action '{action}'")
    after = browser_locate(cmd, snap, i, scroll=False)
    out["after"] = {"states": after.get("states") or [], "value": after.get("value")}
    out["center"] = after.get("center")
    return out


# ---------- at-spi (native toolkits) ----------
def atspi_call(python: str, env: dict, *args, timeout: float = 12) -> dict:
    """Run a11y_tree.py under the system python on the display's own accessibility bus."""
    try:
        r = subprocess.run([python, "-I", TREE_HELPER, *[str(a) for a in args]], env=env, capture_output=True,
                           text=True, timeout=timeout)
        d = json.loads(r.stdout or "{}")
    except subprocess.TimeoutExpired:
        raise RuntimeError("the accessibility bus did not answer in time (a hung or very large app)") from None
    except ValueError:
        raise RuntimeError("the accessibility helper returned no JSON") from None
    if d.get("stale"):
        raise Stale(d["stale"])
    if not d.get("ok"):
        e = RuntimeError(str(d.get("error") or "the accessibility helper failed")[:200])
        e.no_action = bool(d.get("no_action"))
        raise e
    return d
