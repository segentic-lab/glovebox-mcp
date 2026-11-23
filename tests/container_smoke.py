#!/usr/bin/env python3
"""Smoke test of container mode: a real MCP session through `container.py` into the image.

Not collected by pytest (it needs Docker and a built image); CI runs it after `docker build`:
    GLOVEBOX_CONTAINER=glovebox-ci python tests/container_smoke.py
It starts the container if needed, drives a browser inside it through the accessibility and the
OCR path, checks that a staged file lands in the host's data folder, and closes what it opened.
Exit code 0 = every check passed."""
import asyncio
import json
import os
import sys
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
import container  # noqa: E402

PAGE = ("data:text/html,<title>t</title><h1>Obrazec</h1><label for=n>Ime</label> <input id=n>"
        "<label><input type=checkbox id=c> Strinjam se</label>"
        "<button onclick=\"document.getElementById('o').textContent='saved '+n.value+' '+c.checked\">Shrani</button>"
        "<p id=o></p>")


def payload(res):
    text = res.content[0].text
    return json.loads(text[text.find("{"):])


async def main() -> int:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    checks = []

    def check(name, ok, detail=""):
        checks.append(ok)
        print(("PASS " if ok else "FAIL ") + name + (f" — {detail}" if detail and not ok else ""), flush=True)

    params = StdioServerParameters(command=sys.executable, args=[os.path.join(REPO, "container.py")],
                                   env={**os.environ, "GLOVEBOX_CLEANUP_ON_EXIT": "1"})
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            init = await s.initialize()
            check("handshake reports the version", init.server_info.version == container.__version__, init.server_info.version)
            st = payload(await s.call_tool("status", {}))
            check("status says it runs in the container", bool(st.get("container")), str(st.get("container")))
            check("vision resolves to OCR without a GPU", st["vision"]["effective"] == "basic", st["vision"].get("reason", ""))
            check("AT-SPI bindings are in the image", st["deps"].get("atspi") is True)
            try:
                code = urllib.request.urlopen(f"http://127.0.0.1:{container.PORT}/vnc.html", timeout=10).status
            except Exception as e:
                code = repr(e)
            check("the web viewer answers on the host's loopback", code == 200, str(code))

            p = payload(await s.call_tool("launch_app", {"command": "chromium about:blank", "size": "1100x750"}))
            check("a browser instance launches", p.get("ok") is True, json.dumps(p)[:300])
            n = p.get("instance")
            try:
                for _ in range(40):
                    res = await s.call_tool("browser", {"action": "navigate", "url": PAGE, "instance": n})
                    if not res.is_error:
                        break
                    await asyncio.sleep(0.5)
                check("the browser answers over DevTools", not res.is_error, payload(res).get("error", ""))

                f = payload(await s.call_tool("find_elements", {"query": "ime", "type": "input", "instance": n}))
                check("the page is read through accessibility", f.get("source") == "browser-dom" and f.get("count") == 1, json.dumps(f)[:300])
                res = await s.call_tool("fill", {"text": "Ana Kovač", "element": f["elements"][0]["id"], "instance": n})
                p = payload(res)
                check("fill types real keys and reads the value back", not res.is_error and p.get("verified") is True, json.dumps(p)[:300])
                f = payload(await s.call_tool("find_elements", {"query": "strinjam", "type": "checkbox", "instance": n}))
                p = payload(await s.call_tool("act", {"element": f["elements"][0]["id"], "instance": n}))
                check("act presses a checkbox without the pointer", "checked" in (p.get("after") or {}).get("states", []), json.dumps(p)[:300])
                f = payload(await s.call_tool("find_elements", {"query": "shrani", "instance": n}))
                res = await s.call_tool("click_element", {"id": f["elements"][0]["id"], "instance": n})
                check("click_element hits the button", (payload(res).get("hit") or {}).get("name") == "Shrani", json.dumps(payload(res))[:300])
                p = payload(await s.call_tool("wait_for_text", {"text": "saved Ana Kovač true", "instance": n, "timeout_ms": 8000}))
                check("the page shows the result", p.get("satisfied") is True, json.dumps(p)[:300])

                p = payload(await s.call_tool("find_elements", {"query": "Obrazec", "instance": n, "source": "vision"}))
                check("OCR reads the same screen", p.get("source") == "vision" and p.get("count", 0) >= 1, json.dumps(p)[:300])
                res = await s.call_tool("screenshot", {"instance": n, "scale": 0.5})
                check("a screenshot comes back as an image", len(res.content) == 2 and res.content[1].type == "image")

                p = payload(await s.call_tool("put_file", {"name": "smoke.txt", "content": "čšž", "instance": n}))
                host = os.path.join(container.DATA, "files", str(n), "smoke.txt")
                on_host = os.path.exists(host) and open(host, encoding="utf-8").read() == "čšž"
                check("a staged file lands in the host's data folder", p.get("ok") is True and on_host, host)
            finally:
                res = await s.call_tool("close_instance", {"instance": n})
                check("the instance closes", not res.is_error, json.dumps(payload(res))[:300])
    print(f"{sum(checks)}/{len(checks)} checks passed", flush=True)
    return 0 if all(checks) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
