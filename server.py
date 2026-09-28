#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ghdl —— GitHub 代理下载工具（本地网页版）

启动：双击 ghdl.bat 选 1（或命令行 `py -3 server.py`）→ 自动打开 http://127.0.0.1:8765
只监听 127.0.0.1，不对外开放；不写注册表、不需要管理员权限、除 Python 标准库外无依赖。

文件分工（为什么这么写、改哪里，见 实现导读.md）：
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
# 不一致就直接失败不出包，避免"tag 说 1.2.0、界面里写 1.1.0"这种事。
APP_VERSION = "1.1.0"
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

DEFAULT_PROXIES = {
    "_说明": "prefix 后面直接拼完整的 https 地址；enabled=false 的不参与尝试；order 小的先试",
    "proxies": [
        {"id": "ghpro", "name": "gh-proxy.com", "prefix": "https://gh-proxy.com/",
         "enabled": True, "order": 1, "last_ms": None, "last_ok": None, "tried": 0, "ok": 0, "last_at": None},
        {"id": "ghfast", "name": "ghfast.top", "prefix": "https://ghfast.top/",
         "enabled": True, "order": 2, "last_ms": None, "last_ok": None, "tried": 0, "ok": 0, "last_at": None},
        {"id": "ghpnet", "name": "ghproxy.net", "prefix": "https://ghproxy.net/",
         "enabled": True, "order": 3, "last_ms": None, "last_ok": None, "tried": 0, "ok": 0, "last_at": None},
        {"id": "direct", "name": "直连 GitHub（兜底，通常很慢）", "prefix": "",
         "enabled": True, "order": 99, "last_ms": None, "last_ok": None, "tried": 0, "ok": 0, "last_at": None},
    ],
}

DEFAULT_CONFIG = {
    "port": 8765,
    "default_dir": "",            # 留空 = 自动用 ~/Downloads，见 default_downloads_dir()
    "quick_dirs": [],              # 常用目录快捷按钮；首次运行后你可以在界面里加自己的（存进 config.json）
    "speed_floor_bps": 2048,      # 连续 speed_floor_secs 秒低于这个速度 → 判定这个源卡死，换下一个
    "speed_floor_secs": 20,       # 实测：连上却一直不回数据的源（多是直连 raw.githubusercontent.com）靠这条才不至于干等
    "stall_secs": 30,             # 连续 30 秒文件字节没增长就判这个源卡死（TLS 握手 / CRL 查询卡住时 curl 自己的超时管不到）
    "attempt_cap_secs": 600,      # 单次尝试的绝对上限，防病态挂死
    "verify": True,
}

LOCK = threading.RLock()
TASKS: dict = {}
STARTED_AT = time.time()
SERVER = None                    # main() 里赋成 httpd，界面点「停止服务」时要它收尾
BUSY_STATES = ("queued", "parsing", "probing", "downloading", "verify", "need_asset")


def hard_exit():
    """给自己半秒把响应发回去，然后退出。exe 没有控制台窗口，界面里必须留这么个出口。"""
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
    """去掉查询串和 Windows 非法字符；太长就截断主体保留扩展名。"""
    name = (name or "").strip().strip('"')
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
    返回 plan：{ok, kind, url, filename, repo, tag, note}
    kind ∈ asset | raw | archive | repo_zip | clone | release_page | external | unknown
    """
    u = (raw or "").strip().strip('"').strip("'")
    if not u:
        return {"ok": False, "error": "链接是空的"}
    if not re.match(r"^https?://", u, re.I):
        if "github.com/" in u or "githubusercontent.com/" in u:
            u = "https://" + u.lstrip("/")
        else:
            return {"ok": False, "error": "这不像 http(s) 链接：%s" % u[:60]}
    u, stripped = strip_proxy_prefix(u)
    note = "粘贴的链接里已经带代理前缀，先剥掉了" if stripped else ""

    pr = urlparse(u)
    host = (pr.hostname or "").lower()
    segs = [s for s in pr.path.split("/") if s]
    plan = {"ok": True, "kind": "unknown", "url": u, "filename": None,
            "repo": None, "tag": None, "note": note}

    if host == "raw.githubusercontent.com":
        plan.update(kind="raw", filename=safe_filename(segs[-1] if segs else ""))
        return plan

    if host == "codeload.github.com":
        plan.update(kind="archive", filename=safe_filename(segs[-1] if segs else "", "archive.zip"))
        return plan

    if host in ("github.com", "www.github.com"):
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

    plan.update(kind="external",
                filename=safe_filename(os.path.basename(pr.path) or "download.bin"),
                note=(note + "；非 GitHub 链接，代理未必支持，先当普通下载处理").strip("；"))
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
                    "-o", DEVNULL, "-w", "%{http_code}|%{size_download}", target], timeout=20)
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


def pick_order():
    """启用中的代理排队：上次失败的排最后，其余按最近耗时升序，没测过的按手动顺序。"""
    lst = [p for p in get_proxies() if p.get("enabled")]

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
                            "-w", "|CODE|%{http_code}", url], timeout=20)
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
    m = re.search(r'filename\*?\s*=\s*"?([^";]+)', raw)
    if not m:
        return None
    name = m.group(1).strip().strip('"')
    if name.startswith("UTF-8''"):
        name = unquote(name[len("UTF-8''"):])
    name = safe_filename(name)
    return name if name != "download.bin" else None


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
    order = pick_order()
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
    try:
        if t.get("mode") == "verify":
            do_verify_task(t)
            return
        with LOCK:
            t["state"] = "parsing"
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
        os.makedirs(t["destdir"], exist_ok=True)
        with LOCK:
            t["plan"] = {k: plan.get(k) for k in ("kind", "url", "filename", "repo", "note")}
        if plan.get("filename"):
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
                    t["error"] = "所有启用中的源都没拿下这个文件，展开日志看每次失败的原因"
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
                         "attempts": len(t.get("attempts", [])), "bytes": t.get("downloaded"),
                         "secs": round(time.time() - t["started"], 1), "speed": t.get("speed"),
                         "sha256": None, "verify": "失败", "ok": False})
            return

        with LOCK:
            t["state"] = "verify"
        v = {}
        if plan["kind"] != "clone" and cfg_verify():
            v = run_checks(dest)
            if v.get("sha256"):
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
                     "proxy": t.get("proxy_used"), "attempts": len(t.get("attempts", [])),
                     "bytes": v.get("bytes"), "secs": round(secs, 1), "speed": t.get("speed"),
                     "sha256": v.get("sha256"), "verify": summarize_verify(v), "ok": True})
    except Exception as e:
        log(traceback.format_exc())
        with LOCK:
            t.update(state="failed", finished=time.time(), error="%s: %s" % (type(e).__name__, e))
        task_log(t, "异常：%s: %s" % (type(e).__name__, e))


def cfg_verify():
    return bool(get_config().get("verify", True))


def task_public(t):
    keep = ("id", "url", "mode", "destdir", "filename", "dest", "state", "proxy", "proxy_used",
            "downloaded", "total", "speed", "error", "log", "verify", "plan", "attempts", "secs")
    d = {k: t.get(k) for k in keep}
    d["elapsed"] = round((t.get("finished") or time.time()) - (t.get("started") or time.time()), 1)
    d["eta"] = None
    if d.get("state") == "downloading" and d.get("total") and d.get("speed"):
        d["eta"] = int(max(d["total"] - (d.get("downloaded") or 0), 0) / d["speed"])
    return d


def start_task(url, destdir, mode):
    cfg = get_config()
    target = expand_path(destdir or cfg["default_dir"])
    if not target:
        raise ValueError("下载目录是空的")
    os.makedirs(target, exist_ok=True)
    tid = uuid.uuid4().hex[:8]
    t = {"id": tid, "url": url, "mode": mode, "destdir": target, "state": "queued",
         "filename": "", "dest": "", "downloaded": 0, "total": None, "speed": 0.0,
         "attempts": [], "log": [], "verify": None, "error": None,
         "started": time.time(), "finished": None, "cancel": False}
    with LOCK:
        TASKS[tid] = t
    threading.Thread(target=run_task, args=(tid,), daemon=True).start()
    return t


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


# =========================================================== HTTP 接口

CTYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
          ".js": "application/javascript; charset=utf-8", ".svg": "image/svg+xml",
          ".ico": "image/x-icon", ".txt": "text/plain; charset=utf-8",
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
            if path in ("/", "/index.html", "/favicon.ico"):
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
            with LOCK:
                tasks = [task_public(t) for t in sorted(TASKS.values(), key=lambda x: -(x.get("started") or 0))[:30]]
            return self.send_json({"ok": True, "version": APP_VERSION, "config": get_config(),
                                   "proxies": get_proxies(), "tasks": tasks,
                                   "history": get_history()[:80],
                                   "up": int(time.time() - STARTED_AT)})

        if path == "/api/download":
            url = (body.get("url") or "").strip()
            if not url:
                return self.send_json({"ok": False, "error": "链接还没填"})
            try:
                t = start_task(url, body.get("destdir") or "", body.get("mode") or "auto")
            except (OSError, ValueError) as e:
                return self.send_json({"ok": False, "error": "目录不能用：%s" % e})
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
            cfg = get_config()
            if "default_dir" in body:
                cfg["default_dir"] = expand_path(body["default_dir"]) or default_downloads_dir()
            if "quick_dirs" in body:
                dirs = []
                for d in body.get("quick_dirs") or []:
                    d = expand_path(d)
                    if d and d not in dirs:
                        dirs.append(d)
                cfg["quick_dirs"] = dirs[:8]
            if "verify" in body:
                cfg["verify"] = bool(body["verify"])
            write_json(CONFIG_F, cfg)
            return self.send_json({"ok": True, "config": cfg})

        if path == "/api/assets":
            return self.send_json(release_assets(q.get("url") or body.get("url") or ""))

        if path == "/api/ls":
            return self.send_json(list_dirs(q.get("path") or body.get("path") or "",
                                             bool(q.get("files")) or bool(body.get("files"))))

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
    global SERVER
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
