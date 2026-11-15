"""验证前端图片加载策略：直连 CDN 优先，失败回退 /api/img。

用本地 static 起页面，桩掉 /api/parse 与图片请求，检查：
1. 直连可用的图 → src 保持直连地址（不经过 /api/img）
2. 直连 404 的图 → 自动回退到 /api/img 并最终显示
3. 两级都失败 → 出现「加载失败 · 点我重试」标签
"""
import base64
import json
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import sync_playwright

STATIC = Path(r"D:\Users\DT\Documents\deepseek-harness\default-workspace\grabber\grabber\static")
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAgAAAAIAQMAAAD+wSzIAAAABlBMVEX///+/v7+jQ3Y5AAAADklEQVQI12P4AIX8EAgALgAD/aNpbtEAAAAASUVORK5CYII="
)

results = []
HITS = {'direct_all_fail': 0, 'proxy_all_fail': 0}


def check(name, cond, extra=""):
    results.append((name, bool(cond), extra))
    print(("PASS " if cond else "FAIL ") + name + (("  " + str(extra)) if extra and not cond else ""))


class Handler(SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


def make_route():
    def route(r):
        u = r.request.url
        if "/api/parse" in u:
            r.fulfill(status=200, content_type="application/json", body=json.dumps({
                "platform": "douyin", "note_id": "123", "type": "image", "title": "t", "author": "a",
                "cover": "", "warnings": [], "source_url": "x",
                "items": [
                    {"kind": "image", "url": "https://cdn.example/x-direct-ok.png", "width": 1440, "height": 2158,
                     "tier": "original", "badge": "高清原图"},
                    {"kind": "image", "url": "https://cdn.example/x-direct-404.png", "width": 1440, "height": 2158,
                     "tier": "original", "badge": "高清原图"},
                    {"kind": "image", "url": "https://cdn.example/x-all-fail.png", "width": 1440, "height": 2158,
                     "tier": "original", "badge": "高清原图"},
                ],
            }).encode())
            return
        if "/api/img" in u and "all-fail" in u:
            HITS["proxy_all_fail"] += 1
        if "/api/img" in u and "direct-404" in u:
            r.fulfill(status=200, content_type="image/png", body=PNG)
            return
        if "/api/img" in u:
            r.fulfill(status=500, content_type="text/plain", body=b"boom")
            return
        if "x-direct-ok.png" in u:
            r.fulfill(status=200, content_type="image/png", body=PNG)
            return
        if "x-all-fail.png" in u:
            HITS["direct_all_fail"] += 1
        if "x-direct-404.png" in u or "x-all-fail.png" in u:
            r.fulfill(status=404, content_type="text/plain", body=b"nope")
            return
        r.continue_()
    return route


def main():
    handler = partial(Handler, directory=str(STATIC))
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = "http://127.0.0.1:%d/" % port

    with sync_playwright() as p:
        b = p.chromium.launch(channel="chrome", headless=True)
        page = b.new_context(viewport={"width": 430, "height": 932}).new_page()
        page.route("**/api/**", make_route())
        page.route("**/x-*.png", make_route())
        page.goto(url, wait_until="domcontentloaded")
        page.fill("#input", "https://v.douyin.com/xxx/")
        page.click("#go")
        page.wait_for_selector(".grid .cell", timeout=30000)
        page.wait_for_timeout(1500)

        info = page.evaluate("""() => {
            const cells=[...document.querySelectorAll('.grid .cell')];
            return cells.map(c=>{
              const im=c.querySelector('img');
              return {src: im.getAttribute('src'), nw: im.naturalWidth,
                      step: im.dataset.step, failed: im.dataset.failed||'',
                      err: !!c.querySelector('.imgerr')};
            });
        }""")
        print(json.dumps(info, ensure_ascii=False, indent=1))

        check("E1 直连可用时走直连（不经 /api/img）", "/api/img" not in (info[0]["src"] or "") and info[0]["nw"] > 0, info[0])
        check("E2 直连 404 时回退到 /api/img 并成功显示",
              "/api/img" in (info[1]["src"] or "") and info[1]["nw"] > 0 and not info[1]["err"], info[1])
        check("E3 两级都失败时显示重试标签", info[2]["err"] and info[2]["failed"] == "1", info[2])

        # 点重试：应重新从直连开始
        before = dict(HITS)
        page.evaluate("document.querySelector('.imgerr').click()")
        page.wait_for_timeout(800)
        after = page.evaluate("""() => {
            const c=document.querySelectorAll('.grid .cell')[2];
            const im=c.querySelector('img');
            return {src: im.getAttribute('src'), step: im.dataset.step};
        }""")
        check("E4 点重试会重新尝试直连",
              HITS["direct_all_fail"] == before["direct_all_fail"] + 1, {"before": before, "after": dict(HITS)})

        # 打开查看器
        page.locator(".grid .cell img").first.click(force=True)
        page.wait_for_timeout(500)
        vsrc = page.evaluate("() => document.querySelector('#vimg').src")
        check("E5 查看器用当前实际地址", bool(vsrc) and "cdn.example" in vsrc, vsrc)
        b.close()

    ok = sum(1 for _, c, _ in results if c)
    print("\n%d/%d passed" % (ok, len(results)))
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())


