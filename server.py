#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ghdl —— GitHub 代理下载工具（本地网页版）

启动：双击 ghdl.bat 选 1（或命令行 `py -3 server.py`）→ 自动打开 http://127.0.0.1:8765
只监听 127.0.0.1，不对外开放；不写注册表、不需要管理员权限、除 Python 标准库外无依赖。

文件分工（按下面的编号顺序读就是执行顺序）：
  server.py           本文件：链接解析 / 代理选源 / 下载执行 / 校验 / HTTP 接口
  static/             前端页面（手写 HTML+CSS+JS，不引框架、不引 CDN）
  data/proxies.json   代理列表，界面上「添加代理」就是往这里写
  data/config.json    默认下载目录、常用目录、端口
  data/history.json   下载历史（大小、速度、SHA256、校验结论）
  data/server.log     运行日志，出问题先看它
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
import tempfile
import threading
import time
import traceback
import uuid
import webbrowser
import zipfile
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

try:
    import converters as CONV
    CONV_ERR = None
except Exception as _e:                      # 转换依赖缺了也不该让下载器起不来
    CONV = None
    CONV_ERR = "%s: %s" % (type(_e).__name__, _e)

# 控制台是 GBK，遇到编码不了的字符（箭头之类）用 ? 顶上，别让 print 抛异常把服务搞崩。
# 注意：打包成 --windowed 的 exe 后 sys.stdout / sys.stderr 直接是 None，所以这里要判空。
for _s in (sys.stdout, sys.stderr):
    try:
        if _s is not None:
            _s.reconfigure(errors="replace")
    except (AttributeError, OSError, ValueError):
        pass


def say(msg):
    """往控制台说一句话；没有控制台（窗口化 exe）就安静地跳过。"""
    try:
        if sys.stdout is not None:
            print(msg, flush=True)
    except Exception:
        pass

FROZEN = bool(getattr(sys, "frozen", False))          # PyInstaller 打包后为 True
# 版本号的唯一出处。发版流程：改这一行 → commit → 打同名 tag（去掉 v）→ CI 会比对，
# 不一致就直接失败不出包，避免"tag 说 1.2.2、界面里写 1.2.1"这种事。
APP_VERSION = "1.2.2"
# 打包后 __file__ 指向临时解压目录（_MEIxxxxxx，每次运行都换、退出就没了）。
# 若还用它的目录当 BASE，data/ 会被写进临时目录里 —— 配置和历史每次都丢。
# 所以：可写数据一律放 exe 旁边；只读的 static 优先用 exe 旁边的（方便他改样式），
# 旁边没有就用打包进去的那份。
APP_DIR = os.path.dirname(os.path.abspath(sys.executable)) if FROZEN else os.path.dirname(os.path.abspath(__file__))
BUNDLE_DIR = getattr(sys, "_MEIPASS", APP_DIR)
if os.path.isdir(os.path.join(APP_DIR, "static")):
    BASE, STATIC = APP_DIR, os.path.join(APP_DIR, "static")
else:
    BASE, STATIC = BUNDLE_DIR, os.path.join(BUNDLE_DIR, "static")
DATA = os.path.abspath(os.environ.get("GHDL_DATA") or os.path.join(APP_DIR, "data"))
PROXIES_F = os.path.join(DATA, "proxies.json")
CONFIG_F = os.path.join(DATA, "config.json")
HISTORY_F = os.path.join(DATA, "history.json")
LOG_F = os.path.join(DATA, "server.log")

# 探活用几百字节的小文件：只测「通不通 + 大概多快」，不浪费流量
PROBE_TARGET = "https://github.com/octocat/Hello-World/raw/master/README"
CURL = "curl.exe"
DEVNULL = "NUL"
_WIN = os.name == "nt"

DEFAULT_PROXIES = {
    "_说明": "prefix 后面直接拼完整的 https 地址；enabled=false 的不参与尝试；order 小的先试",
    "proxies": [
        {"id": "ghpro", "name": "gh-proxy.com", "prefix": "https://gh-proxy.com/",
         "enabled": True, "order": 1, "last_ms": None, "last_ok": None, "tried": 0, "ok": 0, "last_at": None},
        {"id": "ghfast", "name": "ghfast.top", "prefix": "https://ghfast.top/",
         "enabled": True, "order": 2, "last_ms": None, "last_ok": None, "tried": 0, "ok": 0, "last_at": None},
        {"id": "ghpnet", "name": "ghproxy.net", "prefix": "https://ghproxy.net/",
         "enabled": True, "order": 3, "last_ms": None, "last_ok": None, "tried": 0, "ok": 0, "last_at": None},
        {"id": "direct", "name": "直连（不走加速）", "prefix": "",
         "enabled": True, "order": 99, "last_ms": None, "last_ok": None, "tried": 0, "ok": 0, "last_at": None},
    ],
}

DEFAULT_CONFIG = {
    "port": 8765,
    "default_dir": "",            # 留空 = 自动用 ~/Downloads，见 default_downloads_dir()
    "quick_dirs": [],              # 常用目录快捷按钮；首次运行后你可以在界面里加自己的（存进 config.json）
    "last_dir": "",                # 上次真正下载到哪；下次打开页面默认就填它
    "recent_dirs": [],             # 最近用过的目录，最多 8 条
    "speed_floor_bps": 2048,      # 连续 speed_floor_secs 秒低于这个速度 → 判定这个源卡死，换下一个
    "speed_floor_secs": 20,       # 实测：连上却一直不回数据的源（多是直连 raw.githubusercontent.com）靠这条才不至于干等
    "stall_secs": 30,             # 连续 30 秒文件字节没增长就判这个源卡死（TLS 握手 / CRL 查询卡住时 curl 自己的超时管不到）
    "attempt_cap_secs": 600,      # 单次尝试的绝对上限，防病态挂死
    "max_concurrent": 3,          # 同时下载/转换几个；批量粘贴时其余排队
    "proxy_url": "",              # 可选：给 curl 用的系统/公司代理，如 http://127.0.0.1:7890
    "external_proxy": False,      # 非 GitHub 链接是否也走下面那串 GitHub 加速源（默认直连）
    "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "engines": {},                # 手动指定的引擎路径，如 {"libreoffice": "...soffice.exe"}
    "verify": True,
}

LOCK = threading.RLock()
TASKS: dict = {}
STARTED_AT = time.time()
SERVER = None                    # main() 里赋成 httpd，界面点「停止服务」时要它收尾
BUSY_STATES = ("queued", "parsing", "probing", "downloading", "verify",
               "converting", "installing", "need_asset")
SEM = threading.BoundedSemaphore(3)   # 并发上限；main() 里按配置重建
DIALOG_LOCK = threading.Lock()        # 同一时间只弹一个系统选择框


def hard_exit():
    """给自己半秒把响应发回去，然后退出。exe 没有控制台窗口，界面里必须留这么个出口。"""
    # os._exit 不会替我们收子进程：先把还在跑的 curl 停掉，否则它们会变成孤儿，
    # 继续往 .part 里写（下一次自测就会看到莫名其妙的 (2) 和抢带宽）。
    try:
        if CONV is not None:
            CONV.kill_all()
    except Exception:
        pass
    try:
        with LOCK:
            tasks = list(TASKS.values())
        for t in tasks:
            t["cancel"] = True
            p = t.get("proc")
            if p is not None:
                reap(p)
    except Exception:
        pass
    try:
        if SERVER:
            SERVER.shutdown()
    except Exception:
        pass
    log("服务退出")
    os._exit(0)


# =========================================================== 配置 / 存储的底层小工具

def log(msg):
    line = "%s %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        os.makedirs(DATA, exist_ok=True)
        with open(LOG_F, "a", encoding="utf-8", errors="replace") as f:
            f.write(line + "\n")
    except OSError:
        pass
    try:
        say(line)
    except Exception:
        pass


def read_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except (OSError, ValueError) as e:
        log("读 %s 失败：%s（退回默认值）" % (path, e))
        return default


def write_json(path, obj):
    """原子写：先落 .tmp 再 replace，免得中途被强杀留下半截 JSON。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def default_downloads_dir():
    d = os.path.join(os.path.expanduser("~"), "Downloads")
    return d if os.path.isdir(d) else os.path.expanduser("~")


def ensure_data():
    os.makedirs(DATA, exist_ok=True)
    if not os.path.exists(PROXIES_F):
        write_json(PROXIES_F, DEFAULT_PROXIES)
    if not os.path.exists(CONFIG_F):
        cfg = dict(DEFAULT_CONFIG)
        cfg["default_dir"] = default_downloads_dir()
        write_json(CONFIG_F, cfg)
    if not os.path.exists(HISTORY_F):
        write_json(HISTORY_F, {"items": []})


def get_config():
    cfg = dict(DEFAULT_CONFIG)
    cfg.update(read_json(CONFIG_F, {}) or {})
    if not cfg.get("default_dir"):
        cfg["default_dir"] = default_downloads_dir()
    return cfg


def get_proxies():
    doc = read_json(PROXIES_F, DEFAULT_PROXIES)
    lst = [p for p in doc.get("proxies", []) if isinstance(p, dict) and "prefix" in p]
    for i, p in enumerate(lst):
        p.setdefault("id", "px%d" % i)
        p.setdefault("name", p["prefix"] or "直连")
        p.setdefault("enabled", True)
        p.setdefault("order", i + 1)
    lst.sort(key=lambda p: (p.get("order") or 999))
    return lst


def save_proxies(lst):
    for i, p in enumerate(lst):
        p["order"] = i + 1
    write_json(PROXIES_F, {"_说明": DEFAULT_PROXIES["_说明"], "proxies": lst})


def get_history():
    return list(read_json(HISTORY_F, {"items": []}).get("items", []))


def add_history(rec):
    with LOCK:
        items = [x for x in get_history() if x.get("key") != rec.get("key")]
        items.insert(0, rec)
        write_json(HISTORY_F, {"items": items[:300]})


def norm_prefix(prefix):
    """把用户填的代理前缀规范成 https://xxx/ 结尾的样子。留空 = 直连。"""
    p = (prefix or "").strip()
    if not p:
        return ""
    if not re.match(r"^https?://", p, re.I):
        p = "https://" + p
    if not p.endswith("/"):
        p += "/"
    return p


def expand_path(path):
    return os.path.abspath(os.path.expandvars(os.path.expanduser((path or "").strip().strip('"'))))


def safe_filename(name, fallback="download.bin"):
    """去掉查询串和 Windows 非法字符；太长就截断主体保留扩展名。
    先做 URL 解码：有些站点把中文名写成 %E7%BC%96%E7%A8%8B.ico，
    以前会原样落盘成乱码名。
    """
    name = unquote(str(name or "")).strip().strip('"')
    name = name.split("?")[0].split("#")[0]
    name = os.path.basename(name.replace("\\", "/"))
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    if not name:
        name = fallback
    if len(name) > 150:
        stem, dot, ext = name.rpartition(".")
        name = (stem[:120] + "." + ext) if dot else name[:150]
    return name


def fmt_bytes(n):
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "-"
    if n < 1024:
        return "%d B" % n
    for unit in ("KB", "MB", "GB", "TB"):
        n /= 1024.0
        if n < 1024 or unit == "TB":
            return ("%.0f %s" if n >= 100 else "%.1f %s") % (n, unit)
    return "-"


# =========================================================== 第 1 步：把他粘贴的东西变成能下的地址

def strip_proxy_prefix(url):
    """已经套过代理的链接剥回来，这样他粘贴加速链接也能用，不会套两层。"""
    n = 0
    while True:
        m = re.match(r"^https?://[^/]+/(https?://.+)$", url, re.I)
        if not m:
            return url, n
        url = m.group(1)
        n += 1


def parse_url(raw, mode="auto"):
    """
    返回 plan：{ok, kind, url, filename, repo, tag, note, is_github, host, direct_first}
    kind ∈ asset | raw | archive | repo_zip | clone | release_page | external | unknown
    """
    u = (raw or "").strip().strip('"').strip("'")
    if not u:
        return {"ok": False, "error": "链接是空的"}
    if not re.match(r"^https?://", u, re.I):
        if "github.com/" in u or "githubusercontent.com/" in u:
            u = "https://" + u.lstrip("/")
        elif re.match(r"^[\w.-]+\.[a-z]{2,}(?::\d+)?(?:/|$)", u, re.I):
            u = "https://" + u.lstrip("/")     # example.com/a.pdf 也认
        else:
            return {"ok": False, "error": "这不像 http(s) 链接：%s" % u[:60]}
    u, stripped = strip_proxy_prefix(u)
    note = "粘贴的链接里已经带代理前缀，先剥掉了" if stripped else ""

    pr = urlparse(u)
    host = (pr.hostname or "").lower()
    segs = [s for s in pr.path.split("/") if s]
    is_gh = (host in ("github.com", "www.github.com", "raw.githubusercontent.com",
                      "codeload.github.com")
             or host.endswith(".github.com") or host.endswith(".githubusercontent.com"))
    plan = {"ok": True, "kind": "unknown", "url": u, "filename": None,
            "repo": None, "tag": None, "note": note,
            "is_github": is_gh, "host": host, "direct_first": False}

    if host == "raw.githubusercontent.com":
        plan.update(kind="raw", filename=safe_filename(segs[-1] if segs else ""))
        return plan

    if host == "codeload.github.com":
        plan.update(kind="archive", filename=safe_filename(segs[-1] if segs else "", "archive.zip"))
        return plan

    if host in ("github.com", "www.github.com") or host.endswith(".github.com"):
        # /owner/repo/releases/download/<tag>/<asset>
        if len(segs) >= 5 and segs[2] == "releases" and segs[3] == "download":
            o, r, tag = segs[0], segs[1], segs[4]
            asset = "/".join(segs[5:]) or "asset"
            plan.update(kind="asset", repo="%s/%s" % (o, r), tag=tag, filename=safe_filename(asset))
            return plan
        # /owner/repo/archive/refs/heads/main.zip
        if len(segs) >= 4 and segs[2] == "archive":
            plan.update(kind="archive", repo="%s/%s" % (segs[0], segs[1]),
                        filename=safe_filename(segs[-1], "archive.zip"))
            return plan
        # /owner/repo/raw/<ref>/<path> 和 /blob/<ref>/<path>（blob 是网页版，转成 raw 才是纯文件）
        if len(segs) >= 5 and segs[2] in ("raw", "blob"):
            o, r, ref = segs[0], segs[1], segs[3]
            path = "/".join(segs[4:])
            plan.update(kind="raw", repo="%s/%s" % (o, r),
                        url="https://raw.githubusercontent.com/%s/%s/%s/%s" % (o, r, ref, path),
                        filename=safe_filename(os.path.basename(path)),
                        note=(note + ("；blob 网页链接已转成 raw 直链" if segs[2] == "blob" else "")).strip("；"))
            return plan
        # /owner/repo/releases 或 /releases/tag/v1.2.3 → 需要先列文件
        if len(segs) >= 3 and segs[2] == "releases":
            o, r = segs[0], segs[1]
            tag = "latest"
            if len(segs) >= 5 and segs[3] in ("expanded_assets", "tag"):
                tag = segs[4]
            plan.update(kind="release_page", repo="%s/%s" % (o, r), tag=tag,
                        note="这是发布页不是某个文件的直链，点【列出该页文件】选一个")
            return plan
        # 仓库首页 / tree / issues → 整仓
        if len(segs) >= 2 and (len(segs) == 2 or segs[2] in ("tree", "issues", "actions", "wiki")):
            o, r = segs[0], segs[1]
            if mode == "clone":
                plan.update(kind="clone", repo="%s/%s" % (o, r),
                            url="https://github.com/%s/%s.git" % (o, r),
                            filename=r, note="按选的方式走 git clone（浅克隆，只要最新一次提交）")
            else:
                plan.update(kind="repo_zip", repo="%s/%s" % (o, r),
                            url="https://github.com/%s/%s/archive/HEAD.zip" % (o, r),
                            filename="%s-HEAD.zip" % r,
                            note="仓库页默认下默认分支的整仓 zip（HEAD 会自动跳到默认分支）；要 .git 历史就选强制 clone")
            return plan
        if segs:
            plan.update(kind="external", filename=safe_filename(segs[-1]))
            return plan

    # 非 GitHub：文件名先解码再取 basename（%E7%BC%96%E7%A8%8B.ico → 编程.ico），
    # 拿不到名字也不要紧，下载前会用 Content-Disposition 补。
    path_dec = unquote(pr.path or "")
    fn = os.path.basename(path_dec) or "download.bin"
    if fn in ("download.bin", "download", ""):
        qs = parse_qs(pr.query or "")
        for key in ("filename", "name", "file", "download"):
            if qs.get(key) and qs[key][0].strip():
                fn = qs[key][0].strip()
                break
    plan.update(kind="external", filename=safe_filename(fn), direct_first=True,
                note=(note + "；非 GitHub 链接，直连优先（要套加速源可在设置里打开）").strip("；"))
    return plan


# =========================================================== 第 2 步：curl 封装 + 代理测速

def decode_out(b):
    for enc in ("utf-8", "gbk"):
        try:
            return b.decode(enc)
        except UnicodeDecodeError:
            continue
    return b.decode("utf-8", "replace")


def curl(args, timeout=None):
    """跑一次 curl.exe，返回 (退出码, stdout+stderr 合并文本)。参数里要自己带上目标 URL。"""
    try:
        p = subprocess.run([CURL] + list(args), capture_output=True, timeout=timeout,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return p.returncode, decode_out((p.stdout or b"") + (p.stderr or b""))
    except FileNotFoundError:
        return -1, "找不到 curl.exe（Win10 1803 之后系统自带）"
    except subprocess.TimeoutExpired:
        return -2, "本地等待超时，已放弃这次请求"


def split_stats(out, marker="%{http_code}"):
    """从 curl -w 的输出尾部取统计串。"""
    lines = [l for l in (out or "").strip().splitlines() if l.strip()]
    return lines[-1] if lines else ""


def probe_proxy(px):
    """小文件探活 + 粗略测速，结果写回 proxies.json。"""
    target = norm_prefix(px.get("prefix")) + PROBE_TARGET
    t0 = time.time()
    rc, out = curl(["-sS", "-L", "--fail", "--max-time", "12", "--connect-timeout", "8",
                    "-o", DEVNULL, "-w", "%{http_code}|%{size_download}"] + proxy_args() + [target],
                   timeout=20)
    ms = int((time.time() - t0) * 1000)
    code = size = 0
    tail = split_stats(out)
    if "|" in tail:
        try:
            code, size = int(tail.split("|")[0]), int(float(tail.split("|")[1]))
        except ValueError:
            pass
    ok = (rc == 0 and code == 200 and size > 0)
    with LOCK:
        lst = get_proxies()
        for p in lst:
            if p.get("id") == px.get("id"):
                p["last_ms"], p["last_ok"], p["last_at"] = ms, ok, time.strftime("%H:%M:%S")
                p["tried"] = (p.get("tried") or 0) + 1
                p["ok"] = (p.get("ok") or 0) + (1 if ok else 0)
        save_proxies(lst)
    return {"id": px.get("id"), "name": px.get("name"), "ms": ms, "ok": ok,
            "http": code, "bytes": size,
            "err": None if ok else (tail or out.strip()[-160:] or "curl 退出码 %s" % rc)}


def probe_all():
    """并发测速，不然三个源各等 12 秒太磨人。"""
    lst = [p for p in get_proxies() if p.get("enabled")]
    results = {}
    threads = []
    for px in lst:
        def run(p=px):
            results[p["id"]] = probe_proxy(p)
        t = threading.Thread(target=run, daemon=True)
        threads.append(t)
        t.start()
    for t in threads:
        t.join(timeout=30)
    return [results.get(px["id"]) or {"id": px["id"], "name": px["name"], "ok": False,
                                      "err": "测速线程没返回"} for px in lst]


def pick_order(plan=None):
    """启用中的代理排队：上次失败的排最后，其余按最近耗时升序，没测过的按手动顺序。

    非 GitHub 链接默认只走「直连」：gh-proxy 这类加速源本来就是给 GitHub 做的，
    把 work 的内网/对象存储地址拼在它后面，等来的只会是 404/403，白白浪费十几秒。
    """
    lst = [p for p in get_proxies() if p.get("enabled")]
    cfg = get_config()
    if plan is not None and not plan.get("is_github"):
        direct = [p for p in lst if not (p.get("prefix") or "").strip()]
        if not cfg.get("external_proxy"):
            return direct or lst[:1]
        others = [p for p in lst if (p.get("prefix") or "").strip()]
        return direct + sorted(others, key=lambda p: (p.get("order") or 99))

    def key(p):
        ms = p.get("last_ms")
        return (1 if p.get("last_ok") is False else 0,
                ms if isinstance(ms, int) else 10 ** 9,
                p.get("order") or 99)
    return sorted(lst, key=key)


# =========================================================== 第 3 步：下完之后的校验

class Canceled(Exception):
    """校验一个几 GB 的文件也要能停下来，所以哈希循环里留了个检查点。"""


def sha256_of(path, tick=None):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8 * 1024 * 1024), b""):
            h.update(chunk)
            if tick and tick():
                raise Canceled()
    return h.hexdigest()


def archive_check(path):
    """压缩包做「全量」结构校验，不是只看文件头 —— 半截文件只有全读才暴露得出来。"""
    low = path.lower()
    try:
        if low.endswith((".zip", ".apk", ".jar", ".whl", ".xpi", ".epub")):
            with zipfile.ZipFile(path) as z:
                bad = z.testzip()
                n = len(z.namelist())
            if bad:
                return {"archive": "fail", "detail": "zip 在 %s 处 CRC 失败，文件是不完整的" % bad}
            return {"archive": "ok", "detail": "zip 全量 CRC 通过（%d 个条目）" % n}
        if low.endswith((".tar.gz", ".tgz")):
            import tarfile
            n = 0
            with tarfile.open(path, "r:gz") as t:
                for _ in t:
                    n += 1
            return {"archive": "ok", "detail": "tar.gz 完整解包通过（%d 个成员）" % n}
        if low.endswith(".gz") and not low.endswith(".tar.gz"):
            import gzip
            with gzip.open(path, "rb") as g:
                while g.read(1 << 20):
                    pass
            return {"archive": "ok", "detail": "gzip 解压通过"}
    except Exception as e:
        return {"archive": "fail", "detail": "%s: %s" % (type(e).__name__, e)}
    return {"archive": "skip", "detail": "不是压缩类文件，跳过结构校验"}


def authenticode(path):
    """
    验 exe / msi 的数字签名。
    坑：PowerShell 5.1 读无 BOM 的 UTF-8 脚本会按 GBK 解析，中文路径直接废掉，
    所以这里的临时 .ps1 用 utf-8-sig 写、换行 CRLF，并强制控制台输出 UTF-8。
    """
    if not path.lower().endswith((".exe", ".dll", ".msi", ".scr", ".appx")):
        return {"sig": "skip", "detail": "非可执行文件，不查签名"}
    body = [
        "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8",
        "$ErrorActionPreference='Continue'",
        "$p = '%s'" % path.replace("'", "''"),
        "$s = Get-AuthenticodeSignature -LiteralPath $p",
        "$subj = ''",
        "if ($s.SignerCertificate) { $subj = [string]$s.SignerCertificate.Subject }",
        "Write-Output ($s.Status.ToString() + '|' + $subj)",
    ]
    tmp = ""
    try:
        fd, tmp = tempfile.mkstemp(suffix=".ps1")
        os.close(fd)
        with open(tmp, "w", encoding="utf-8-sig", newline="\r\n") as f:
            f.write("\r\n".join(body) + "\r\n")
        p = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", tmp],
                           capture_output=True, timeout=90,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        lines = decode_out(p.stdout or b"").strip().splitlines()
        line = lines[-1] if lines else ""
        status, subj = (line.split("|", 1) + [""])[:2] if "|" in line else (line, "")
        ok = status.strip().lower() == "valid"
        signer = subj.split(",")[0]
        if signer.startswith("CN="):
            signer = signer[3:]
        return {"sig": "ok" if ok else "bad", "status": status.strip() or "Unknown",
                "signer": signer,
                "detail": ("签名有效，签发者：%s" % subj) if ok else
                          ("签名状态 = %s（未签名、证书过期或被撤销都算这类，装之前自己拿主意）" % (status.strip() or "读不到"))}
    except FileNotFoundError:
        return {"sig": "skip", "detail": "找不到 powershell.exe，没查签名"}
    except subprocess.TimeoutExpired:
        return {"sig": "skip", "detail": "查签名超时（多半是联网验证书吊销列表被卡），只给了 SHA256"}
    except Exception as e:
        return {"sig": "skip", "detail": "查签名失败：%s" % e}
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def official_checksum(plan, sha_hex):
    """
    release 目录常放 checksums.txt 或 <文件名>.sha256。找到就比对，找不到就说清楚找过哪几个。
    只对 release 资产尝试，且只试队列里前两个源，不为几十字节卡住主流程。
    """
    fn = plan.get("filename") or ""
    if plan.get("kind") == "release_page":
        return {"found": False, "detail": "给的是发布页，填那个文件自己的下载链接才能比对官方校验值"}
    if plan.get("kind") != "asset" or not fn:
        return {"found": False, "detail": "不是 release 资产链接，没有官方校验文件可取"}
    base = plan["url"].rsplit("/", 1)[0]
    cands = [fn + ".sha256", "checksums.txt", "sha256-checksums.txt", "SHA256SUMS.txt", "sha256sums.txt"]
    tried = 0
    for px in pick_order()[:2]:
        for c in cands:
            url = norm_prefix(px.get("prefix")) + base + "/" + c
            rc, out = curl(["-sS", "-L", "--max-time", "12", "--max-filesize", "2000000",
                            "-w", "|CODE|%{http_code}"] + proxy_args() + [url], timeout=20)
            tried += 1
            body, _, code_s = out.rpartition("|CODE|")
            try:
                code = int(code_s.strip())
            except ValueError:
                code = 0
            text = (body or "").strip()
            if code != 200 or not text or len(text) > 2_000_000:
                continue
            hit = None
            lines = text.splitlines()
            for line in lines:
                if fn.lower() in line.lower() or len(lines) == 1:
                    if sha_hex and sha_hex.lower() in line.lower():
                        hit = ("match", line.strip()[:140])
                        break
                    if fn.lower() in line.lower():
                        hit = hit or ("miss", line.strip()[:140])
            if hit:
                kind, line = hit
                return {"found": True, "source": c, "match": kind == "match", "line": line,
                        "detail": ("和官方 %s 一致" % c) if kind == "match"
                                   else ("官方 %s 里这个文件的值对不上！" % c)}
    return {"found": False, "detail": "试了 %d 个常见校验文件名都没找到（作者不放校验文件很常见，不算异常）" % tried}


def summarize_verify(v):
    """把校验结果拼成一句人话。skip 类的噪音（"非可执行文件，不查签名"）不写进来。"""
    bits = []
    if v.get("sha256"):
        bits.append("SHA256 %s…" % v["sha256"][:12])
    if v.get("archive") == "ok":
        bits.append(v.get("archive_detail") or "压缩包结构完整")
    elif v.get("archive") == "fail":
        bits.append("压缩包校验失败：" + (v.get("archive_detail") or ""))
    if v.get("sig") == "ok":
        bits.append("签名有效（%s）" % (v.get("signer") or "?"))
    elif v.get("sig") == "bad":
        bits.append("签名状态 %s" % v.get("status"))
    elif v.get("sig") == "skip" and v.get("sig_detail") and "不查签名" not in (v.get("sig_detail") or ""):
        bits.append(v["sig_detail"])
    cs = v.get("checksum") or {}
    if cs.get("found"):
        bits.append("官方校验值一致" if cs.get("match") else "官方校验值对不上！")
    elif v.get("sha256"):
        bits.append(cs.get("detail", "没查官方校验值"))
    if v.get("note"):
        bits.append(v["note"])
    return " · ".join(bits) or "已完成"


# =========================================================== 第 4 步：下载执行（含换源续传）

def task_log(t, msg):
    with LOCK:
        t.setdefault("log", []).append("[%s] %s" % (time.strftime("%H:%M:%S"), msg))
        t["log"] = t["log"][-200:]
    log("[任务 %s] %s" % (t["id"], msg))


def unique_dest(destdir, filename):
    """同名文件已存在就自动加 (2)(3)，绝不覆盖他已有的下载。"""
    base, ext = os.path.splitext(filename)
    cand = os.path.join(destdir, filename)
    i = 2
    while os.path.exists(cand) or os.path.exists(cand + ".part"):
        cand = os.path.join(destdir, "%s (%d)%s" % (base, i, ext))
        i += 1
    return cand


def dir_bytes(path):
    total = 0
    for root, _dirs, files in os.walk(path):
        if ".git" in root.split(os.sep):
            continue
        for fn in files:
            try:
                total += os.path.getsize(os.path.join(root, fn))
            except OSError:
                pass
    return total


def header_value(hdr_file, name):
    """curl -D 导出的是包含所有重定向的头部块，取最后一个才算真身。"""
    try:
        with open(hdr_file, "r", encoding="utf-8", errors="replace") as f:
            txt = f.read()
    except OSError:
        return None
    vals = re.findall(r"(?im)^%s:\s*(.+)\s*$" % name, txt)
    return vals[-1] if vals else None


def header_total(hdr_file, offset=0):
    """
    只认最后一个响应块，且必须是 200/206 —— 否则 404 页面自己的 14 字节
    Content-Length 会被当成"文件总大小"，界面上就出现"共 14 B"这种胡话。
    """
    try:
        with open(hdr_file, "r", encoding="utf-8", errors="replace") as f:
            txt = f.read()
    except OSError:
        return None
    blocks = [b for b in txt.split("\r\n\r\n") if b.strip()]
    if not blocks:
        return None
    last = blocks[-1]
    m = re.match(r"(?i)^HTTP/[\d.]+\s+(\d{3})", last)
    if not m or m.group(1) not in ("200", "206"):
        return None
    cl = re.search(r"(?im)^content-length:\s*(\d+)", last)
    return int(cl.group(1)) + offset if cl else None


def header_filename(hdr_file):
    raw = header_value(hdr_file, "content-disposition")
    if not raw:
        return None
    m = re.search(r'filename\*?\s*=\s*"?([^";]+)', raw, re.I)
    if not m:
        return None
    name = m.group(1).strip().strip('"')
    if name.upper().startswith("UTF-8''"):
        name = unquote(name[7:])
    name = safe_filename(name)
    return name if name != "download.bin" else None


def header_range_total(hdr_file):
    """Range 请求（0-0）时总大小在 Content-Range 里，不在 Content-Length。"""
    try:
        with open(hdr_file, "r", encoding="utf-8", errors="replace") as f:
            txt = f.read()
    except OSError:
        return None
    m = re.search(r"(?im)^content-range:\s*bytes\s+\d+-\d+/(\d+)", txt)
    return int(m.group(1)) if m else None


def request_headers(plan, t=None):
    """把配置里的 UA 和任务里的 Referer/Cookie/Authorization 拼成 curl -H 参数。
    日志里绝不打印这些值 —— 它们经常是内网 token。"""
    cfg = get_config()
    t = t or {}
    h = []
    ua = (t.get("user_agent") or cfg.get("user_agent") or "").strip()
    if ua:
        h += ["-H", "User-Agent: " + ua]
    for key, hname in (("referer", "Referer"), ("cookie", "Cookie"),
                       ("authorization", "Authorization")):
        v = (t.get(key) or "").strip()
        if v and "\r" not in v and "\n" not in v:
            h += ["-H", "%s: %s" % (hname, v)]
    return h


def proxy_args():
    p = (get_config().get("proxy_url") or "").strip()
    return ["--proxy", p] if p else []


def probe_url(plan, t=None):
    """
    下载前做一次 HEAD 预检：
      - 拿到真正的文件名（Content-Disposition，尤其 download?id=123 这种）
      - 拿到总大小，进度条一开始就是准的
      - 认出「这其实是个网页/登录页」，别把 HTML 存成 .exe
    HEAD 不被支持时退回 Range: bytes=0-0，只取一个字节。失败就当没预检，不影响下载。
    """
    hdr = tempfile.mktemp(prefix="ghdl_head_", suffix=".hdr")
    common = ["-sS", "-L", "--max-time", "15", "--connect-timeout", "8",
              "-D", hdr, "-o", DEVNULL] + request_headers(plan, t) + proxy_args()
    info = {"ok": False, "code": 0, "filename": None, "size": None,
            "ctype": "", "html": False, "final_url": plan.get("url")}
    try:
        rc, out = curl(common + ["-I", "-w", "%{http_code}|%{url_effective}", plan["url"]],
                       timeout=25)
        code = 0
        tail = split_stats(out)
        if "|" in tail:
            try:
                code = int(tail.split("|")[0])
            except ValueError:
                code = 0
        if rc != 0 or code in (0, 403, 405, 400, 501):
            # 有些站（对象存储的预签名链接、内网系统）不认 HEAD，只认 GET
            try:
                os.unlink(hdr)
            except OSError:
                pass
            rc, out = curl(common + ["-r", "0-0", "-w", "%{http_code}|%{url_effective}",
                                     plan["url"]], timeout=25)
            tail = split_stats(out)
            if "|" in tail:
                try:
                    code = int(tail.split("|")[0])
                except ValueError:
                    code = 0
        info["code"] = code
        info["final_url"] = tail.split("|", 1)[1] if "|" in tail else plan.get("url")
        info["filename"] = header_filename(hdr)
        info["ctype"] = (header_value(hdr, "content-type") or "").split(";")[0].strip().lower()
        size = header_range_total(hdr) or header_total(hdr)
        info["size"] = size if size and size > 0 else None
        info["html"] = info["ctype"] in ("text/html", "application/xhtml+xml")
        info["ok"] = 200 <= code < 400
        return info
    except Exception as e:
        info["error"] = str(e)
        return info
    finally:
        try:
            os.unlink(hdr)
        except OSError:
            pass


def reap(proc):
    """停掉 curl 并确认真的走了：terminate 不动就 kill，别留个占着文件句柄的僵尸。"""
    try:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
    except OSError:
        pass


def read_pipe(proc):
    try:
        return proc.stdout.read() if proc.stdout else b""
    except (OSError, ValueError):
        return b""


def run_file_download(t, plan):
    """
    核心策略：一个源一个源试。
    中断时不清掉已下部分（写在 <目标>.part 里），下一个源用 curl -C - 从断点接着要，
    所以「换源」不等于「重来」。
    源不支持 Range（curl 退出码 33）时，先让同一个源从头再来一次 —— 别把它用掉，
    否则碰上"只有一个源能用而且它不支持续传"的情况就会自己把自己绕死。
    """
    cfg = get_config()
    dest = t["dest"]
    part = dest + ".part"
    order = pick_order(plan)
    i = 0
    fresh_retried = set()
    while i < len(order):
        px = order[i]
        if t.get("cancel"):
            task_log(t, "已取消，不再尝试其它源")
            return False
        prefix = norm_prefix(px.get("prefix"))
        t["proxy"] = px.get("name")
        with LOCK:
            t.setdefault("attempts", []).append({"proxy": px.get("name"), "started": time.time(),
                                                 "rc": None, "bytes": 0, "speed": 0.0, "secs": 0})
            attempt = t["attempts"][-1]
        resume = os.path.getsize(part) if os.path.exists(part) else 0
        target = prefix + plan["url"]
        hdr = part + ".hdr"

        def mark(okflag, took=None):
            with LOCK:
                lst = get_proxies()
                for p in lst:
                    if p.get("id") == px.get("id"):
                        p["tried"] = (p.get("tried") or 0) + 1
                        p["ok"] = (p.get("ok") or 0) + (1 if okflag else 0)
                        p["last_ok"] = okflag
                        if took and not p.get("last_ms"):
                            p["last_ms"] = int(took * 1000)
                save_proxies(lst)
        args = ["-sS", "-L", "--fail", "--connect-timeout", "12",
                "--speed-limit", str(int(cfg["speed_floor_bps"])),
                "--speed-time", str(int(cfg["speed_floor_secs"])),
                "-D", hdr, "-o", part,
                "-w", "%{http_code}|%{size_download}|%{speed_download}|%{time_total}"]
        args += request_headers(plan, t) + proxy_args()
        if resume:
            args += ["-C", "-"]
        args.append(target)

        task_log(t, "第 %d 次尝试：%s%s → %s" % (
            len(t["attempts"]), px.get("name"), "" if prefix else "（直连）", plan["url"][:90]))
        if resume:
            task_log(t, "  发现未完成文件，从 %s 处续传" % fmt_bytes(resume))

        proc = prev = None
        prev_ts = started_ts = time.time()
        last_change = started_ts      # 文件最后一次变大的时刻，比 curl 自己的超时靠谱
        samples = []                  # 最近 15 秒的 (时刻, 字节数)，用来算滑动窗口速度
        stalled = False
        stall_secs = int(cfg.get("stall_secs") or 30)
        cap_secs = int(cfg.get("attempt_cap_secs") or 600)   # 单次尝试的绝对上限，防病态挂死
        try:
            proc = subprocess.Popen([CURL] + args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            with LOCK:
                t["proc"] = proc
            while proc.poll() is None:
                time.sleep(0.4)
                try:
                    size = os.path.getsize(part)
                except OSError:
                    size = resume
                now = time.time()
                dt = max(now - prev_ts, 1e-6)
                inst = (size - (prev or resume)) / dt if prev is not None else 0.0
                if prev is None or size > prev:
                    last_change = now
                prev, prev_ts = size, now
                with LOCK:
                    t["downloaded"] = size
                    t["total"] = t.get("total") or header_total(hdr, resume)
                    if inst > 0:
                        t["speed"] = round(inst, 1)
                    t["state"] = "downloading"
                if t.get("cancel"):
                    reap(proc)
                    task_log(t, "收到取消，已停止")
                    return False
                # curl 的 --connect-timeout 只管 TCP 连接，TLS 握手和 schannel 查证书吊销
                # 都能在外面卡很久；--speed-limit 又只在有数据传输时才作数。
                # 所以"有没有进展"这道钟必须由我们自己来掐。
                if now - last_change > stall_secs:
                    stalled = "连续 %d 秒没动静" % stall_secs
                    break
                # 只盯"有没有增长"会被挤牙膏式的源骗过去：每次蹦一个字节，钟就永远被重置。
                # 所以再看最近 15 秒的实际增长量。用滑动窗口而不是"自开始以来的平均值"，
                # 是为了不把刚建连、还没热起来的好源误踢掉。
                samples.append((now, size))
                while samples and now - samples[0][0] > 15:
                    samples.pop(0)
                if len(samples) >= 2 and now - started_ts > 15:
                    gained = size - samples[0][1]
                    if gained / (now - samples[0][0]) < float(cfg["speed_floor_bps"]):
                        stalled = "最近 15 秒只有 %s/s" % fmt_bytes(gained / (now - samples[0][0]))
                        break
                if now - started_ts > cap_secs:
                    stalled = "超过单次上限 %d 秒" % cap_secs
                    break
            if stalled:
                reap(proc)
                task_log(t, "  这个源%s，已经停掉它换下一个" % stalled)
            out = decode_out(read_pipe(proc))
            rc = proc.returncode if proc.returncode is not None else -9
        except FileNotFoundError:
            task_log(t, "找不到 curl.exe，这条路不通")
            return False
        except OSError as e:
            task_log(t, "启动 curl 失败：%s" % e)
            return False
        finally:
            if proc and proc.stdout:
                try:
                    proc.stdout.close()
                except OSError:
                    pass
            with LOCK:
                t["proc"] = None
        if stalled:
            attempt.update(rc="stalled", bytes=0)
            mark(False, time.time() - started_ts)
            i += 1
            continue

        tail = split_stats(out)
        code, size_dl, speed, secs = 0, 0, 0.0, 0.0
        parts = tail.split("|")
        if len(parts) == 4:
            try:
                code = int(parts[0]); size_dl = int(float(parts[1]))
                speed = float(parts[2]); secs = float(parts[3])
            except ValueError:
                pass
        attempt.update(rc=rc, bytes=size_dl, speed=round(speed, 1), secs=round(secs, 1))
        total = header_total(hdr, resume)
        try:
            size_now = os.path.getsize(part)
        except OSError:
            size_now = resume
        with LOCK:
            t["total"] = total or t.get("total")
            t["downloaded"] = size_now
        task_log(t, "  → http=%s curl退出码=%s，本地现有 %s%s，平均 %s/s，用时 %.1f 秒" % (
            code or "-", rc, fmt_bytes(size_now),
            (" / 共 %s" % fmt_bytes(total)) if total else "",
            fmt_bytes(speed) if speed else "-", secs))

        # 续传请求被拒的两种表现：curl 退出码 33（源不认 Range），或者源直接回 416。
        # 416 这一种尤其阴：curl 认定"文件应该已经下完了"，于是退出码给 0。
        # 真按它说的收尾，就会把半截文件改名成交付物，界面上还显示"完成"。
        cannot_resume = (rc == 33) or (resume > 0 and code == 416)
        if cannot_resume:
            try:
                os.unlink(part)
            except OSError:
                pass
            with LOCK:
                t["downloaded"] = 0
                t["total"] = None
            if px.get("id") not in fresh_retried:
                fresh_retried.add(px.get("id"))
                task_log(t, "  这个源续传不了（%s），删掉半截，同一个源从头再来一次"
                            % ("curl 退出码 33" if rc == 33 else "它回了 416"))
                mark(False)
                continue          # 不 i+=1：还用它，只是这次从头下
            task_log(t, "  同一个源重头下也没成，换下一个")
            mark(False)
            i += 1
            continue

        complete = rc == 0 and (resume == 0 or code in (200, 206))
        if complete and total and size_now < total:
            task_log(t, "  说是完了但字节数不够（%s / %s），当成中断，换源继续" % (fmt_bytes(size_now), fmt_bytes(total)))
            complete = False

        if complete:
            # 文件名兜底：URL 末端没名字时（比如某些 download?  接口），认服务器给的 Content-Disposition
            if not plan.get("filename") or plan["filename"] == "download.bin":
                got = header_filename(hdr)
                if got:
                    plan["filename"] = got
                    real = unique_dest(t["destdir"], got)
                    with LOCK:
                        t["filename"] = os.path.basename(real)
                        t["dest"] = real
                    dest = t["dest"]
                    part_new = dest + ".part"
                    try:
                        os.replace(part, part_new)
                        part = part_new
                    except OSError as e:
                        task_log(t, "  改名时出问题但继续：%s" % e)
            try:
                if os.path.exists(dest):
                    os.unlink(dest)
                os.replace(part, dest)
            except OSError as e:
                task_log(t, "  最后一步改名失败：%s" % e)
                mark(False)
                return False
            for junk in (hdr,):
                try:
                    os.unlink(junk)
                except OSError:
                    pass
            final = os.path.getsize(dest)
            with LOCK:
                t.update(downloaded=final, total=final, proxy_used=px.get("name"),
                         speed=round(speed, 1) or round(final / max(secs, 0.1), 1))
            mark(True)
            task_log(t, "下载完成：%s（%s）" % (dest, fmt_bytes(final)))
            return True

        mark(False)
        if rc == 22:
            task_log(t, "  服务器拒绝请求（4xx/5xx）：链接写错、release 里没这个文件，或这个源被上游挡了")
        elif size_now > resume:
            task_log(t, "  中途断了，已下的 %s 留着，换下一个源接着下" % fmt_bytes(size_now - resume))
        else:
            task_log(t, "  这个源一点数据都没给，换下一个")
        i += 1
    return False


def run_clone(t, plan):
    """git clone 走同一个代理前缀（gh-proxy 这类支持 smart HTTP）。目录大小靠轮询算。"""
    git = shutil.which("git")
    if not git:
        task_log(t, "PATH 里找不到 git.exe —— 改成「自动」模式下整仓 zip 也能拿到代码")
        return False
    name = (plan.get("repo") or "repo").split("/")[-1]
    dest = unique_dest(t["destdir"], name)
    with LOCK:
        t.update(dest=dest, filename=os.path.basename(dest))
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_LFS_SKIP_SMUDGE="1")
    _px = (get_config().get("proxy_url") or "").strip()
    if _px:
        env.update(HTTP_PROXY=_px, HTTPS_PROXY=_px, ALL_PROXY=_px)
    for px in pick_order():
        prefix = norm_prefix(px.get("prefix"))
        if not prefix:
            task_log(t, "跳过「直连」这一条：clone 直连 github.com 在这个网络下基本没戏")
            continue
        with LOCK:
            t.setdefault("attempts", []).append({"proxy": px.get("name"), "started": time.time()})
            attempt = t["attempts"][-1]
        t["proxy"] = px.get("name")
        url = prefix + plan["url"]
        task_log(t, "git clone --depth 1  经 %s" % px.get("name"))
        try:
            proc = subprocess.Popen([git, "clone", "--depth", "1", "--", url, dest],
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, encoding="utf-8", errors="replace", env=env,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            with LOCK:
                t["proc"] = proc
        except OSError as e:
            task_log(t, "启动 git 失败：%s" % e)
            return False
        stall_secs = int(get_config().get("stall_secs") or 30)
        cap_secs = int(get_config().get("attempt_cap_secs") or 600)
        started_ts = last_change = time.time()
        last_size, stalled = -1, ""
        while proc.poll() is None:
            time.sleep(0.6)
            now = time.time()
            try:
                got = dir_bytes(dest)
            except OSError:
                got = 0
            with LOCK:
                t["downloaded"] = got
                t["state"] = "downloading"
            if got != last_size:
                last_size, last_change = got, now
            elif now - last_change > stall_secs:
                stalled = "连续 %d 秒没动静" % stall_secs
            if now - started_ts > cap_secs:
                stalled = stalled or "超过单次上限 %d 秒" % cap_secs
            if t.get("cancel"):
                reap(proc)
                task_log(t, "收到取消，已停止 clone")
                return False
            if stalled:
                reap(proc)
                task_log(t, "  这个源%s，停掉换下一个" % stalled)
                break
        try:
            out = (proc.stdout.read() if proc.stdout else "") or ""
        except (OSError, ValueError):
            out = ""
        with LOCK:
            t["proc"] = None
        attempt.update(rc=proc.returncode, bytes=t.get("downloaded") or 0)
        if proc.returncode == 0:
            with LOCK:
                t.update(downloaded=dir_bytes(dest), proxy_used=px.get("name"), mode="clone")
                t["total"] = t["downloaded"]
            task_log(t, "clone 完成：%s%s" % (dest, ("\n" + out.strip()[-300:]) if out.strip() else ""))
            return True
        task_log(t, "  clone 失败（退出码 %s）：%s" % (proc.returncode, out.strip()[-220:]))
        shutil.rmtree(dest, ignore_errors=True)
    return False


# =========================================================== 第 5 步：任务状态机

def run_checks(path, stop=None):
    """对一个已存在的本地文件跑全套校验。下载完成和本地校验共用这一份，结论口径必须一致。"""
    v = {"bytes": dir_bytes(path) if os.path.isdir(path) else os.path.getsize(path)}
    if os.path.isdir(path):
        v["note"] = "目录（git 仓库），完整性由 git 自己保证"
        return v
    v["sha256"] = sha256_of(path, stop)
    # archive_check 和 authenticode 都返回一个叫 detail 的键，直接 update 会让后一个把
    # 前一个的结论盖掉（实测踩过：zip 的 CRC 明明过了，界面却显示"非可执行文件，不查签名"）。
    # 所以显式分键，谁的话记在谁名下。
    ac = archive_check(path)
    v["archive"], v["archive_detail"] = ac.get("archive"), ac.get("detail")
    au = authenticode(path)
    v["sig"], v["sig_detail"] = au.get("sig"), au.get("detail")
    for k in ("status", "signer"):
        if au.get(k):
            v[k] = au[k]
    return v


def do_verify_task(t):
    """只校验磁盘上已有的文件，不下载、不写历史（不然历史里全是没下过的东西，反而误导）。"""
    f = t["dest"]
    size = os.path.getsize(f)
    with LOCK:
        t.update(state="verify", filename=os.path.basename(f), dest=f,
                 downloaded=size, total=size)
    task_log(t, "校验本地文件：%s（%s）" % (f, fmt_bytes(size)))
    try:
        v = run_checks(f, stop=lambda: t.get("cancel"))
    except Canceled:
        with LOCK:
            t.update(state="canceled", finished=time.time())
        task_log(t, "收到取消，已停止校验")
        return
    url = (t.get("url") or "").strip()
    if url.startswith("http") and v.get("sha256"):
        plan = parse_url(url)
        if plan.get("ok"):
            task_log(t, "  再按原链接找作者发布的校验值…")
            v["checksum"] = official_checksum(plan, v["sha256"])
    with LOCK:
        t.update(state="done", finished=time.time(), verify=v,
                 secs=round(max(time.time() - t["started"], 0.1), 1),
                 downloaded=v.get("bytes") or size, total=v.get("bytes") or size,
                 proxy_used=None, proxy="本地")
        t["log"].append("[%s] %s" % (time.strftime("%H:%M:%S"), summarize_verify(v)))


def start_verify_task(fpath, src_url=""):
    # 空串不能让 os.path.abspath 兜底 —— 它会解析成服务的当前目录，
    # 报出"找不到这个文件：D:\LkWorkplace\githubDownload"这种把人绕晕的话
    if not (fpath or "").strip():
        raise ValueError("文件路径是空的")
    f = expand_path(fpath)
    if not f or not os.path.isfile(f):
        raise ValueError("找不到这个文件：%s" % f)
    tid = uuid.uuid4().hex[:8]
    t = {"id": tid, "url": (src_url or "").strip(), "mode": "verify",
         "destdir": os.path.dirname(f), "state": "queued",
         "filename": os.path.basename(f), "dest": f, "downloaded": 0, "total": None,
         "speed": 0.0, "attempts": [], "log": [], "verify": None, "error": None,
         "started": time.time(), "finished": None, "cancel": False}
    with LOCK:
        TASKS[tid] = t
    threading.Thread(target=run_task, args=(tid,), daemon=True).start()
    return t


def run_task(tid):
    t = TASKS[tid]
    dest = ""
    held = False
    try:
        if t.get("mode") == "verify":
            do_verify_task(t)
            return
        if t.get("mode") == "convert":
            run_convert_task(t)
            return
        if t.get("mode") == "install":
            run_install_task(t)
            return

        # 并发闸门：批量粘 10 个链接时，其余任务老老实实排队，
        # 不要 10 个 curl 一起抢带宽把每个都拖成龟速。
        SEM.acquire()
        held = True
        with LOCK:
            t["state"] = "parsing"
        reuse = bool(t.get("reuse") and t.get("plan") and t.get("dest"))
        if reuse:
            plan = dict(t["plan"])
            plan["ok"] = True
            if not plan.get("url"):
                plan = parse_url(t["url"], t.get("mode") or "auto")
            task_log(t, "沿用上次的落点继续：%s" % t.get("dest"))
        else:
            plan = parse_url(t["url"], t.get("mode") or "auto")
        if not plan.get("ok"):
            raise ValueError(plan.get("error") or "链接解析失败")
        if plan["kind"] == "release_page":
            with LOCK:
                t.update(state="need_asset", plan=plan, error=plan.get("note"))
            task_log(t, "这是发布页：点【列出该页文件】挑一个，或者去页面上右键复制具体文件的下载链接")
            return
        if plan["kind"] == "unknown":
            raise ValueError("认不出这个地址要下什么")
        # 用户手填的文件名优先级最高
        if t.get("want_filename"):
            plan["filename"] = safe_filename(t["want_filename"])

        # 非 GitHub 链接先做一次 HEAD 预检：文件名/大小/是不是网页。GitHub 的
        # release 直链文件名本来就在 URL 里，省掉这一次往返。
        if plan["kind"] == "external":
            task_log(t, "先探一下这个链接（文件名 / 大小 / 是不是网页）…")
            info = probe_url(plan, t)
            if info.get("ok"):
                if info.get("final_url"):
                    plan["url"] = info["final_url"]
                if info.get("filename") and not t.get("want_filename"):
                    if not plan.get("filename") or plan["filename"] in ("download.bin", "download"):
                        plan["filename"] = info["filename"]
                if info.get("size"):
                    with LOCK:
                        t["total"] = info["size"]
                ctype = info.get("ctype") or "?"
                task_log(t, "  http=%s 类型=%s%s" % (
                    info.get("code") or "-", ctype,
                    "，大小 " + fmt_bytes(info["size"]) if info.get("size") else ""))
                keep_html = t.get("force_html") or plan["url"].lower().split("?")[0].endswith((".html", ".htm"))
                if info.get("html") and not keep_html:
                    raise ValueError(
                        "这个链接返回的是网页（text/html），不是文件。可能是登录页或分享页；"
                        "如果它确实就是你要的网页文件，勾上「网页也照下」再试。")
            else:
                task_log(t, "  预检没成功（%s），直接按原链接下，不影响后面的换源重试"
                         % (info.get("error") or ("http %s" % info.get("code"))))

        os.makedirs(t["destdir"], exist_ok=True)
        with LOCK:
            t["plan"] = {k: plan.get(k) for k in
                         ("kind", "url", "filename", "repo", "note", "is_github", "host", "direct_first")}
        if plan.get("filename") and not reuse:
            real = unique_dest(t["destdir"], plan["filename"])
            with LOCK:
                # 记成落盘时真正用的名字：同名会自动变 (2)，界面不能还写着老名字
                t["filename"] = os.path.basename(real)
                t["dest"] = real
        task_log(t, "识别为 %s：%s" % (plan["kind"], plan["url"]))
        if plan.get("note"):
            task_log(t, "  %s" % plan["note"])
        with LOCK:
            t["state"] = "probing"

        ok = run_clone(t, plan) if plan["kind"] == "clone" else run_file_download(t, plan)
        dest = t.get("dest") or ""
        if not ok:
            with LOCK:
                t.update(state="canceled" if t.get("cancel") else "failed", finished=time.time())
                if t["state"] == "failed":
                    t["error"] = ("所有启用中的源都没拿下这个文件，展开日志看每次失败的原因"
                                  if plan.get("is_github") else
                                  "直连没拿下这个文件，展开日志看原因（内网系统常需要填 Referer/Cookie，"
                                  "或在设置里填公司代理）")
            # 失败/取消不能在你的目录里留下临时文件：.hdr 一律清掉；
            # .part 只在"一个字节都没进来"时清，真下到一半的留着（日志里能看到位置）。
            pbase = (t.get("dest") or "") + ".part"
            try:
                if os.path.exists(pbase + ".hdr"):
                    os.unlink(pbase + ".hdr")
                if os.path.exists(pbase) and os.path.getsize(pbase) == 0:
                    os.unlink(pbase)
            except OSError:
                pass
            add_history({"key": t["url"] + "|" + os.path.basename(dest or "失败"),
                         "at": time.strftime("%Y-%m-%d %H:%M:%S"), "name": t.get("filename") or "（失败）",
                         "path": dest, "url": t["url"], "proxy": t.get("proxy_used"),
                         "host": plan.get("host"), "kind": plan.get("kind"),
                         "attempts": len(t.get("attempts", [])), "bytes": t.get("downloaded"),
                         "secs": round(time.time() - t["started"], 1), "speed": t.get("speed"),
                         "sha256": None, "verify": "失败", "ok": False})
            return

        with LOCK:
            t["state"] = "verify"
        v = {}
        if plan["kind"] != "clone" and cfg_verify():
            v = run_checks(dest)
            if v.get("sha256") and plan.get("is_github"):
                v["checksum"] = official_checksum(plan, v["sha256"])
        elif plan["kind"] == "clone":
            v = {"bytes": t.get("downloaded"), "note": "git 仓库，完整性由 git 自己保证"}
        secs = max(time.time() - t["started"], 0.1)
        with LOCK:
            t.update(state="done", finished=time.time(), verify=v, secs=round(secs, 1),
                     total=v.get("bytes") or t.get("total"), downloaded=v.get("bytes") or t.get("downloaded"))
            t["log"].append("[%s] 校验：%s" % (time.strftime("%H:%M:%S"), summarize_verify(v)))
        add_history({"key": t["url"] + "|" + os.path.basename(dest),
                     "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                     "name": os.path.basename(dest), "path": dest, "url": t["url"],
                     "proxy": t.get("proxy_used"), "host": plan.get("host"), "kind": plan.get("kind"),
                     "attempts": len(t.get("attempts", [])),
                     "bytes": v.get("bytes"), "secs": round(secs, 1), "speed": t.get("speed"),
                     "sha256": v.get("sha256"), "verify": summarize_verify(v), "ok": True})
    except ValueError as e:
        # 预检发现"这是网页"、链接写错这类是用户可读的失败，不要往 server.log 里灌整段栈
        with LOCK:
            t.update(state="failed", finished=time.time(), error=str(e))
        task_log(t, "失败：%s" % e)
    except Exception as e:
        log(traceback.format_exc())
        with LOCK:
            t.update(state="failed", finished=time.time(), error="%s: %s" % (type(e).__name__, e))
        task_log(t, "异常：%s: %s" % (type(e).__name__, e))
    finally:
        if held:
            try:
                SEM.release()
            except ValueError:
                pass


def cfg_verify():
    return bool(get_config().get("verify", True))


def task_public(t):
    keep = ("id", "url", "mode", "destdir", "filename", "dest", "state", "proxy", "proxy_used",
            "downloaded", "total", "speed", "error", "log", "verify", "plan", "attempts", "secs",
            "outputs", "convert_items", "engine", "want_filename", "target")
    d = {k: t.get(k) for k in keep}
    d["elapsed"] = round((t.get("finished") or time.time()) - (t.get("started") or time.time()), 1)
    d["eta"] = None
    if d.get("state") == "downloading" and d.get("total") and d.get("speed"):
        d["eta"] = int(max(d["total"] - (d.get("downloaded") or 0), 0) / d["speed"])
    # 失败/取消后如果还留着 .part，界面给一个「继续」；有没有半截文件由后端说了算
    if t.get("mode") in ("convert", "install"):
        d["resumable"] = t.get("state") in ("failed", "canceled")
    else:
        d["resumable"] = bool(t.get("dest")) and (
            os.path.exists((t.get("dest") or "") + ".part")
            or t.get("state") in ("failed", "canceled"))
    return d


def remember_dir(path):
    """记住这次下到哪：下次打开页面默认填它，最近目录最多 8 条。"""
    try:
        cfg = get_config()
        d = os.path.abspath(path)
        cfg["last_dir"] = d
        old = [x for x in (cfg.get("recent_dirs") or []) if x and os.path.abspath(x) != d]
        cfg["recent_dirs"] = ([d] + old)[:8]
        write_json(CONFIG_F, cfg)
    except OSError as e:
        log("记住目录失败：%s" % e)


def start_task(url, destdir, mode, filename="", headers=None, force_html=False):
    cfg = get_config()
    raw_dir = (destdir or "").strip() or (cfg.get("last_dir") or "").strip() or cfg["default_dir"]
    target = expand_path(raw_dir)
    if not target:
        raise ValueError("下载目录是空的")
    os.makedirs(target, exist_ok=True)
    remember_dir(target)
    tid = uuid.uuid4().hex[:8]
    hdrs = {}
    for k in ("referer", "cookie", "authorization", "user_agent"):
        v = str((headers or {}).get(k) or "").strip()
        if v:
            hdrs[k] = v[:8192]
    t = {"id": tid, "url": url, "mode": mode if mode in ("auto", "file", "clone") else "auto",
         "destdir": target, "state": "queued",
         "filename": "", "dest": "", "downloaded": 0, "total": None, "speed": 0.0,
         "attempts": [], "log": [], "verify": None, "error": None,
         "started": time.time(), "finished": None, "cancel": False,
         "want_filename": safe_filename(filename) if (filename or "").strip() else "",
         "headers": hdrs, "force_html": bool(force_html)}
    with LOCK:
        TASKS[tid] = t
    threading.Thread(target=run_task, args=(tid,), daemon=True).start()
    return t


def retry_task(tid):
    """失败/取消后从 .part 接着下；转换任务则原样重跑。"""
    with LOCK:
        t = TASKS.get(tid)
        if not t:
            raise ValueError("任务不在了（服务重启过？）")
        if t.get("state") in BUSY_STATES:
            raise ValueError("这个任务还在跑，先取消再继续")
        t.update(cancel=False, error=None, finished=None, started=time.time(),
                 state="queued", speed=0.0, downloaded=0, attempts=[])
        if t.get("mode") in ("convert", "install"):
            t["outputs"] = []
            t["convert_items"] = []
        t["reuse"] = bool(t.get("dest") or t.get("mode") == "convert")
        t.setdefault("log", []).append("[%s] 继续/重试" % time.strftime("%H:%M:%S"))
    threading.Thread(target=run_task, args=(tid,), daemon=True).start()
    return t


# =========================================================== 转换任务

def start_convert_task(paths, target, outdir, options):
    if CONV is None:
        raise ValueError("转换模块加载失败：%s" % CONV_ERR)
    cfg = get_config()
    raw_dir = (outdir or "").strip() or (cfg.get("last_dir") or "").strip() or cfg["default_dir"]
    target_dir = expand_path(raw_dir)
    if not target_dir:
        raise ValueError("输出目录是空的")
    os.makedirs(target_dir, exist_ok=True)
    remember_dir(target_dir)
    paths = [expand_path(p) for p in (paths or []) if str(p or "").strip()]
    if not paths:
        raise ValueError("还没选文件")
    missing = [p for p in paths if not os.path.isfile(p)]
    if missing:
        raise ValueError("找不到文件：%s" % missing[0])
    if not isinstance(target, dict) or not target.get("ext"):
        raise ValueError("没选目标格式")
    tid = uuid.uuid4().hex[:8]
    ext = str(target.get("ext")).lstrip(".").lower()
    t = {"id": tid, "url": "、".join(os.path.basename(p) for p in paths[:3]) +
                             (" 等 %d 个" % len(paths) if len(paths) > 3 else ""),
         "mode": "convert", "destdir": target_dir, "state": "queued",
         "filename": "%d 个文件 → .%s" % (len(paths), ext),
         "dest": target_dir, "downloaded": 0, "total": len(paths), "speed": 0.0,
         "attempts": [], "log": [], "verify": None, "error": None,
         "started": time.time(), "finished": None, "cancel": False,
         "paths": paths, "target": target, "options": options or {},
         "outputs": [], "convert_items": []}
    with LOCK:
        TASKS[tid] = t
    threading.Thread(target=run_task, args=(tid,), daemon=True).start()
    return t


def run_convert_task(t):
    held = False
    try:
        SEM.acquire()
        held = True
        with LOCK:
            t["state"] = "converting"
        engines = CONV.detect_engines(get_config(), DATA)

        def tlog(msg):
            with LOCK:
                t.setdefault("log", []).append("[%s] %s" % (time.strftime("%H:%M:%S"), msg))
                t["log"] = t["log"][-300:]
                m = re.match(r"^\[(\d+)/(\d+)\]", msg or "")
                if m:
                    t["downloaded"] = max(0, int(m.group(1)) - 1)
                    t["total"] = int(m.group(2))
            log("[转换 %s] %s" % (t["id"], msg))

        res = CONV.convert_batch(t["paths"], t["target"], t["destdir"],
                                 t.get("options") or {}, tlog,
                                 cancel=lambda: bool(t.get("cancel")),
                                 engines=engines)
        ok_items = [x for x in res["items"] if x.get("ok")]
        bad_items = [x for x in res["items"] if not x.get("ok")]
        with LOCK:
            t["outputs"] = res.get("outputs") or []
            t["convert_items"] = res.get("items") or []
            t["downloaded"] = len(ok_items)
            t["total"] = len(res.get("items") or [])
            t["finished"] = time.time()
            if t.get("cancel"):
                t["state"] = "canceled"
                t["error"] = "已取消（已转好的文件仍然保留）"
            elif ok_items:
                t["state"] = "done"
                note = "转换完成 %d 个文件" % len(ok_items)
                if bad_items:
                    note += "，%d 个失败（展开日志看原因）" % len(bad_items)
                t["verify"] = {"note": note}
            else:
                t["state"] = "failed"
                t["error"] = "所有文件都没转成功，展开日志看原因"
    except Exception as e:
        log(traceback.format_exc())
        with LOCK:
            t.update(state="failed", finished=time.time(),
                     error="%s: %s" % (type(e).__name__, e))
        task_log(t, "转换异常：%s: %s" % (type(e).__name__, e))
    finally:
        if held:
            try:
                SEM.release()
            except ValueError:
                pass


# =========================================================== 可选引擎按需下载

ENGINE_REPOS = {
    "magick": {
        "repo": "ImageMagick/ImageMagick",
        # 优先 Q16 常规便携包；没有就退而求其次拿任意 x64 portable zip
        "pick": lambda n: n.endswith(".zip") and "portable-Q16-x64" in n,
        "fallback": lambda n: n.endswith(".zip") and "portable" in n and "x64" in n,
        "exe": "magick.exe",
    },
    "pandoc": {
        "repo": "jgm/pandoc",
        "pick": lambda n: n.startswith("pandoc-") and n.endswith("windows-x86_64.zip"),
        "fallback": lambda n: n.endswith("windows-x86_64.zip"),
        "exe": "pandoc.exe",
    },
}


def _github_json(url):
    """按 直连 → 代理 的顺序取 GitHub API JSON。"""
    tried = []
    for px in [{"name": "直连", "prefix": ""}] + pick_order():
        target = norm_prefix(px.get("prefix")) + url
        rc, out = curl(["-sS", "-L", "--max-time", "20",
                        "-H", "Accept: application/vnd.github+json",
                        "-H", "User-Agent: ghdl-local",
                        "-w", "|CODE|%{http_code}"] + proxy_args() + [target], timeout=30)
        body, _, code_s = out.rpartition("|CODE|")
        try:
            code = int(code_s.strip())
        except ValueError:
            code = 0
        tried.append("%s→%s" % (px.get("name"), code or rc))
        if code == 200 and body.strip().startswith("{"):
            try:
                return json.loads(body), None
            except ValueError:
                continue
    return None, "GitHub API 取不到（%s）" % "，".join(tried[:5])


def start_install_task(engine_id):
    if engine_id not in ENGINE_REPOS:
        raise ValueError("不认识这个引擎：%s" % engine_id)
    tid = uuid.uuid4().hex[:8]
    t = {"id": tid, "url": ENGINE_REPOS[engine_id]["repo"], "mode": "install",
         "engine": engine_id, "destdir": os.path.join(DATA, "engines"),
         "filename": "下载 %s 便携版" % ("ImageMagick" if engine_id == "magick" else "Pandoc"),
         "dest": "", "state": "queued", "downloaded": 0, "total": None, "speed": 0.0,
         "attempts": [], "log": [], "verify": None, "error": None,
         "started": time.time(), "finished": None, "cancel": False, "outputs": []}
    with LOCK:
        TASKS[tid] = t
    threading.Thread(target=run_task, args=(tid,), daemon=True).start()
    return t


def run_install_task(t):
    held = False
    try:
        SEM.acquire()
        held = True
        engine_id = t.get("engine")
        meta = ENGINE_REPOS.get(engine_id)
        if not meta:
            raise ValueError("不认识的引擎")
        with LOCK:
            t["state"] = "installing"
        tlog = lambda m: (task_log(t, m), None)[1]

        tlog("向 GitHub 查 %s 的最新发布…" % meta["repo"])
        data, err = _github_json("https://api.github.com/repos/%s/releases/latest" % meta["repo"])
        if not data:
            raise RuntimeError(err + "；可以手动下载后放到 data/engines/ 下")
        assets = data.get("assets") or []
        pick = next((a for a in assets if meta["pick"](a.get("name") or "")), None)
        if not pick:
            pick = next((a for a in assets if meta["fallback"](a.get("name") or "")), None)
        if not pick:
            raise RuntimeError("这个 release 里没找到便携包，请手动下载：%s"
                               % data.get("html_url", ""))
        url = pick.get("browser_download_url")
        size = pick.get("size") or 0
        tlog("找到 %s（%s），开始下载" % (pick.get("name"), fmt_bytes(size)))
        with LOCK:
            t["total"] = size or None
            t["url"] = url

        edir = os.path.join(DATA, "engines")
        os.makedirs(edir, exist_ok=True)
        zpath = os.path.join(edir, "%s-%s" % (engine_id, safe_filename(pick.get("name"), "engine.zip")))
        order = [p for p in pick_order() if p.get("prefix")] + [p for p in pick_order() if not p.get("prefix")]
        ok = False
        for px in order:
            if t.get("cancel"):
                raise RuntimeError("已取消")
            target = norm_prefix(px.get("prefix")) + url
            tlog("用 %s 下载 %s" % (px.get("name"), pick.get("name")))
            proc = subprocess.Popen(
                [CURL, "-sS", "-L", "--fail", "--connect-timeout", "12",
                 "-o", zpath, "-w", "%{http_code}|%{size_download}"] + proxy_args() + [target],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            with LOCK:
                t["proc"] = proc
            last = -1
            while proc.poll() is None:
                time.sleep(0.4)
                try:
                    got = os.path.getsize(zpath)
                except OSError:
                    got = 0
                if got != last:
                    last = got
                    with LOCK:
                        t["downloaded"] = got
                        t["state"] = "installing"
                if t.get("cancel"):
                    reap(proc)
                    with LOCK:
                        t["proc"] = None
                    raise RuntimeError("已取消")
            out = decode_out(read_pipe(proc))
            with LOCK:
                t["proc"] = None
            if proc.returncode == 0 and os.path.exists(zpath) and os.path.getsize(zpath) > 1000:
                ok = True
                break
            tlog("  这个源没成（curl 退出码 %s）" % proc.returncode)
        with LOCK:
            t["proc"] = None
        if not ok:
            raise RuntimeError("所有源都没下下来，可以手动下载后放到 data/engines/")

        tlog("下载完成，解压中…")
        dest = os.path.join(edir, engine_id)
        shutil.rmtree(dest, ignore_errors=True)
        os.makedirs(dest, exist_ok=True)
        with zipfile.ZipFile(zpath) as z:
            for m in z.infolist():
                name = m.filename.replace("\\", "/")
                if name.startswith("/") or ".." in name.split("/"):
                    continue
                z.extract(m, dest)
        try:
            os.unlink(zpath)
        except OSError:
            pass
        exe = None
        for dirpath, _dirs, files in os.walk(dest):
            if meta["exe"].lower() in [f.lower() for f in files]:
                exe = os.path.join(dirpath, [f for f in files if f.lower() == meta["exe"].lower()][0])
                break
        if not exe:
            raise RuntimeError("解压完了但没找到 %s，请手动指定路径" % meta["exe"])
        cfg = get_config()
        if not isinstance(cfg.get("engines"), dict):
            cfg["engines"] = {}
        cfg["engines"][engine_id] = exe
        write_json(CONFIG_F, cfg)
        if CONV:
            CONV.detect_engines(cfg, DATA, force=True)
        with LOCK:
            t.update(state="done", finished=time.time(), dest=os.path.dirname(exe),
                     filename=meta["exe"],
                     verify={"note": "已装好：%s" % exe})
        tlog("装好了：%s" % exe)
    except Exception as e:
        log(traceback.format_exc())
        with LOCK:
            t.update(state="failed", finished=time.time(),
                     error="%s: %s" % (type(e).__name__, e))
        task_log(t, "安装失败：%s" % e)
    finally:
        if held:
            try:
                SEM.release()
            except ValueError:
                pass


# =========================================================== release 页列文件 / 目录浏览

def release_assets(url):
    plan = parse_url(url, "auto")
    if not plan.get("ok"):
        return {"ok": False, "error": plan.get("error")}
    repo = plan.get("repo")
    if not repo:
        m = re.match(r"^https?://github\.com/([^/]+)/([^/]+)", plan.get("url") or "")
        if m:
            repo = "%s/%s" % (m.group(1), m.group(2))
    if not repo:
        return {"ok": False, "error": "这不是 GitHub 的发布页链接"}
    tag = plan.get("tag") or "latest"
    api = "https://api.github.com/repos/%s/releases/%s" % (
        repo, "latest" if tag == "latest" else "tags/" + tag)
    tried = []
    prefixes = [""] + [norm_prefix(p.get("prefix")) for p in pick_order() if p.get("prefix")]
    for prefix in prefixes:
        target = prefix + api
        rc, out = curl(["-sS", "-L", "--max-time", "15", "-H", "Accept: application/vnd.github+json",
                        "-H", "User-Agent: ghdl-local", "-w", "|CODE|%{http_code}", target], timeout=20)
        body, _, code_s = out.rpartition("|CODE|")
        try:
            code = int(code_s.strip())
        except ValueError:
            code = 0
        tried.append("%s → http %s / curl %s" % (prefix or "直连", code or "-", rc))
        if code == 200 and body.strip().startswith("{"):
            try:
                data = json.loads(body)
            except ValueError:
                continue
            assets = [{"name": a.get("name"), "size": a.get("size"),
                       "url": a.get("browser_download_url")} for a in data.get("assets", [])]
            assets.sort(key=lambda a: -(a.get("size") or 0))
            return {"ok": True, "repo": repo, "tag": data.get("tag_name") or tag,
                    "title": data.get("name"), "assets": assets, "via": prefix or "直连"}
    return {"ok": False, "error": "各个入口都取不到 release 列表，直接把某个文件的下载链接贴进来更稳",
            "tried": tried}


def list_dirs(path, want_files=False):
    if not path:
        drives = [{"name": "本地磁盘 (%s:)" % d, "path": d + ":\\"}
                  for d in "CDEFGHIJKLMNOPQRSTUVWXYZ" if os.path.exists(d + ":\\")]
        return {"path": "", "parent": None, "items": drives, "roots": True}
    path = expand_path(path)
    skip = {"windows", "system volume information", "programdata", "$recycle.bin", "recovery"}
    items, files = [], []
    try:
        for name in sorted(os.listdir(path), key=str.lower):
            if name.startswith(".") or name.lower() in skip:
                continue
            full = os.path.join(path, name)
            try:
                if os.path.isdir(full):
                    items.append({"name": name, "path": full})
                elif want_files and os.path.isfile(full):
                    files.append({"name": name, "path": full,
                                  "size": os.path.getsize(full)})
            except OSError:
                continue
        parent = os.path.dirname(path)
        return {"path": path, "parent": parent if parent and parent != path else None,
                "items": items, "files": files[:400]}
    except OSError as e:
        return {"path": path, "parent": None, "items": [], "files": [], "error": "打不开：%s" % e}


def make_dir(parent, name):
    if not (parent or "").strip():
        raise ValueError("先进入一个盘符或目录，再新建文件夹")
    parent = expand_path(parent)
    name = (name or "").strip()
    if not parent or not os.path.isdir(parent):
        raise ValueError("上级目录不存在：%s" % parent)
    if not name or name in (".", "..") or re.search(r'[\\/:*?"<>|]', name):
        raise ValueError("文件夹名不能为空，也不能包含 \\ / : * ? \" < > |")
    p = os.path.join(parent, name)
    os.makedirs(p, exist_ok=False)
    return p


def _dialog_script(kind="dir", initial="", filter_kind=""):
    """生成弹原生选择框的 PowerShell 脚本。单独拆出来是为了能在自测里做语法检查，
    不用真的把框弹到用户脸上。"""
    initial = (initial or "").strip().strip('"')
    if initial and os.path.isfile(initial):
        initial = os.path.dirname(initial)
    if initial and not os.path.isdir(initial):
        initial = ""
    esc = initial.replace("'", "''")
    common = [
        "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8",
        "$ErrorActionPreference='Stop'",
        "Add-Type -AssemblyName System.Windows.Forms",
        "Add-Type -AssemblyName System.Drawing",
        "$owner = New-Object System.Windows.Forms.Form",
        "$owner.Text = 'ghdl'; $owner.ShowInTaskbar = $false",
        "$owner.FormBorderStyle = 'None'; $owner.Size = New-Object System.Drawing.Size(1,1)",
        "$owner.StartPosition = 'CenterScreen'; $owner.TopMost = $true; $owner.Show(); $owner.Activate()",
    ]
    if kind == "files":
        filters = {
            "image": "图片|*.jpg;*.jpeg;*.png;*.webp;*.avif;*.bmp;*.gif;*.tif;*.tiff;*.ico;*.heic;*.heif|所有文件|*.*",
            "doc": "文档|*.doc;*.docx;*.odt;*.rtf;*.txt;*.md;*.html;*.xls;*.xlsx;*.ods;*.csv;*.ppt;*.pptx;*.odp;*.pdf|所有文件|*.*",
            "media": "音视频|*.mp4;*.mkv;*.mov;*.avi;*.webm;*.mp3;*.wav;*.flac;*.m4a;*.ogg;*.gif|所有文件|*.*",
        }.get(filter_kind, "所有文件|*.*")
        tail = [
            "$d = New-Object System.Windows.Forms.OpenFileDialog",
            "$d.Multiselect = $true; $d.CheckFileExists = $true",
            "$d.Title = '选择要转换的文件'",
            "$d.Filter = '%s'" % filters,
            ("if ('%s' -ne '') { $d.InitialDirectory = '%s' }" % (esc, esc)) if esc else "if ($false) {}",
            "if ($d.ShowDialog($owner) -eq [System.Windows.Forms.DialogResult]::OK) {",
            "  $d.FileNames | ForEach-Object { Write-Output $_ }",
            "}",
            "$owner.Close()",
        ]
    else:
        tail = [
            "$d = New-Object System.Windows.Forms.FolderBrowserDialog",
            "$d.Description = '选择目录'; $d.ShowNewFolderButton = $true",
            ("if ('%s' -ne '') { $d.SelectedPath = '%s' }" % (esc, esc)) if esc else "if ($false) {}",
            "if ($d.ShowDialog($owner) -eq [System.Windows.Forms.DialogResult]::OK) {",
            "  Write-Output $d.SelectedPath",
            "}",
            "$owner.Close()",
        ]
    return "\r\n".join(common + tail) + "\r\n"


def pick_paths(kind="dir", initial="", filter_kind=""):
    """
    弹 Windows 原生选择框（文件夹 / 多选文件）。服务本身是个无窗口进程，
    所以这里用 PowerShell + WinForms 起一个交给系统，比网页里一层层点目录树省事。
    选完把绝对路径一行一个写回；取消就是空。
    """
    if not _WIN:
        return {"ok": False, "error": "原生选择框只支持 Windows", "fallback": True}
    if not DIALOG_LOCK.acquire(blocking=False):
        return {"ok": False, "error": "已经有一个选择框开着，先在那边选完", "fallback": True}
    tmp = None
    try:
        script = _dialog_script(kind, initial, filter_kind)
        fd, tmp = tempfile.mkstemp(suffix=".ps1")
        os.close(fd)
        with open(tmp, "w", encoding="utf-8-sig", newline="\r\n") as f:
            f.write(script)
        ps = shutil.which("powershell.exe") or "powershell.exe"
        p = subprocess.run([ps, "-NoProfile", "-STA", "-ExecutionPolicy", "Bypass", "-File", tmp],
                           capture_output=True, timeout=300,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        out = decode_out(p.stdout or b"")
        err = decode_out(p.stderr or b"")
        paths = [ln.strip() for ln in out.splitlines() if ln.strip()]
        paths = [x for x in paths if os.path.exists(x)]
        if p.returncode != 0 and not paths:
            return {"ok": False, "error": "选择框起不来：%s" % (err.strip()[-240:] or p.returncode),
                    "fallback": True}
        return {"ok": True, "paths": paths}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "等了 5 分钟没选，已放弃", "fallback": False}
    except Exception as e:
        return {"ok": False, "error": "选择框失败：%s: %s" % (type(e).__name__, e), "fallback": True}
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        DIALOG_LOCK.release()


# =========================================================== HTTP 接口

CTYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
          ".js": "application/javascript; charset=utf-8", ".svg": "image/svg+xml",
          ".ico": "image/x-icon", ".png": "image/png",
          ".txt": "text/plain; charset=utf-8",
          ".md": "text/plain; charset=utf-8", ".map": "application/json; charset=utf-8"}


class Handler(BaseHTTPRequestHandler):
    server_version = "ghdl"      # 版本号统一从 APP_VERSION 走，别在这里再写一遍

    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        self.route("GET")

    def do_POST(self):
        self.route("POST")

    def send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def send_file(self, path, code=200):
        try:
            with open(path, "rb") as f:
                body = f.read()
        except OSError:
            return self.send_json({"ok": False, "error": "缺文件：%s" % os.path.basename(path)}, 404)
        self.send_response(code)
        self.send_header("Content-Type", CTYPES.get(os.path.splitext(path)[1], "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def read_body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}

    def route(self, method):
        u = urlparse(self.path)
        path = u.path
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if path in ("/", "/index.html"):
                return self.send_file(os.path.join(STATIC, "index.html"))
            if path == "/favicon.ico":
                fp = os.path.join(STATIC, "logo.ico")
                if os.path.exists(fp):
                    return self.send_file(fp)
                return self.send_file(os.path.join(STATIC, "index.html"))
            if path.startswith("/static/"):
                rel = os.path.normpath(path[len("/static/"):]).lstrip("\\/")
                fp = os.path.join(STATIC, rel)
                if not os.path.abspath(fp).startswith(os.path.abspath(STATIC)):
                    return self.send_json({"ok": False, "error": "路径越界"}, 403)
                return self.send_file(fp)
            if not path.startswith("/api/"):
                return self.send_json({"ok": False, "error": "没有这个页面：%s" % path}, 404)
            body = self.read_body() if method == "POST" else {}
            return self.api(path, q, body)
        except Exception as e:
            log(traceback.format_exc())
            return self.send_json({"ok": False, "error": "%s: %s" % (type(e).__name__, e)}, 500)

    def api(self, path, q, body):
        if path == "/api/state":
            cfg = get_config()
            engines = CONV.detect_engines(cfg, DATA) if CONV else {"error": CONV_ERR}
            with LOCK:
                tasks = [task_public(t) for t in sorted(TASKS.values(), key=lambda x: -(x.get("started") or 0))[:30]]
            return self.send_json({"ok": True, "version": APP_VERSION, "config": cfg,
                                   "proxies": get_proxies(), "tasks": tasks,
                                   "history": get_history()[:80], "engines": engines,
                                   "conv_error": CONV_ERR,
                                   "up": int(time.time() - STARTED_AT)})

        if path == "/api/download":
            raw = body.get("urls")
            if raw is None:
                raw = [body.get("url") or ""]
            if isinstance(raw, str):
                raw = [raw]
            urls, seen = [], set()
            for chunk in raw:
                for u in re.split(r"[\r\n]+", str(chunk or "")):
                    u = u.strip().strip('"').strip("'")
                    if u and u not in seen:
                        seen.add(u)
                        urls.append(u)
            if not urls:
                return self.send_json({"ok": False, "error": "链接还没填"})
            if len(urls) > 50:
                return self.send_json({"ok": False, "error": "一次最多 50 个链接，分两批吧"})
            destdir = body.get("destdir") or ""
            mode = body.get("mode") or "auto"
            headers = body.get("headers") or {}
            filename = (body.get("filename") or "").strip() if len(urls) == 1 else ""
            force_html = bool(body.get("force_html"))
            tasks, errs = [], []
            for u in urls:
                try:
                    t = start_task(u, destdir, mode, filename, headers, force_html)
                    tasks.append(t)
                except (OSError, ValueError) as e:
                    errs.append("%s → %s" % (u[:60], e))
            if not tasks:
                return self.send_json({"ok": False, "error": "一个都没启动：%s" % "；".join(errs[:3])})
            out = {"ok": True, "tasks": [task_public(t) for t in tasks]}
            out["task"] = out["tasks"][0]          # 兼容旧前端/旧脚本
            if errs:
                out["warnings"] = errs
            return self.send_json(out)

        if path == "/api/retry":
            try:
                t = retry_task(body.get("id"))
            except (OSError, ValueError) as e:
                return self.send_json({"ok": False, "error": str(e)})
            return self.send_json({"ok": True, "task": task_public(t)})

        if path == "/api/task":
            with LOCK:
                t = TASKS.get(q.get("id"))
            if not t:
                return self.send_json({"ok": False, "error": "任务不在了（服务重启过？）"})
            return self.send_json({"ok": True, "task": task_public(t)})

        if path == "/api/cancel":
            with LOCK:
                t = TASKS.get(body.get("id"))
                if t:
                    t["cancel"] = True
            return self.send_json({"ok": bool(t)})

        if path == "/api/forget":
            with LOCK:
                t = TASKS.pop(body.get("id"), None)
            if t:
                for junk in ((t.get("dest") or "") + ".part", (t.get("dest") or "") + ".part.hdr"):
                    try:
                        if junk and os.path.exists(junk):
                            os.unlink(junk)
                    except OSError:
                        pass
            return self.send_json({"ok": bool(t)})

        if path == "/api/probe":
            return self.send_json({"ok": True, "results": probe_all(), "proxies": get_proxies()})

        if path == "/api/proxies":
            act = body.get("action")
            lst = get_proxies()
            if act == "add":
                prefix = norm_prefix(body.get("prefix"))
                if not prefix:
                    return self.send_json({"ok": False, "error": "前缀至少填个域名，例如 https://my-proxy.example/"})
                if any(p.get("prefix") == prefix for p in lst):
                    return self.send_json({"ok": False, "error": "这个前缀已经在列表里"})
                host = urlparse(prefix).hostname or prefix
                lst.append({"id": uuid.uuid4().hex[:6], "name": (body.get("name") or host).strip(),
                            "prefix": prefix, "enabled": True, "order": len(lst) + 1,
                            "last_ms": None, "last_ok": None, "tried": 0, "ok": 0, "last_at": None})
            elif act == "del":
                lst = [p for p in lst if p.get("id") != body.get("id")]
            elif act == "enable":
                for p in lst:
                    if p.get("id") == body.get("id"):
                        p["enabled"] = bool(body.get("enabled"))
            elif act == "rename":
                for p in lst:
                    if p.get("id") == body.get("id"):
                        p["name"] = (body.get("name") or p["name"]).strip()
            elif act == "move":
                ids = [p.get("id") for p in lst]
                i = ids.index(body.get("id")) if body.get("id") in ids else None
                if i is None:
                    return self.send_json({"ok": False, "error": "找不到这个代理"})
                j = max(0, min(len(lst) - 1, i + int(body.get("delta") or 0)))
                lst.insert(j, lst.pop(i))
            else:
                return self.send_json({"ok": False, "error": "未知的代理操作：%s" % act})
            save_proxies(lst)
            return self.send_json({"ok": True, "proxies": get_proxies()})

        if path == "/api/config":
            global SEM
            cfg = get_config()
            restart = False
            if "default_dir" in body:
                raw = str(body.get("default_dir") or "").strip()
                cfg["default_dir"] = expand_path(raw) if raw else default_downloads_dir()
            if "last_dir" in body:
                raw = str(body.get("last_dir") or "").strip()
                cfg["last_dir"] = expand_path(raw) if raw else ""
            if "quick_dirs" in body:
                dirs = []
                for d in body.get("quick_dirs") or []:
                    if not str(d or "").strip():
                        continue
                    d = expand_path(d)
                    if d and d not in dirs:
                        dirs.append(d)
                cfg["quick_dirs"] = dirs[:8]
            if "recent_dirs" in body:
                dirs = []
                for d in body.get("recent_dirs") or []:
                    if not str(d or "").strip():
                        continue
                    d = expand_path(d)
                    if d and d not in dirs:
                        dirs.append(d)
                cfg["recent_dirs"] = dirs[:8]
            if "max_concurrent" in body:
                try:
                    n = max(1, min(8, int(body.get("max_concurrent") or 3)))
                except (TypeError, ValueError):
                    n = 3
                cfg["max_concurrent"] = n
                with LOCK:
                    busy = any(t.get("state") in BUSY_STATES for t in TASKS.values())
                if not busy:
                    SEM = threading.BoundedSemaphore(n)
                else:
                    restart = True          # 有任务在跑时不换闸门，下次启动生效
            if "proxy_url" in body:
                cfg["proxy_url"] = str(body.get("proxy_url") or "").strip()[:300]
            if "external_proxy" in body:
                cfg["external_proxy"] = bool(body.get("external_proxy"))
            if "user_agent" in body:
                cfg["user_agent"] = str(body.get("user_agent") or "").strip()[:300]
            if "engines" in body and isinstance(body.get("engines"), dict):
                eng = dict(cfg.get("engines") or {})
                for k, v in body["engines"].items():
                    if k not in ("libreoffice", "ffmpeg", "magick", "pandoc"):
                        continue
                    raw = str(v or "").strip()
                    v = expand_path(raw) if raw else ""
                    if v:
                        eng[k] = v
                    else:
                        eng.pop(k, None)
                cfg["engines"] = eng
                if CONV:
                    CONV.detect_engines(cfg, DATA, force=True)
            if "verify" in body:
                cfg["verify"] = bool(body["verify"])
            write_json(CONFIG_F, cfg)
            return self.send_json({"ok": True, "config": cfg, "restart_required": restart})

        if path == "/api/assets":
            return self.send_json(release_assets(q.get("url") or body.get("url") or ""))

        if path == "/api/ls":
            return self.send_json(list_dirs(q.get("path") or body.get("path") or "",
                                             bool(q.get("files")) or bool(body.get("files"))))

        if path == "/api/mkdir":
            try:
                p = make_dir(body.get("parent") or "", body.get("name") or "")
            except FileExistsError:
                return self.send_json({"ok": False, "error": "这个文件夹已经存在"})
            except (OSError, ValueError) as e:
                return self.send_json({"ok": False, "error": str(e)})
            return self.send_json({"ok": True, "path": p})

        if path == "/api/pickdir":
            return self.send_json(pick_paths("dir", q.get("initial") or body.get("initial") or ""))

        if path == "/api/pickfiles":
            return self.send_json(pick_paths("files", q.get("initial") or body.get("initial") or "",
                                              q.get("kind") or body.get("kind") or ""))

        if path == "/api/formats":
            if CONV is None:
                return self.send_json({"ok": False, "error": "转换模块没加载：%s" % CONV_ERR})
            paths = body.get("paths") or q.get("paths") or []
            if isinstance(paths, str):
                paths = [x for x in paths.splitlines() if x.strip()]
            return self.send_json(CONV.targets_for(paths, get_config(), DATA))

        if path == "/api/convert":
            if CONV is None:
                return self.send_json({"ok": False, "error": "转换模块没加载：%s" % CONV_ERR})
            paths = body.get("paths") or []
            if isinstance(paths, str):
                paths = [x for x in paths.splitlines() if x.strip()]
            try:
                t = start_convert_task(paths, body.get("target") or {},
                                       body.get("outdir") or "", body.get("options") or {})
            except (OSError, ValueError) as e:
                return self.send_json({"ok": False, "error": str(e)})
            return self.send_json({"ok": True, "task": task_public(t)})

        if path == "/api/engineinstall":
            try:
                t = start_install_task(body.get("id") or "")
            except (OSError, ValueError) as e:
                return self.send_json({"ok": False, "error": str(e)})
            return self.send_json({"ok": True, "task": task_public(t)})

        if path == "/api/engine/install":
            return self.api("/api/engineinstall", q, body)

        if path == "/api/verify":
            try:
                t = start_verify_task(body.get("path") or "", body.get("url") or "")
            except (OSError, ValueError) as e:
                return self.send_json({"ok": False, "error": str(e)})
            return self.send_json({"ok": True, "task": task_public(t)})

        if path == "/api/shutdown":
            with LOCK:
                busy = [t for t in TASKS.values() if t.get("state") in BUSY_STATES]
            if busy and not body.get("force"):
                return self.send_json({"ok": False, "busy": len(busy),
                                       "error": "还有 %d 个任务在跑。真要停就再按一次「强制停止」，"
                                                "已下的部分会留在 .part 文件里" % len(busy)})
            log("收到界面发来的停止请求（有 %d 个任务在跑）" % len(busy))
            self.send_json({"ok": True})
            threading.Timer(0.5, hard_exit).start()
            return

        if path == "/api/open":
            raw = (q.get("path") or "").strip()
            if not raw:
                return self.send_json({"ok": False, "error": "没给路径"})
            p = expand_path(raw)
            if not os.path.exists(p):
                return self.send_json({"ok": False, "error": "这个文件已经不在原来的位置了"})
            try:
                if os.path.isdir(p):
                    os.startfile(p)
                else:
                    subprocess.Popen(["explorer.exe", "/select," + p],
                                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            except OSError as e:
                return self.send_json({"ok": False, "error": str(e)})
            return self.send_json({"ok": True})

        return self.send_json({"ok": False, "error": "没有这个接口：%s" % path}, 404)


def find_port(base, tries=8):
    for p in range(base, base + tries):
        s = None
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.bind(("127.0.0.1", p))
            return p
        except OSError:
            continue
        finally:
            if s:
                try:
                    s.close()
                except OSError:
                    pass
    return base


def main():
    global SERVER, SEM
    if "--version" in sys.argv:
        say("ghdl %s（打包版=%s）" % (APP_VERSION, FROZEN))
        return
    if "--stop" in sys.argv:
        # 给 .bat / 命令行用的停止入口：逻辑放这儿，别在批处理里拼 JSON 转义（那种写法很容易出错）
        port = int(os.environ.get("GHDL_PORT") or get_config().get("port") or 8765)
        try:
            rq = urllib.request.Request("http://127.0.0.1:%d/api/shutdown" % port,
                                        data=b'{"force": true}',
                                        headers={"Content-Type": "application/json"})
            say("已通知 %d 端口的服务退出：%s" % (port, urllib.request.urlopen(rq, timeout=10).read().decode("utf-8")))
        except Exception as e:
            say("没连上服务（也许本来就停着）：%s" % e)
        return
    ensure_data()
    cfg = get_config()
    try:
        SEM = threading.BoundedSemaphore(max(1, min(8, int(cfg.get("max_concurrent") or 3))))
    except (TypeError, ValueError):
        SEM = threading.BoundedSemaphore(3)
    base_port = int(os.environ.get("GHDL_PORT") or cfg.get("port") or 8765)
    port = find_port(base_port)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    SERVER = httpd
    url = "http://127.0.0.1:%d/" % port
    log("ghdl 启动，监听 %s（pid %d，打包版=%s）" % (url, os.getpid(), FROZEN))
    if "--no-browser" not in sys.argv:
        threading.Timer(0.7, lambda: webbrowser.open(url)).start()
    if FROZEN:
        say("  ghdl 就绪 → %s   要停止请在页面右上角按「停止服务」" % url)
    else:
        say("  ghdl 就绪 → %s   关掉这个窗口就是停止（页面里也能按停止）" % url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log("收到 Ctrl+C，退出")


if __name__ == "__main__":
    main()
