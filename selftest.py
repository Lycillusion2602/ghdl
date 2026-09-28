#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ghdl 自测：改完代码跑一下，确认没把核心路径改坏。

跑法：双击 ghdl.bat 选 2，或命令行 `py -3 selftest.py`
它用临时目录（_selftest_data）和备用端口 8899 起一个独立实例，不碰你真正的
data/ 配置和历史；测完自己清理。要联网（会真下两个小文件，一共几 KB）。

覆盖的东西：
  语法    server.py 能否 import、app.js 能否过 node --check
  解析    12 种链接形态（release / raw / blob / archive / 仓库页 / 已套代理 / 漏协议 / 非 GitHub / 垃圾）
  校验    在本机常用目录里找一个真实安装包，验"查签名 + 算 SHA256"这条路（找不到就跳过）
  真下载  经真代理下 README.md 和整仓 zip，验 zip 全量 CRC 与 SHA256
  抗断线  造一个"下到 55% 就掐线"的本地假源排在第一位，看它能不能换源后拿到字节完全正确的文件
          （这条能抓出"半截文件被当成下载完成"这类最阴的 bug，别删）
  接口    代理增删改排序停用、配置、目录浏览、release 列表、测速、非法目录
  对齐    前端引用的元素 id、data-act、/api/* 是不是都存在
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

PROJ = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(PROJ, "_selftest_out")
DATA = os.path.join(PROJ, "_selftest_data")
PORT = 8899
MOCK_PORT = 8799
sys.path.insert(0, PROJ)
try:
    sys.stdout.reconfigure(errors="replace")
except (AttributeError, OSError):
    pass
import server as SV  # noqa: E402

FAILS = []


def chk(name, cond, extra=""):
    print(("  [OK] " if cond else "  [NO] ") + name + (("   " + str(extra)) if extra else ""))
    if not cond:
        FAILS.append(name)


def info(name, val):
    print("  ..   %s %s" % (name, val))


def free_port(base):
    """找一个真空出来的端口。测试最怕连上的其实是上一个残留进程跑的旧代码。"""
    for cand in range(base, base + 60):
        s = socket.socket()
        try:
            s.bind(("127.0.0.1", cand))
            return cand
        except OSError:
            pass
        finally:
            s.close()
    raise SystemExit("从 %d 起连续 60 个端口都被占着，先清掉残留的 server.py / 假源进程" % base)


def clean():
    shutil.rmtree(OUT, ignore_errors=True)
    shutil.rmtree(DATA, ignore_errors=True)
    shutil.rmtree(os.path.join(PROJ, "__pycache__"), ignore_errors=True)
    os.makedirs(OUT, exist_ok=True)


# ============================================================ 1 语法
print("=== 1. 语法与可导入性 ===")
chk("server.py 能被 import（说明没有语法错）", True)
node = shutil.which("node")
if node:
    p = subprocess.run([node, "--check", os.path.join(PROJ, "static", "app.js")],
                       capture_output=True, text=True)
    chk("app.js 过 node --check", p.returncode == 0, p.stderr.strip()[-300:])
else:
    info("跳过 JS 语法检查", "PATH 里没 node；app.js 里的括号请自己确认")
html = open(os.path.join(PROJ, "static", "index.html"), encoding="utf-8").read()
js = open(os.path.join(PROJ, "static", "app.js"), encoding="utf-8").read()
css = open(os.path.join(PROJ, "static", "style.css"), encoding="utf-8").read()
chk("三个前端文件都非空", len(html) > 800 and len(js) > 3000 and len(css) > 800)

# ============================================================ 1b 入口与版本一致性
print("=== 1b. 入口与版本一致性 ===")
chk("开发入口收敛成一个 ghdl.bat", os.path.exists(os.path.join(PROJ, "ghdl.bat")))
gone_bats = [n for n in ("start.bat", "自测.bat", "打包.bat") if os.path.exists(os.path.join(PROJ, n))]
chk("旧的三个 .bat 已删（文档说的入口必须真存在）", not gone_bats, gone_bats)
bb = open(os.path.join(PROJ, "ghdl.bat"), "rb").read() if os.path.exists(os.path.join(PROJ, "ghdl.bat")) else b""
chk("ghdl.bat 纯 ASCII + 全 CRLF（cmd 读 UTF-8 中文会乱码）",
    bool(bb) and all(c < 128 for c in bb) and bb.count(b"\n") == bb.count(b"\r\n") and bb.count(b"\r\n") > 10)
chk("菜单五个分支齐", all(k in bb.decode("ascii", "replace") for k in (":run", ":test", ":pack", ":stop", ":ver")))
vf = subprocess.run([sys.executable, os.path.join(PROJ, "server.py"), "--version"],
                    capture_output=True, text=True, encoding="utf-8", errors="replace")
chk("--version 打印的就是 server.py 里的 APP_VERSION",
    vf.returncode == 0 and SV.APP_VERSION in (vf.stdout or ""), (vf.stdout or vf.stderr).strip()[:60])
chk("前端没写死版本号（只从 /api/state 取）",
    re.search(r"\b\d+\.\d+\.\d+\b", js) is None and "r.version" in js)
wf = os.path.join(PROJ, ".github", "workflows", "release.yml")
chk("CI 配置在位", os.path.exists(wf))
if os.path.exists(wf):
    ytxt = open(wf, encoding="utf-8").read()
    chk("CI 先跑自测、并卡 tag 与 APP_VERSION 一致",
        "selftest.py" in ytxt and "APP_VERSION" in ytxt and "tags" in ytxt)

# ============================================================ 2 解析
print("=== 2. 链接解析 ===")
cases = [
    ("https://github.com/Eugeny/tabby/releases/download/v1.0.237/tabby-1.0.237-setup-x64.exe",
     "asset", "tabby-1.0.237-setup-x64.exe"),
    ("https://github.com/git/git/raw/master/README.md", "raw", "README.md"),
    ("https://raw.githubusercontent.com/git/git/master/README.md", "raw", "README.md"),
    ("https://github.com/git/git/blob/master/t/README", "raw", "README"),
    ("https://github.com/git/git/archive/refs/heads/master.zip", "archive", "master.zip"),
    ("https://github.com/octocat/Hello-World", "repo_zip", "Hello-World-HEAD.zip"),
    ("https://github.com/octocat/Hello-World/releases", "release_page", None),
    ("https://gh-proxy.com/https://github.com/git/git/raw/master/README.md", "raw", "README.md"),
    ("https://ghfast.top/https://gh-proxy.com/https://github.com/git/git/raw/master/README.md",
     "raw", "README.md"),
    ("github.com/git/git/raw/master/README.md", "raw", "README.md"),
    ("https://gitlab.com/x/y/-/raw/main/a.txt", "external", "a.txt"),
    ("不是链接", None, None),
]
for url, want_kind, want_fn in cases:
    p = SV.parse_url(url)
    if want_kind is None:
        chk("拒绝非链接", not p.get("ok"), p.get("error"))
    else:
        got = (p.get("kind"), p.get("filename"))
        chk("解析 %s" % url[:70], got == (want_kind, want_fn), got)
p = SV.parse_url("https://github.com/octocat/Hello-World", "clone")
chk("仓库页 + 强制 clone", p.get("kind") == "clone" and p["url"].endswith(".git"), p.get("url"))
chk("代理前缀自动补协议和斜杠", SV.norm_prefix("my.prox") == "https://my.prox/")
chk("文件名去非法字符", SV.safe_filename('a<b>:c/d\\e|?*.txt') == "e_")
chk("文件名保留中文和扩展名", SV.safe_filename("dir/sub/名字.txt") == "名字.txt")
chk("超长文件名被截断", len(SV.safe_filename("x" * 400 + ".zip")) < 160)
chk("字节数格式化", SV.fmt_bytes(169734624) == "162 MB", SV.fmt_bytes(169734624))
chk("列得出盘符", bool(SV.list_dirs("").get("items")))

def find_exe_sample():
    """
    找一个本机真实存在的安装包来验"查签名 + 算哈希"这条路。
    不写死目录（仓库里不该露个人路径），而是从本地那份不公开的 data/config.json 的
    quick_dirs / default_dir 里翻，也可以用环境变量 GHDL_TEST_EXE 指定一个文件。
    """
    if os.environ.get("GHDL_TEST_EXE"):
        return os.environ["GHDL_TEST_EXE"]
    cfgf = os.path.join(PROJ, "data", "config.json")
    roots = []
    try:
        c = json.load(open(cfgf, encoding="utf-8"))
        roots = list(c.get("quick_dirs") or [])
        if c.get("default_dir"):
            roots.append(c["default_dir"])
    except (OSError, ValueError):
        pass
    roots.append(os.path.join(os.path.expanduser("~"), "Downloads"))
    for d in roots:
        if not d or not os.path.isdir(d):
            continue
        try:
            big = [os.path.join(d, n) for n in os.listdir(d)
                   if n.lower().endswith((".exe", ".msi"))
                   and os.path.getsize(os.path.join(d, n)) > 20 * 1024 * 1024]
        except OSError:
            continue
        if big:
            # 取最小的那只：验的是"这条路走不走得通"，不是哈希大文件的速度
            return sorted(big, key=lambda p: os.path.getsize(p))[0]
    return ""


T = find_exe_sample()
if T and os.path.exists(T):
    a = SV.authenticode(T)
    chk("查签名：本机安装包能读出签名状态", a.get("sig") in ("ok", "bad") and bool(a.get("status")),
        "%s → %s（%s）" % (os.path.basename(T), a.get("status"), a.get("signer") or "无签发者"))
    sha = SV.sha256_of(T)
    chk("SHA256 算得出且格式正确", len(sha) == 64 and all(c in "0123456789abcdef" for c in sha), sha[:16])
    if os.path.basename(T).lower() == "tabby-1.0.237-setup-x64.exe":
        # 这台机器上实测过的那只：拿 certutil 的结论当交叉校验
        chk("与 certutil 的结果一致", sha == "38b63b09c082d1c13db82bb95e8f5b0a80311cd04a57a673c3db7b3c20ccd3b7")
else:
    info("跳过签名测试", "没在本机的常用目录里找到 >20MB 的安装包；设 GHDL_TEST_EXE 可强制指定")

# ============================================================ 3 造一个会断线的假源
print("=== 3. 起假源 + 起服务 ===")
CACHE, STOP = {}, threading.Event()


def mock_dying_proxy(port):
    """接受任意请求，转发给 gh-proxy，但只把前 55% 发出去就关连接 —— 模拟源半路挂。"""
    def run():
        s = socket.socket()
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", port))
        s.listen(8)
        s.settimeout(1.0)
        while not STOP.is_set():
            try:
                c, _ = s.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            try:
                data = c.recv(8192).decode("latin1")
                m = re.match(r"GET (\S+) HTTP", data)
                if not m:
                    c.close()
                    continue
                target = m.group(1)[1:]
                body = CACHE.get(target)
                if body is None:
                    # 必须用 curl 取上游：gh-proxy 见到 Python 默认 UA 直接 403
                    try:
                        q = subprocess.run(["curl.exe", "-sS", "-L", "--fail", "--max-time", "40",
                                            "https://gh-proxy.com/" + target],
                                           capture_output=True, timeout=50)
                    except subprocess.TimeoutExpired:
                        c.close()
                        continue
                    if q.returncode != 0:
                        c.close()
                        continue
                    body = q.stdout
                    CACHE[target] = body
                cut = max(1, int(len(body) * 0.55))
                head = ("HTTP/1.1 200 OK\r\nContent-Length: %d\r\nConnection: close\r\n\r\n" % len(body))
                c.sendall(head.encode() + body[:cut])   # 声称全长，只发一截
            except OSError:
                pass
            finally:
                try:
                    c.close()
                except OSError:
                    pass
        s.close()
    threading.Thread(target=run, daemon=True).start()


def drip_server(port):
    """挤牙膏源：声称有 900MB，然后每 0.5 秒只吐 1 个字节，永远不结束。
    专门用来验"最近 15 秒增长量"这条判据 —— 只盯'有没有增长'的话会被它骗过去。"""
    def run():
        s = socket.socket()
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", port))
        s.listen(4)
        s.settimeout(1.0)
        while not STOP.is_set():
            try:
                c, _ = s.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(target=_drip_serve, args=(c,), daemon=True).start()
        s.close()

    def _drip_serve(c):
        try:
            c.recv(4096)
            c.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 900000000\r\n\r\n")
            for _ in range(2000):
                if STOP.is_set():
                    break
                c.sendall(b"x")
                time.sleep(0.5)
        except OSError:
            pass
        finally:
            try:
                c.close()
            except OSError:
                pass

    threading.Thread(target=run, daemon=True).start()


MOCK_PORT = free_port(MOCK_PORT)
DRIP_PORT = free_port(8797)
mock_dying_proxy(MOCK_PORT)
drip_server(DRIP_PORT)
time.sleep(0.4)
REF_URL = "https://github.com/git/git/raw/master/README.md"


def fetch_ref():
    """参考文件多源重试。取不到就如实说"联网部分跳过"，绝不能把校园网抖动报成产品的错。"""
    order = ["https://gh-proxy.com/", "https://ghfast.top/", "https://ghproxy.net/", ""]
    for attempt in (1, 2):
        for px in order:
            q = subprocess.run(["curl.exe", "-sS", "-L", "--fail", "--max-time", "40",
                                px + REF_URL], capture_output=True)
            if q.returncode == 0 and len(q.stdout) > 1000:
                return q.stdout, (px or "直连"), attempt
        time.sleep(2)
    return b"", None, 0


ref, ref_via, ref_try = fetch_ref()
chk("参考文件拿到了（联网可用）", bool(ref),
    "%d 字节，走 %s，第 %d 轮" % (len(ref), ref_via, ref_try) if ref else "三个源加直连都取不到")
if not ref:
    STOP.set()
    print("\n校园网这会儿取不到参考文件，联网部分（真下载 / 断线换源 / 假源识别）测了也不可信；")
    print("本轮只算前面离线检查的结论，等网络缓过来再跑一次。")
    sys.exit(1 if FAILS else 3)
REF_SHA = hashlib.sha256(ref).hexdigest()

clean()
# 闸门：端口上如果已经有残留的旧代码进程在跑，自测就是在测那个陌生人，结论全不可信
PORT = free_port(PORT)
env = dict(os.environ, GHDL_DATA=DATA, GHDL_PORT=str(PORT))
# 千万不能用 stdout=PIPE 又不读：管道缓冲（几十 KB）写满后，服务里的 print 会永久阻塞，
# 表现成"任务卡住不动"，看着像产品的 bug，其实是夹具把自己 deadlock 了。落到文件里才安全。
console = open(os.path.join(OUT, "server-console.log"), "ab")
srv = subprocess.Popen([sys.executable, os.path.join(PROJ, "server.py"), "--no-browser"],
                       cwd=PROJ, stdout=console, stderr=subprocess.STDOUT, env=env,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def req(path, body=None, timeout=60):
    data = None if body is None else json.dumps(body).encode("utf-8")
    r = urllib.request.Request("http://127.0.0.1:%d%s" % (PORT, path), data=data,
                               headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=timeout) as f:
        return json.loads(f.read().decode("utf-8"))


def wait_task(tid, tries=100):
    st = None
    for _ in range(tries):
        time.sleep(0.5)
        st = req("/api/task?id=%s" % tid)["task"]
        if st["state"] in ("done", "failed", "canceled"):
            return st
    return st


up = False
for _ in range(60):
    try:
        up = req("/api/state").get("ok")
        break
    except (urllib.error.URLError, OSError):
        time.sleep(0.3)
chk("服务在备用端口起来了", up)
base = req("/api/state")
chk("state 字段齐", {"config", "proxies", "tasks", "history", "version"} <= set(base), sorted(base))
chk("接口报的版本号 = 代码里的 APP_VERSION", base.get("version") == SV.APP_VERSION, base.get("version"))
chk("默认 4 条源（3 个实测站 + 直连兜底）", len(base["proxies"]) == 4,
    [p["name"] for p in base["proxies"]])
chk("首页 HTML 出得来", "<title>ghdl" in urllib.request.urlopen(
    "http://127.0.0.1:%d/" % PORT, timeout=10).read().decode("utf-8"))
chk("样式出得来", "bar.done" in urllib.request.urlopen(
    "http://127.0.0.1:%d/static/style.css" % PORT, timeout=10).read().decode("utf-8"))

# 把假源排到第一位（last_ms 最小 → 排队最前）
pxf = os.path.join(DATA, "proxies.json")
doc = json.load(open(pxf, encoding="utf-8"))
doc["proxies"].insert(0, {"id": "mockA", "name": "假源(半路断)", "prefix": "http://127.0.0.1:%d/" % MOCK_PORT,
                          "enabled": True, "order": 0, "last_ms": 1, "last_ok": True,
                          "tried": 3, "ok": 3, "last_at": None})
json.dump(doc, open(pxf, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

print("=== 4. 抗断线：假源掐线后换真源，文件必须一个字节都不差 ===")
final = wait_task(req("/api/download", {"url": REF_URL, "destdir": OUT, "mode": "file"})["task"]["id"])
chk("任务最终完成", final and final["state"] == "done", final and final["state"])
if final:
    got = final.get("dest") or ""
    chk("落盘文件名与界面显示一致", os.path.basename(got) == final.get("filename"),
        "%s / %s" % (final.get("filename"), os.path.basename(got)))
    sha = hashlib.sha256(open(got, "rb").read()).hexdigest() if os.path.exists(got) else ""
    chk("断线换源后文件字节完全正确", sha == REF_SHA, (sha or "-")[:12] + " 期望 " + REF_SHA[:12])
    chk("确实换了源才成功", len(final.get("attempts") or []) >= 2,
        [(a.get("proxy"), a.get("rc")) for a in final.get("attempts") or []])
    if os.environ.get("VERBOSE"):
        for line in final.get("log") or []:
            print("        " + line)
chk("失败的源被记了账（下次自动排后面）",
    [p for p in json.load(open(pxf, encoding="utf-8"))["proxies"] if p["id"] == "mockA"][0]["last_ok"] is False)

print("=== 5. 真下载 + 校验 ===")
zf = wait_task(req("/api/download", {"url": "https://github.com/octocat/Hello-World/archive/refs/heads/master.zip",
                                     "destdir": OUT, "mode": "auto"})["task"]["id"])
chk("整仓 zip 下完", zf and zf["state"] == "done", zf and zf["state"])
if zf and zf.get("verify"):
    chk("zip 走了全量 CRC 校验", zf["verify"].get("archive") == "ok", zf["verify"].get("archive_detail"))
    # 这两个函数都返回过同名 detail 键，签名那句会把压缩包那句盖掉（实测踩过）
    chk("压缩包结论没被签名的结论盖掉",
        "CRC" in str(zf["verify"].get("archive_detail") or ""),
        "%s | %s" % (zf["verify"].get("archive_detail"), zf["verify"].get("sig_detail")))
    chk("SHA256 有值", bool(zf["verify"].get("sha256")), zf["verify"]["sha256"][:12])
    print("      校验汇总：" + SV.summarize_verify(zf["verify"]))
big = req("/api/download", {"url": "https://github.com/Eugeny/tabby/releases/download/v1.0.237/"
                                   "tabby-1.0.237-setup-arm64.exe", "destdir": OUT, "mode": "file"})
bf = wait_task(big["task"]["id"], tries=200)
chk("release 大文件（arm64 安装包 ~160MB）也能下完", bf and bf["state"] == "done",
    "%s / %s" % (bf and bf["state"], SV.fmt_bytes((bf or {}).get("downloaded"))))
if bf and bf.get("verify"):
    print("      大文件校验汇总：" + SV.summarize_verify(bf["verify"]))
    os.remove(bf["dest"]) if os.path.exists(bf["dest"]) else None
    info("大文件测速", "%s/s" % SV.fmt_bytes(bf.get("speed")))
bad = req("/api/download", {"url": REF_URL, "destdir": "\x00坏路径", "mode": "file"})
chk("非法目录被挡下并说清原因", bad.get("ok") is False, bad.get("error"))
print("=== 5b. 挤牙膏源：必须在十几秒内被识别并弃用 ===")
req("/api/proxies", {"action": "add", "prefix": "http://127.0.0.1:%d/" % DRIP_PORT, "name": "挤牙膏源"})
# 排队看的是"最近测速耗时"，光排序号不管用 —— 直接把它的 last_ms 写成 0，让它排第一个被试
doc = json.load(open(pxf, encoding="utf-8"))
for p in doc["proxies"]:
    if p["name"] == "挤牙膏源":
        p["last_ms"], p["last_ok"] = 0, True
json.dump(doc, open(pxf, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
dpx = [p for p in req("/api/state")["proxies"] if p["name"] == "挤牙膏源"][0]
t0 = time.time()
df = wait_task(req("/api/download", {"url": REF_URL, "destdir": OUT, "mode": "file"})["task"]["id"], tries=160)
spent = time.time() - t0
first = ((df or {}).get("attempts") or [{}])[0]
chk("第一个被试的就是挤牙膏源", first.get("proxy") == "挤牙膏源", first.get("proxy"))
chk("它被中途放弃（退出码记成 stalled 而不是让它拖完）", first.get("rc") == "stalled", first.get("rc"))
chk("弃用后换真源仍然拿到完整文件",
    bool(df) and df["state"] == "done" and df.get("dest") and os.path.exists(df["dest"])
    and hashlib.sha256(open(df["dest"], "rb").read()).hexdigest() == REF_SHA,
    df and df["state"])
chk("整个过程没被拖过 90 秒", spent < 90, "%.1f 秒" % spent)
req("/api/proxies", {"action": "del", "id": dpx["id"]})

# 这条测的是"链接指向不存在的文件"，跟哪个源通不通无关：把假源和直连都停用，
# 免得它们今天抽风把计时搞得不确定（直连 raw.githubusercontent.com 在这台机器上会卡在 TLS 握手）。
for pid in ("mockA", "direct"):
    if any(p["id"] == pid for p in req("/api/state")["proxies"]):
        req("/api/proxies", {"action": "enable", "id": pid, "enabled": False})
nof = req("/api/download", {"url": "https://github.com/git/git/raw/master/这个文件不存在.md",
                            "destdir": OUT, "mode": "file"})
nf = wait_task(nof["task"]["id"], tries=120)
if nf and nf["state"] not in ("done", "failed", "canceled"):
    # 有源正把请求拖住（这台机器上直连和 ghproxy.net 都会这样）。这不算产品的错，
    # 正好用它验一次"取消"真的能停下来。
    print("      （第 3 次尝试还挂着，改用取消收口 —— 顺带测取消）")
    req("/api/cancel", {"id": nof["task"]["id"]})
    nf = wait_task(nof["task"]["id"], tries=40)
chk("文件不存在时不产生假成功（如实失败，或至少能被取消）",
    nf and nf["state"] in ("failed", "canceled") and not os.path.exists((nf or {}).get("dest") or "@"),
    "%s / 尝试 %s" % (nf and nf["state"], [(a.get("proxy", "")[:12], a.get("rc")) for a in (nf or {}).get("attempts") or []]))
for pid in ("mockA", "direct"):
    req("/api/proxies", {"action": "enable", "id": pid, "enabled": True})
chk("失败也会写进历史（带失败标记）",
    any((not h.get("ok")) for h in req("/api/state")["history"]))
left = [n for n in os.listdir(OUT)
        if n.endswith(".hdr") or (n.endswith(".part") and os.path.getsize(os.path.join(OUT, n)) == 0)]
chk("失败的任务不在你目录里留临时文件", not left, left)

print("=== 5c. 校验本地已有文件（不下载）===")
样本 = os.path.join(OUT, "README.md")
if os.path.exists(样本):
    before = len(req("/api/state")["history"])
    vf = wait_task(req("/api/verify", {"path": 样本, "url": ""})["task"]["id"])
    vv = (vf or {}).get("verify") or {}
    chk("只读本地文件就能算出 SHA256，且和参考一致",
        vf and vf["state"] == "done" and vv.get("sha256") == REF_SHA, vv.get("sha256", "")[:12])
    chk("校验任务不往下载历史里塞东西", len(req("/api/state")["history"]) == before, before)
    chk("校验任务标的是本地校验而不是下载",
        (vf or {}).get("mode") == "verify" and (vf or {}).get("proxy") in (None, "本地"),
        "%s/%s" % ((vf or {}).get("mode"), (vf or {}).get("proxy")))
else:
    chk("本地校验样本存在", False, "没找到 " + 样本)
vbad = req("/api/verify", {"path": os.path.join(OUT, "根本不存在的东西.bin")})
chk("校验不存在的文件时给出明确错误", vbad.get("ok") is False, vbad.get("error"))
vempty = req("/api/verify", {"path": "   "})
chk("空路径不会被解析成服务自己的目录", vempty.get("ok") is False and "空" in str(vempty.get("error")),
    vempty.get("error"))
vopen = req("/api/open?path=")
chk("open 不给路径时也只报错不打开项目目录", vopen.get("ok") is False, vopen.get("error"))
lsf = req("/api/ls?path=" + urllib.parse.quote(OUT) + "&files=1")
chk("文件浏览器能列出文件带大小", bool(lsf.get("files")) and "size" in lsf["files"][0],
    [(f["name"], f["size"]) for f in lsf.get("files", [])][:3])

print("=== 5d. 主题 ===")
page = urllib.request.urlopen("http://127.0.0.1:%d/" % PORT, timeout=10).read().decode("utf-8")
chk("右上角没有主题切换器了（他要求删掉）", 'data-act="theme"' not in page)
chk("主题锁死可爱", "dataset.theme = 'cute'" in page)
css_body = open(os.path.join(PROJ, "static", "style.css"), encoding="utf-8").read()
need = ["--bg", "--card", "--fg", "--acc", "--ok", "--bad", "--radius", "--btn-radius", "--bar-bg"]
blocks = re.findall(r':root\[data-theme="([a-z]+)"\]\s*\{(.*?)\}', css_body, re.S)
chk("每套主题都定义了同一批关键变量",
    len(blocks) >= 4 and all(all(v in body for v in need) for _n, body in blocks),
    [n for n, _ in blocks])
shared = css_body.split("* { box-sizing", 1)[-1]
hard = [l.strip()[:60] for l in shared.splitlines() if re.search(r"#[0-9a-fA-F]{3,8}\b", l)]
chk("共用样式区没有写死颜色（主题切换器删了，这条不再留例外）", not hard, hard[:3])

print("=== 5e. 动效层（本地 animate.css，不引 CDN）===")
adir = os.path.join(PROJ, "static")
acss = open(os.path.join(adir, "animate.css"), encoding="utf-8").read()
lic = os.path.join(adir, "ANIMATE-LICENSE.txt")
chk("animate.css 是本地文件且摘进了 8 个关键帧",
    acss.count("@keyframes") >= 10, acss.count("@keyframes"))
chk("文件头写明了出处和许可证", "animate.css v4.1.1" in acss and "Hippocratic" in acss)
chk("许可证全文随附（v4 是 Hippocratic 2.1，不是 MIT）",
    os.path.exists(lic) and "Hippocratic" in open(lic, encoding="utf-8").read()[:600])
chk("每个用到的动画类都有对应 keyframes",
    all(("@keyframes " + n) in acss for n in
        ["fadeIn", "fadeInDown", "fadeInUp", "zoomIn", "pulse", "tada", "headShake", "heartBeat"]))
chk("--animate-duration 定义了（基类要用，缺了就退回默认时长）", "--animate-duration:" in acss)
# 动画名打错不会报错，只会"静悄悄地不动"，所以静态比对引用名和定义名
defined = set(re.findall(r"@keyframes\s+([A-Za-z0-9_-]+)", css + "\n" + acss))
refd = set(re.findall(r"animation-name:\s*([A-Za-z0-9_-]+)", css + "\n" + acss))
refd |= set(re.findall(r"animation:\s*([A-Za-z0-9_-]+)", css + "\n" + acss))
refd.discard("none")
chk("每个被引用的动画名都有对应 keyframes（打错会静默不动）",
    not (refd - defined), "缺定义: " + str(sorted(refd - defined)))
chk("尊重系统「减少动效」设置", "prefers-reduced-motion" in css)
# 最关键的一条：这个工具是给"GitHub 打不开"时用的，页面绝不能依赖外部资源
ext = re.findall(r'(?:src|href)\s*=\s*"(https?://[^"]+|//[^"]+)"', html)
ext += re.findall(r'@import\s+(?:url\()?\s*["\']?(https?://[^\)"\']+)', css)
ext += re.findall(r'url\(\s*["\']?(https?://[^\)"\']+)', css)
chk("前端不引用任何外部资源（断网也能完整显示）", not ext, ext[:4])
static_ok = True
for f in ("animate.css", "style.css", "app.js", "ANIMATE-LICENSE.txt"):
    try:
        urllib.request.urlopen("http://127.0.0.1:%d/static/%s" % (PORT, f), timeout=10).read()
    except Exception:
        static_ok = False
chk("四个静态资源服务都取得到", static_ok)
chk("单次动画是渲染后临时挂类（不是写进模板，否则每次轮询重播）",
    "applyFx()" in js and "pendingFx" in js and "animationend" in js
    and 'class="task ' not in js and 'fx-done"></div>' not in js)
chk("入场动画不用带位移的 fadeInUp（那会把内容推到下面，打开就不在顶上）",
    re.search(r"\.fx-in\s*\{[^}]*fadeInUp", css_body) is None
    and re.search(r"\.fx-in\s*\{[^}]*animation-name:\s*fadeIn", css_body) is not None)
chk("加载时强制回到页面顶部", "window.scrollTo(0, 0)" in js)

print("=== 6. 代理与配置接口 ===")
a = req("/api/proxies", {"action": "add", "prefix": "my.new-proxy.dev", "name": "新加的"})
chk("添加代理", any(p["prefix"] == "https://my.new-proxy.dev/" for p in a["proxies"]))
nid = [p["id"] for p in a["proxies"] if p["prefix"] == "https://my.new-proxy.dev/"][0]
chk("重复前缀被拒", req("/api/proxies", {"action": "add", "prefix": "https://my.new-proxy.dev/"}).get("ok") is False)
b = req("/api/proxies", {"action": "move", "id": nid, "delta": -99})
chk("上移能挪到首位", [p["id"] for p in b["proxies"]].index(nid) == 0)
chk("下移按 delta", [p["id"] for p in req("/api/proxies", {"action": "move", "id": nid, "delta": 2})["proxies"]].index(nid) == 2)
chk("停用", [p for p in req("/api/proxies", {"action": "enable", "id": nid, "enabled": False})["proxies"] if p["id"] == nid][0]["enabled"] is False)
chk("删除", all(p["id"] != nid for p in req("/api/proxies", {"action": "del", "id": nid})["proxies"]))
chk("常用目录写进配置",
    len(req("/api/config", {"quick_dirs": [OUT, os.path.expanduser("~")]})["config"]["quick_dirs"]) >= 2)
chk("历史里留下了成功记录（含 sha256）", len([h for h in req("/api/state")["history"] if h.get("ok")]) >= 2)
chk("目录浏览能进能退", bool(req("/api/ls")["items"]) and bool(req("/api/ls?path=" + urllib.parse.quote(OUT))["parent"]))
chk("open 不存在的路径只报错不弹窗", req("/api/open?path=" + urllib.parse.quote(os.path.join(OUT, "无.zip"))).get("ok") is False)
try:
    ass = req("/api/assets?url=https://github.com/Eugeny/tabby/releases", timeout=45)
    chk("release 列表接口结构完整", "ok" in ass,
        ("%d 个文件，走 %s" % (len(ass.get("assets", [])), ass.get("via"))) if ass.get("ok") else ass.get("error"))
except Exception as e:
    chk("release 列表接口结构完整", False, repr(e))
pr = req("/api/probe", timeout=70)
chk("全部测速逐个返回", len(pr["results"]) >= 2,
    [(x["name"], "通" if x.get("ok") else "不通", str(x.get("ms")) + "ms") for x in pr["results"]])
live = [x for x in pr["results"] if x.get("ok")]
chk("至少一个真源是通的（否则这台机器现在没法下 GitHub）", any("假源" not in x["name"] for x in live))

print("=== 7. 前后端字段对齐 ===")
ids = set(re.findall(r'id="([^"]+)"', html))
used = set(re.findall(r"\$\('#([^']+)'\)", js))
chk("JS 选的元素 HTML 里都有", not (used - ids), sorted(used - ids) or "全部命中")
apis = {a.split("/api/")[1] for a in re.findall(r"api\('(/api/[a-z]+)", js)}
backend = set(re.findall(r'path == "/api/([a-z]+)"', open(os.path.join(PROJ, "server.py"), encoding="utf-8").read()))
chk("前端调的接口后端都实现了", not (apis - backend), apis - backend or sorted(apis))
acts = set(re.findall(r"act === '([^']+)'", js))
provided = set(re.findall(r'data-act="([a-z-]+)"', html)) | set(re.findall(r'data-act=\\?"([a-z-]+)', js)) | {"px-en"}
chk("JS 处理的按钮都有出处", not (acts - provided), sorted(acts - provided) or "全部有出处")

print("=== 8. 停止服务（打包成 exe 后这是唯一出口）===")
# 用一个"每 0.5 秒只吐 1 字节"的源把任务钉住在下载中，再看后端拦不拦"不带 force 的停止"。
# 之前这里靠下个小文件再 sleep 0.8 秒来制造"在跑"，可那文件不到 1 秒就完事了 —— 时好时坏的假失败。
req("/api/proxies", {"action": "add", "prefix": "http://127.0.0.1:%d/" % DRIP_PORT, "name": "挤牙膏源(占位用)"})
doc2 = json.load(open(os.path.join(DATA, "proxies.json"), encoding="utf-8"))
for px2 in doc2["proxies"]:
    if px2["name"].startswith("挤牙膏源"):
        px2["last_ms"], px2["last_ok"] = 0, True
json.dump(doc2, open(os.path.join(DATA, "proxies.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=2)
sid = req("/api/download", {"url": REF_URL, "destdir": OUT, "mode": "file"})["task"]["id"]
busy_seen = False
for _ in range(20):                    # 等到它确实进入 downloading 再按停止
    time.sleep(0.4)
    s = req("/api/task?id=" + sid)["task"]
    if s["state"] == "downloading":
        busy_seen = True
        break
chk("任务确实被钉在 downloading（这条不成立就说明夹具又不可信了）", busy_seen)
r1 = req("/api/shutdown", {})
chk("有任务在跑时，不带 force 不让停", r1.get("ok") is False and (r1.get("busy") or 0) >= 1,
    r1.get("error"))
r2 = req("/api/shutdown", {"force": True})
chk("force 停止被接受", r2.get("ok") is True, r2)
gone = False
for _ in range(24):
    time.sleep(0.5)
    if srv.poll() is not None:
        gone = True
        break
    try:
        req("/api/state", timeout=2)
    except Exception:
        gone = True
        break
chk("服务真的退出了（不是只回了个 OK）", gone)
if srv.poll() is None:
    srv.terminate()
console.close()
STOP.set()
clean()

print("=== 9. 打包出来的 exe（存在就顺手验一遍）===")
exe = os.path.join(PROJ, "ghdl.exe")
if os.path.exists(exe):
    eport = free_port(8795)
    edata = os.path.join(PROJ, "_selftest_exe_data")
    shutil.rmtree(edata, ignore_errors=True)
    # stdout 一律 DEVNULL：上次我就是用 PIPE 又不读，把服务堵死，误判成产品卡住
    ep = subprocess.Popen([exe, "--no-browser"], cwd=PROJ,
                          env=dict(os.environ, GHDL_PORT=str(eport), GHDL_DATA=edata),
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    def ereq(p, b=None, timeout=60):
        d = None if b is None else json.dumps(b).encode("utf-8")
        r = urllib.request.Request("http://127.0.0.1:%d%s" % (eport, p), data=d,
                                   headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(r, timeout=timeout) as f:
            return json.loads(f.read().decode("utf-8"))

    eup = False
    for _ in range(60):
        try:
            ereq("/api/state", timeout=3)
            eup = True
            break
        except Exception:
            if ep.poll() is not None:
                break
            time.sleep(0.5)
    chk("exe 起得来（这台机器上没有 Python 也能跑）", eup, "进程还在=%s" % (ep.poll() is None))
    if eup:
        page_e = urllib.request.urlopen("http://127.0.0.1:%d/" % eport, timeout=15).read().decode("utf-8")
        chk("页面和样式都打进包里了（不依赖旁边的 static）", "<title>ghdl" in page_e, len(page_e))
        os.makedirs(OUT, exist_ok=True)

        def ewait(tid, tries=120):
            # 不能用上面那个 wait_task —— 它绑的是源码服务的 req()，而源码服务在第 8 节已经被停掉了
            s = None
            for _ in range(tries):
                time.sleep(0.5)
                s = ereq("/api/task?id=" + tid)["task"]
                if s["state"] in ("done", "failed", "canceled"):
                    return s
            return s
        et = ewait(ereq("/api/download", {"url": REF_URL, "destdir": OUT, "mode": "file"})["task"]["id"])
        got = (et or {}).get("dest") or ""
        chk("exe 能真下完文件且字节正确",
            bool(et) and et["state"] == "done" and os.path.exists(got)
            and hashlib.sha256(open(got, "rb").read()).hexdigest() == REF_SHA,
            "%s / %s" % ((et or {}).get("state"), (et or {}).get("filename")))
        chk("exe 的数据落在它自己旁边（这里被 GHDL_DATA 指到临时目录，没碰你真用的 data/）",
            os.path.isdir(edata) and os.path.exists(os.path.join(edata, "config.json")),
            os.listdir(edata) if os.path.isdir(edata) else "无")
        chk("界面那个「停止服务」能把 exe 关掉", ereq("/api/shutdown", {"force": True}).get("ok") is True)
        died = False
        for _ in range(30):
            time.sleep(0.5)
            if ep.poll() is not None:
                died = True
                break
        chk("exe 进程真的退出了（没窗口，只能靠这个出口）", died, "退出码 %s" % ep.returncode)
    if ep.poll() is None:
        ep.kill()
    shutil.rmtree(edata, ignore_errors=True)
else:
    info("跳过 exe 测试", "还没打包 —— 双击 ghdl.bat 选 3 生成 ghdl.exe 后再跑一次自测")
shutil.rmtree(OUT, ignore_errors=True)

print("\n=== 结果 ===")
print("全部通过，可以用了（ghdl.bat 选 1 启动）" if not FAILS else "有 %d 项没过：" % len(FAILS))
for f in FAILS:
    print("  x " + f)
sys.exit(1 if FAILS else 0)
