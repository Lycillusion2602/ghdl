#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ghdl 格式转换引擎适配层。

设计原则（和下载内核一样，能少依赖就少依赖）：
  1. 轻量、宽松许可的库直接内嵌：Pillow（图片）、pypdf（PDF 页操作）、
     pypdfium2（PDF 渲染）、pillow-heif（HEIC/HEIF，BSD-3）。
  2. 重引擎只以「独立进程」调用，不链进 exe：LibreOffice（Office ↔ PDF）、
     ffmpeg（音视频）。这样 MIT 仓库不被 GPL/MPL 污染，用户也不用装 Python 包。
  3. ImageMagick / Pandoc 这类可选引擎放在 data/engines/ 里，缺了由
     server.py 的 /api/engine/install 按需下载，检测在这里做。

对外只暴露四个函数：
  detect_engines(cfg, data_dir)        探测引擎，返回给界面的能力表
  targets_for(paths, cfg, data_dir)    选中文件后，能转成哪些格式
  convert_batch(...)                   真正执行，含多文件合并/拆分
  engine_help(engine_id)               缺引擎时给一句人话

所有转换都遵守：绝不覆盖已有文件；输出落在用户指定的目录；支持取消。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from urllib.parse import quote

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# ---------------------------------------------------------------- 可选依赖
PILLOW = False
HEIF = False
PYPDF = False
PDFIUM = False
_IMPORT_ERR = {}

try:
    from PIL import Image, ImageOps, ImageSequence
    # Pillow 的插件是懒加载的：不 init 的话 Image.SAVE 里只有刚注册进来的
    # HEIF，JPEG/PNG/WEBP 全会误判成"不支持"，界面上就少一半目标格式。
    Image.init()
    PILLOW = True
    try:
        import pillow_heif
        pillow_heif.register_heif_opener()
        HEIF = True
    except Exception as e:                      # pragma: no cover - 取决于安装
        _IMPORT_ERR["heif"] = str(e)
except Exception as e:                          # pragma: no cover
    _IMPORT_ERR["pillow"] = str(e)

try:
    from pypdf import PdfReader, PdfWriter
    PYPDF = True
except Exception as e:                          # pragma: no cover
    _IMPORT_ERR["pypdf"] = str(e)

try:
    import pypdfium2 as pdfium
    PDFIUM = True
except Exception as e:                          # pragma: no cover
    _IMPORT_ERR["pypdfium2"] = str(e)


# ---------------------------------------------------------------- 扩展名分类
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".avif", ".bmp", ".gif", ".tif",
              ".tiff", ".ico", ".heic", ".heif", ".jfif", ".ppm", ".pgm", ".pbm",
              ".psd", ".svg", ".jxl", ".tga", ".dds"}
OFFICE_DOC = {".doc", ".docx", ".odt", ".rtf", ".txt", ".md", ".markdown",
              ".html", ".htm", ".tex", ".rst", ".org", ".epub"}
OFFICE_SHEET = {".xls", ".xlsx", ".ods", ".csv", ".tsv"}
OFFICE_SLIDE = {".ppt", ".pptx", ".odp"}
OFFICE_EXTS = OFFICE_DOC | OFFICE_SHEET | OFFICE_SLIDE
PDF_EXTS = {".pdf"}
VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".flv", ".wmv", ".m4v",
              ".ts", ".mpg", ".mpeg", ".3gp"}
AUDIO_EXTS = {".mp3", ".wav", ".flac", ".aac", ".m4a", ".ogg", ".opus", ".wma",
              ".aiff", ".amr"}
MARKUP_EXTS = {".md", ".markdown", ".html", ".htm", ".tex", ".rst", ".org",
               ".docx", ".odt", ".epub", ".rtf"}

# Pillow 能写的格式：扩展名 → format 名
PIL_FORMATS = {
    ".jpg": "JPEG", ".jpeg": "JPEG", ".png": "PNG", ".webp": "WEBP",
    ".avif": "AVIF", ".bmp": "BMP", ".gif": "GIF", ".tif": "TIFF",
    ".tiff": "TIFF", ".ico": "ICO", ".heic": "HEIF", ".heif": "HEIF",
    ".jxl": "JXL", ".ppm": "PPM", ".tga": "TGA", ".dds": "DDS",
}

# LibreOffice 的 --convert-to 过滤器。不写过滤器也能按扩展名走，
# 但 docx/xlsx/pptx 明确写出来更稳（尤其机器上同时装了 WPS 时）。
LO_FILTERS = {
    ".pdf": "pdf",
    # 过滤器名里不要带双引号：我们用 subprocess 以列表传参，引号不会被 shell 吃掉，
    # LibreOffice 会把 "MS Word 2007 XML" 连同引号当成过滤器名，然后报 0x81a。
    ".docx": "docx:MS Word 2007 XML",
    ".doc": "doc:MS Word 97",
    ".odt": "odt",
    ".rtf": "rtf",
    ".txt": "txt",
    ".html": "html",
    ".xlsx": "xlsx:Calc MS Excel 2007 XML",
    ".xls": "xls:MS Excel 97",
    ".ods": "ods",
    ".csv": "csv:Text - txt - csv (StarCalc):44,34,76",
    ".pptx": "pptx:Impress MS PowerPoint 2007 XML",
    ".odp": "odp",
    ".png": "png",
    ".jpg": "jpg",
}

LO_TARGETS = {
    "doc": [("pdf", "PDF"), ("docx", "Word (.docx)"), ("odt", "ODT"), ("rtf", "RTF"),
            ("txt", "纯文本"), ("html", "HTML")],
    "sheet": [("pdf", "PDF"), ("xlsx", "Excel (.xlsx)"), ("ods", "ODS"), ("csv", "CSV"),
              ("html", "HTML")],
    "slide": [("pdf", "PDF"), ("pptx", "PowerPoint (.pptx)"), ("odp", "ODP")],
}

IMAGE_TARGET_LABELS = [
    ("png", "PNG（无损，透明）"), ("ico", "ICO（Windows 图标，多尺寸）"),
    ("jpg", "JPG（有损，通用）"), ("webp", "WebP（体积小）"),
    ("avif", "AVIF（更小，较新）"), ("heic", "HEIC（苹果设备）"),
    ("tiff", "TIFF（印刷）"), ("bmp", "BMP（无压缩）"),
    ("gif", "GIF（动图）"), ("pdf", "PDF（图片装进 PDF）"),
]

VIDEO_TARGETS = [("mp4", "MP4（H.264）"), ("mkv", "MKV"), ("webm", "WebM（VP9）"),
                 ("gif", "GIF 动图"), ("mp3", "只抽音频 MP3"), ("m4a", "只抽音频 M4A")]
AUDIO_TARGETS = [("mp3", "MP3"), ("wav", "WAV"), ("m4a", "M4A（AAC）"),
                 ("flac", "FLAC"), ("ogg", "OGG")]

ENGINE_NAMES = {
    "pillow": "Pillow（内置图片引擎）",
    "pdfium": "PDFium（内置 PDF 渲染）",
    "pypdf": "pypdf（内置 PDF 操作）",
    "libreoffice": "LibreOffice",
    "ffmpeg": "FFmpeg",
    "magick": "ImageMagick",
    "pandoc": "Pandoc",
}

ENGINE_HELP = {
    "pillow": "图片转换需要 Pillow。源码运行请 pip install -r requirements.txt；发布版应已内置，看到这句说明打包漏了 Pillow。",
    "pdfium": "PDF 渲染需要 pypdfium2。源码运行请 pip install -r requirements.txt；发布版应已内置。",
    "pypdf": "PDF 操作需要 pypdf。源码运行请 pip install -r requirements.txt；发布版应已内置。",
    "libreoffice": "Office 文档互转需要 LibreOffice。装一个（免费）即可，程序会自动找到；也可以把便携版放到 data/engines/LibreOffice/。",
    "ffmpeg": "音视频转换需要 ffmpeg.exe。可以放进 PATH，或放到 data/engines/ffmpeg/。",
    "magick": "这个格式需要 ImageMagick。点下方「下载便携版」即可，约 30 MB。",
    "pandoc": "Markdown/EPUB 等标记文档互转需要 Pandoc。点下方「下载便携版」即可，约 35 MB。",
}

_WIN = os.name == "nt"


# ---------------------------------------------------------------- 基础小工具
def _norm_ext(p):
    return os.path.splitext(str(p or ""))[1].lower()


def _stem(p):
    return os.path.splitext(os.path.basename(str(p or "")))[0]


def _unique(path):
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    i = 2
    while os.path.exists("%s (%d)%s" % (base, i, ext)):
        i += 1
    return "%s (%d)%s" % (base, i, ext)


def _decode(b):
    for enc in ("utf-8", "gbk"):
        try:
            return b.decode(enc)
        except UnicodeDecodeError:
            continue
    return b.decode("utf-8", "replace")


def _reap(proc):
    try:
        proc.terminate()
        try:
            proc.wait(timeout=4)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=4)
            except subprocess.TimeoutExpired:
                pass
    except OSError:
        pass


def _run(cmd, timeout=300, cancel=None, cwd=None, env=None):
    """跑一个转换命令。输出落临时文件而不是管道，避免 ffmpeg/soffice 写满管道把进程堵死。"""
    fd, logf = tempfile.mkstemp(prefix="ghdl_run_", suffix=".log")
    os.close(fd)
    proc = None
    try:
        with open(logf, "wb") as f:
            try:
                proc = subprocess.Popen(cmd, stdout=f, stderr=subprocess.STDOUT,
                                        cwd=cwd, env=env, creationflags=CREATE_NO_WINDOW)
            except FileNotFoundError:
                return -1, "找不到命令：%s" % cmd[0]
            except OSError as e:
                return -1, "启动失败：%s" % e
            with _RUN_LOCK:
                _RUNNING.add(proc)
            started = time.time()
            while proc.poll() is None:
                if cancel and cancel():
                    _reap(proc)
                    return -9, "已取消"
                if time.time() - started > timeout:
                    _reap(proc)
                    return -2, "超过 %d 秒，已停下" % timeout
                time.sleep(0.15)
        try:
            with open(logf, "rb") as f:
                data = f.read()
        except OSError:
            data = b""
        return proc.returncode, _decode(data)[-4000:]
    finally:
        if proc is not None:
            with _RUN_LOCK:
                _RUNNING.discard(proc)
        try:
            os.unlink(logf)
        except OSError:
            pass


def kill_all():
    """服务要退出时把还在跑的 ffmpeg/soffice 收掉，别留孤儿进程。"""
    with _RUN_LOCK:
        procs = list(_RUNNING)
    for p in procs:
        _reap(p)


def _tail(text, n=300):
    text = (text or "").strip()
    return text[-n:] if len(text) > n else text


# ---------------------------------------------------------------- 引擎探测
_CACHE = {"at": 0.0, "key": None, "val": None}
_CACHE_LOCK = threading.Lock()
_RUNNING = set()
_RUN_LOCK = threading.Lock()


def _find_in_engines(data_dir, names, max_depth=4):
    root = os.path.join(data_dir or "", "engines")
    if not data_dir or not os.path.isdir(root):
        return None
    root = os.path.abspath(root)
    for dirpath, dirnames, filenames in os.walk(root):
        depth = dirpath[len(root):].count(os.sep)
        if depth >= max_depth:
            dirnames[:] = []
        lowered = {fn.lower(): fn for fn in filenames}
        for want in names:
            if want.lower() in lowered:
                return os.path.join(dirpath, lowered[want.lower()])
    return None


def _find_soffice(cfg, data_dir):
    eng = (cfg or {}).get("engines") or {}
    cands = [eng.get("libreoffice"), shutil.which("soffice")]
    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"),
                 os.environ.get("LOCALAPPDATA")):
        if not base:
            continue
        cands.append(os.path.join(base, "LibreOffice", "program", "soffice.exe"))
        cands.append(os.path.join(base, "Programs", "LibreOffice", "program", "soffice.exe"))
    cands.append(_find_in_engines(data_dir, ("soffice.exe", "soffice")))
    for c in cands:
        if c and os.path.isfile(c):
            return os.path.abspath(c)
    return None


def _which_or_cfg(cfg, key, exe_names, data_dir, engine_sub=()):
    eng = (cfg or {}).get("engines") or {}
    cands = [eng.get(key)]
    for n in exe_names:
        cands.append(shutil.which(n))
    if data_dir:
        cands.append(_find_in_engines(data_dir, tuple(exe_names),
                                      max_depth=4))
    for c in cands:
        if c and os.path.isfile(c):
            return os.path.abspath(c)
    return None


def _version_of(exe, args=("--version",), timeout=6):
    """只给 ffmpeg/magick/pandoc 用。LibreOffice 的 --version 会挂住，别调它。"""
    try:
        p = subprocess.run([exe] + list(args), capture_output=True, timeout=timeout,
                           creationflags=CREATE_NO_WINDOW)
        text = _decode((p.stdout or b"") + (p.stderr or b"")).strip()
        return (text.splitlines() or [""])[0].strip()[:120]
    except Exception:
        return ""


def detect_engines(cfg=None, data_dir=None, force=False):
    """返回引擎能力表。前端每次轮询都会问，所以结果缓存 30 秒；装完新引擎要 force。"""
    cfg = cfg or {}
    eng_cfg = json.dumps(cfg.get("engines") or {}, ensure_ascii=False, sort_keys=True)
    key = (eng_cfg, os.path.abspath(data_dir) if data_dir else "")
    now = time.time()
    with _CACHE_LOCK:
        if not force and _CACHE["val"] is not None and _CACHE["key"] == key and now - _CACHE["at"] < 30:
            return _CACHE["val"]

    so = _find_soffice(cfg, data_dir)
    ff = _which_or_cfg(cfg, "ffmpeg", ("ffmpeg.exe", "ffmpeg"), data_dir)
    mg = _which_or_cfg(cfg, "magick", ("magick.exe", "magick"), data_dir)
    pd = _which_or_cfg(cfg, "pandoc", ("pandoc.exe", "pandoc"), data_dir)

    out = {
        "pillow": {"ok": PILLOW, "path": None, "kind": "内置",
                   "detail": ("含 HEIC/AVIF" if HEIF else "HEIC 需装 pillow-heif")
                   if PILLOW else _IMPORT_ERR.get("pillow", "未安装")},
        "pdfium": {"ok": PDFIUM, "path": None, "kind": "内置",
                   "detail": "PDF 渲染成图片" if PDFIUM else _IMPORT_ERR.get("pypdfium2", "未安装")},
        "pypdf": {"ok": PYPDF, "path": None, "kind": "内置",
                  "detail": "PDF 合并/拆分/旋转/提取" if PYPDF else _IMPORT_ERR.get("pypdf", "未安装")},
        "libreoffice": {"ok": bool(so), "path": so, "kind": "外部",
                        "detail": "Office 文档互转" if so else "没找到 soffice.exe"},
        "ffmpeg": {"ok": bool(ff), "path": ff, "kind": "外部",
                   "detail": (_version_of(ff) or "音视频转换") if ff else "没找到 ffmpeg.exe"},
        "magick": {"ok": bool(mg), "path": mg, "kind": "可选",
                   "detail": (_version_of(mg, ("-version",)) or "ImageMagick") if mg else "没找到 magick.exe"},
        "pandoc": {"ok": bool(pd), "path": pd, "kind": "可选",
                   "detail": (_version_of(pd) or "Pandoc") if pd else "没找到 pandoc.exe"},
    }
    with _CACHE_LOCK:
        _CACHE.update(at=now, key=key, val=out)
    return out


# ---------------------------------------------------------------- 可转目标
def _eng_ok(engines, name):
    return bool((engines or {}).get(name, {}).get("ok"))


def _lo_family(ext):
    if ext in OFFICE_DOC:
        return "doc"
    if ext in OFFICE_SHEET:
        return "sheet"
    if ext in OFFICE_SLIDE:
        return "slide"
    return None


def _target(ext, label, engine, group, op="convert", options=None):
    d = {"ext": ext, "label": label, "engine": engine, "group": group, "op": op}
    if options:
        d["options"] = options
    return d


def _image_targets(engines, src_exts):
    """图片可转的目标。SVG/PSD 这类 Pillow 读不了的，只列 ImageMagick / LO 能做的。"""
    out = []
    need_magick = any(e in (".svg", ".psd", ".jxl", ".tga", ".dds") for e in src_exts)
    if _eng_ok(engines, "pillow") and not need_magick:
        for ext, label in IMAGE_TARGET_LABELS:
            if "." + ext in src_exts:
                continue                     # 同格式不重复列（PNG 转 PNG 没意义）
            fmt = PIL_FORMATS.get("." + ext)
            if ext == "pdf":
                out.append(_target("pdf", label, "Pillow", "图片", options=["dpi"]))
            elif ext == "ico":
                if fmt and fmt in Image.SAVE:
                    out.append(_target("ico", label, "Pillow", "图片", options=["ico_sizes"]))
            elif fmt and fmt in Image.SAVE:
                out.append(_target(ext, label, "Pillow", "图片", options=["quality"]))
    if _eng_ok(engines, "magick") or _eng_ok(engines, "libreoffice"):
        eng = "ImageMagick" if _eng_ok(engines, "magick") else "LibreOffice"
        fallback = (("png", "PNG"), ("ico", "ICO"), ("jpg", "JPG"),
                    ("webp", "WebP"), ("pdf", "PDF"))
        if not _eng_ok(engines, "magick"):
            fallback = (("png", "PNG"), ("jpg", "JPG"), ("pdf", "PDF"))   # LO 不写 WebP/ICO
        for ext, label in fallback:
            if "." + ext in src_exts:
                continue
            if not any(t["ext"] == ext for t in out):
                out.append(_target(ext, label, eng, "图片",
                                   options=["dpi"] if ext == "pdf" else
                                   (["ico_sizes"] if ext == "ico" else [])))
    return out


def _pdf_targets(engines, multi):
    out = []
    if _eng_ok(engines, "pypdf"):
        out.append(_target("pdf", "合并成一个 PDF" if multi else "无损重写 PDF",
                           "pypdf", "PDF", "merge" if multi else "rewrite"))
        if not multi:
            out.append(_target("pdf", "每页拆成单独 PDF", "pypdf", "PDF", "split"))
            out.append(_target("pdf", "旋转页面", "pypdf", "PDF", "rotate",
                               options=["rotate"]))
            out.append(_target("pdf", "提取指定页", "pypdf", "PDF", "extract",
                               options=["pages"]))
            out.append(_target("pdf", "加密（设密码）", "pypdf", "PDF", "encrypt",
                               options=["password"]))
            out.append(_target("pdf", "解密", "pypdf", "PDF", "decrypt",
                               options=["password"]))
    if _eng_ok(engines, "pdfium"):
        for ext, label in (("png", "每页转 PNG"), ("jpg", "每页转 JPG"), ("webp", "每页转 WebP")):
            out.append(_target(ext, label, "PDFium", "PDF", options=["dpi", "pages"]))
    if _eng_ok(engines, "pypdf"):
        out.append(_target("txt", "提取文字", "pypdf", "PDF", "text", options=["pages"]))
    # 把"无损重写"挪到最后：它是兜底操作，不该是下拉框默认第一项
    out.sort(key=lambda t: 1 if t.get("op") == "rewrite" else 0)
    return out


def _office_targets(engines, ext):
    if not _eng_ok(engines, "libreoffice"):
        return []
    fam = _lo_family(ext)
    out = []
    for t, label in LO_TARGETS.get(fam, []):
        if "." + t == ext:
            continue
        out.append(_target(t, label, "LibreOffice", "文档"))
    # Office → 图片：先 LO 转 PDF，再 PDFium 渲染；两个引擎都在才给这个选项
    if _eng_ok(engines, "pdfium"):
        for t, label in (("png", "每页转 PNG"), ("jpg", "每页转 JPG")):
            out.append(_target(t, label, "LibreOffice + PDFium", "文档",
                               options=["dpi"]))
    return out


def _media_targets(engines, ext):
    if not _eng_ok(engines, "ffmpeg"):
        return []
    base = AUDIO_TARGETS if ext in AUDIO_EXTS else VIDEO_TARGETS
    return [_target(t, label, "FFmpeg", "音视频")
            for t, label in base if "." + t != ext]


def _markup_targets(engines, ext):
    out = []
    if _eng_ok(engines, "pandoc"):
        for t, label in (("html", "HTML"), ("docx", "Word (.docx)"),
                         ("epub", "EPUB"), ("md", "Markdown"), ("rtf", "RTF"),
                         ("txt", "纯文本")):
            if "." + t != ext:
                out.append(_target(t, label, "Pandoc", "标记文档"))
        if "." + ext != ".pdf" and _eng_ok(engines, "libreoffice"):
            out.append(_target("pdf", "PDF（Pandoc + LibreOffice）", "Pandoc + LibreOffice", "标记文档"))
        return out
    # 没有 Pandoc 时，只有 HTML 能稳定地交给 LibreOffice；Markdown 别硬塞给 LO，
    # 它会把 # 和 * 原样当正文，转出来的 PDF 不能看。
    if ext in (".html", ".htm") and _eng_ok(engines, "libreoffice"):
        for t, label in (("pdf", "PDF"), ("docx", "Word (.docx)"), ("odt", "ODT"), ("txt", "纯文本")):
            out.append(_target(t, label, "LibreOffice", "文档"))
    return out


def targets_for(paths, cfg=None, data_dir=None, engines=None):
    """
    返回 {ok, category, targets[], notes[], engines{}}。
    多个文件时取交集：只有所有文件都能转到的目标才列出来，避免转一半失败。
    """
    if isinstance(paths, str):
        paths = [paths]
    paths = [os.path.abspath(p) for p in (paths or []) if str(p or "").strip()]
    if not paths:
        return {"ok": False, "error": "还没选文件"}
    missing = [p for p in paths if not os.path.isfile(p)]
    if missing:
        return {"ok": False, "error": "找不到文件：%s" % missing[0]}
    engines = engines or detect_engines(cfg, data_dir)
    exts = {_norm_ext(p) for p in paths}
    notes = []

    if exts <= IMAGE_EXTS:
        category = "图片"
        targets = _image_targets(engines, exts)
        if not targets:
            notes.append(ENGINE_HELP["magick"])
    elif exts == PDF_EXTS:
        category = "PDF"
        targets = _pdf_targets(engines, len(paths) > 1)
    elif exts <= {".md", ".markdown", ".html", ".htm", ".tex", ".rst", ".org", ".epub"}:
        # 标记文档要排在 Office 前面：.md 虽然也在 OFFICE_DOC 里，但 LibreOffice
        # 读 Markdown 很糟糕，交给 Pandoc 才对。
        category = "标记文档"
        targets = _markup_targets(engines, next(iter(exts)))
        if not targets:
            notes.append(ENGINE_HELP["pandoc"])
    elif exts <= OFFICE_EXTS:
        category = "文档"
        tset = None
        for e in exts:
            cur = {(t["op"], t["ext"]): t for t in _office_targets(engines, e)}
            tset = cur if tset is None else {k: v for k, v in tset.items() if k in cur}
        targets = list((tset or {}).values())
        if not targets:
            notes.append(ENGINE_HELP["libreoffice"])
    elif exts <= VIDEO_EXTS or exts <= AUDIO_EXTS:
        category = "音视频"
        targets = _media_targets(engines, next(iter(exts)))
        if not targets:
            notes.append(ENGINE_HELP["ffmpeg"])
    else:
        return {"ok": False, "category": "混合",
                "error": "一次只能选同一类的文件（现在混了：%s）" % "、".join(sorted(exts))}

    # 目标去重（保留先出现的），同一扩展名只留一个最合适的
    seen, uniq = set(), []
    for t in targets:
        k = (t["op"], t["ext"])
        if k in seen:
            continue
        seen.add(k)
        uniq.append(t)
    if not uniq and not notes:
        notes.append("这几个文件的类型暂时没有可用的转换目标。")
    return {"ok": True, "category": category, "targets": uniq, "notes": notes,
            "engines": engines, "count": len(paths)}


# ---------------------------------------------------------------- 页面范围
def parse_pages(spec, total):
    """'1-3,5' → [0,1,2,4]。空 = 全部。越界自动夹住，不报错。"""
    if not (spec or "").strip():
        return list(range(total))
    picked = []
    for part in re.split(r"[,，\s]+", spec.strip()):
        if not part:
            continue
        m = re.match(r"^(\d+)\s*-\s*(\d*)$", part)
        if m:
            a = int(m.group(1))
            b = int(m.group(2)) if m.group(2) else total
            a, b = max(a, 1), min(b, total)
            picked.extend(range(a - 1, b))
        elif part.isdigit():
            n = int(part)
            if 1 <= n <= total:
                picked.append(n - 1)
    return sorted(set(picked)) or list(range(total))


# ---------------------------------------------------------------- Pillow
def _flatten(im, bg="#ffffff"):
    """透明背景铺成指定颜色，否则存 JPG 会变成黑底。"""
    if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
        im = im.convert("RGBA")
        canvas = Image.new("RGB", im.size, bg)
        canvas.paste(im, mask=im.split()[-1])
        return canvas
    return im.convert("RGB")


def _save_pillow(im, dst, ext, opts, log):
    q = max(1, min(100, int(opts.get("quality") or 90)))
    fmt = PIL_FORMATS.get(ext)
    if not fmt:
        raise ValueError("Pillow 不知道怎么写成 .%s" % ext.lstrip("."))
    if ext in (".jpg", ".jpeg", ".bmp"):
        im = _flatten(im, opts.get("background") or "#ffffff")
        im.save(dst, fmt, quality=q, optimize=True)
    elif ext == ".png":
        if im.mode not in ("RGB", "RGBA", "L", "LA", "P"):
            im = im.convert("RGBA")
        im.save(dst, "PNG", optimize=True)
    elif ext == ".webp":
        if getattr(im, "is_animated", False):
            frames = [f.convert("RGBA") for f in ImageSequence.Iterator(im)]
            frames[0].save(dst, "WEBP", save_all=True, append_images=frames[1:],
                           duration=im.info.get("duration", 100),
                           loop=im.info.get("loop", 0), quality=q)
        else:
            im.save(dst, "WEBP", quality=q, method=6)
    elif ext in (".avif", ".heic", ".heif"):
        im = im.convert("RGB") if im.mode not in ("RGB", "RGBA") else im
        im.save(dst, fmt, quality=q)
    elif ext == ".gif":
        if getattr(im, "is_animated", False):
            frames = [f.convert("P", palette=Image.ADAPTIVE) for f in ImageSequence.Iterator(im)]
            frames[0].save(dst, "GIF", save_all=True, append_images=frames[1:],
                           duration=im.info.get("duration", 100),
                           loop=im.info.get("loop", 0), optimize=True)
        else:
            im.convert("P", palette=Image.ADAPTIVE).save(dst, "GIF", optimize=True)
    elif ext == ".ico":
        # ICO 要正方形：先铺成透明正方形，再让 Pillow 生成多尺寸。
        # 默认 16/32/48/64/128/256；前端也可以只选一个尺寸（opts["ico_sizes"]）。
        side = max(im.size)
        canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
        canvas.paste(im.convert("RGBA"), ((side - im.width) // 2, (side - im.height) // 2))
        raw = opts.get("ico_sizes")
        if isinstance(raw, (list, tuple)):
            parts = list(raw)
        else:
            parts = re.split(r"[,，\s]+", str(raw or ""))
        wanted = []
        for part in parts:
            part = str(part).strip()
            if part.isdigit():
                n = int(part)
                if 16 <= n <= 256:
                    wanted.append(n)
        if not wanted:
            wanted = [256, 128, 64, 48, 32, 16]
        cap = max(side, 16)
        # 不把 64px 的原图硬拉成 256px；要更大请先准备更大的源图
        sizes = [(n, n) for n in sorted({min(n, cap) for n in wanted}, reverse=True)]
        canvas.save(dst, "ICO", sizes=sizes)
    elif ext in (".tif", ".tiff"):
        im.save(dst, "TIFF", compression="tiff_lzw")
    else:
        im.save(dst, fmt)
    return dst


def _pillow_convert(src, ext, dst, opts, log):
    with Image.open(src) as im:
        im.load()
        animated = getattr(im, "is_animated", False)
        # 动图不能做 exif_transpose：它只处理当前帧，会把帧序列打散
        if not animated:
            im = ImageOps.exif_transpose(im)
        if ext == ".pdf":
            dpi = max(72, min(600, int(opts.get("dpi") or 150)))
            pages = [f.convert("RGB") for f in ImageSequence.Iterator(im)] if animated else [im.convert("RGB")]
            pages[0].save(dst, "PDF", save_all=True, append_images=pages[1:],
                          resolution=float(dpi))
            return dst
        return _save_pillow(im, dst, ext, opts, log)


def _images_to_pdf(paths, dst, opts, log):
    pages = []
    for p in paths:
        with Image.open(p) as im:
            im.load()
            im = ImageOps.exif_transpose(im)
            pages.append(im.convert("RGB"))
    dpi = max(72, min(600, int(opts.get("dpi") or 150)))
    pages[0].save(dst, "PDF", save_all=True, append_images=pages[1:], resolution=float(dpi))
    return dst


# ---------------------------------------------------------------- PDF
def _open_pdf_reader(src, log):
    r = PdfReader(src)
    if getattr(r, "is_encrypted", False):
        try:
            r.decrypt("")
            log("  PDF 有密码，按空密码试解成功")
        except Exception:
            raise ValueError("PDF 有密码，先在「PDF 操作 → 解密」里给密码")
    return r


def _pdf_render(src, ext, outdir, opts, log, cancel):
    pdf = pdfium.PdfDocument(src)
    n = len(pdf)
    pages = parse_pages(opts.get("pages"), n)
    dpi = max(36, min(600, int(opts.get("dpi") or 150)))
    outs = []
    for i in pages:
        if cancel and cancel():
            raise RuntimeError("已取消")
        page = pdf[i]
        bitmap = page.render(scale=dpi / 72.0)
        im = bitmap.to_pil()
        dst = _unique(os.path.join(outdir, "%s-p%02d%s" % (_stem(src), i + 1, ext)))
        _save_pillow(im, dst, ext, opts, log)
        outs.append(dst)
        log("  第 %d 页 → %s" % (i + 1, os.path.basename(dst)))
        try:
            bitmap.close()
            page.close()
        except Exception:
            pass
    if not outs:
        raise RuntimeError("没渲染出任何页面")
    return outs


def _pdf_merge(paths, outdir, opts, log):
    w = PdfWriter()
    for p in paths:
        r = _open_pdf_reader(p, log)
        for page in r.pages:
            w.add_page(page)
        log("  + %s（%d 页）" % (os.path.basename(p), len(r.pages)))
    dst = _unique(os.path.join(outdir, "%s-merged.pdf" % _stem(paths[0])))
    with open(dst, "wb") as f:
        w.write(f)
    return [dst]


def _pdf_split(src, outdir, opts, log, cancel):
    r = _open_pdf_reader(src, log)
    outs = []
    for i, page in enumerate(r.pages):
        if cancel and cancel():
            raise RuntimeError("已取消")
        w = PdfWriter()
        w.add_page(page)
        dst = _unique(os.path.join(outdir, "%s-p%02d.pdf" % (_stem(src), i + 1)))
        with open(dst, "wb") as f:
            w.write(f)
        outs.append(dst)
    return outs


def _pdf_rotate(src, outdir, opts, log):
    angle = int(opts.get("rotate") or 90)
    if angle not in (90, 180, 270):
        raise ValueError("旋转角度只能是 90 / 180 / 270")
    r = _open_pdf_reader(src, log)
    w = PdfWriter()
    for page in r.pages:
        page.rotate(angle)
        w.add_page(page)
    dst = _unique(os.path.join(outdir, "%s-rot%d.pdf" % (_stem(src), angle)))
    with open(dst, "wb") as f:
        w.write(f)
    return [dst]


def _pdf_extract(src, outdir, opts, log):
    r = _open_pdf_reader(src, log)
    pages = parse_pages(opts.get("pages"), len(r.pages))
    w = PdfWriter()
    for i in pages:
        w.add_page(r.pages[i])
    dst = _unique(os.path.join(outdir, "%s-提取.pdf" % _stem(src)))
    with open(dst, "wb") as f:
        w.write(f)
    return [dst]


def _pdf_text(src, outdir, opts, log, cancel):
    r = _open_pdf_reader(src, log)
    pages = parse_pages(opts.get("pages"), len(r.pages))
    chunks = []
    for i in pages:
        if cancel and cancel():
            raise RuntimeError("已取消")
        chunks.append("===== 第 %d 页 =====\n%s" % (i + 1, r.pages[i].extract_text() or ""))
    dst = _unique(os.path.join(outdir, "%s.txt" % _stem(src)))
    with open(dst, "w", encoding="utf-8") as f:
        f.write("\n\n".join(chunks))
    return [dst]


def _pdf_rewrite(src, outdir, opts, log):
    r = _open_pdf_reader(src, log)
    w = PdfWriter()
    for page in r.pages:
        w.add_page(page)
    dst = _unique(os.path.join(outdir, "%s-rewrite.pdf" % _stem(src)))
    with open(dst, "wb") as f:
        w.write(f)
    return [dst]


def _pdf_password(src, outdir, opts, log, encrypt=True):
    pwd = str(opts.get("password") or "")
    if encrypt and not pwd:
        raise ValueError("加密需要填一个密码")
    r = PdfReader(src)
    if getattr(r, "is_encrypted", False):
        # decrypt() 返回 0 表示密码不对；已经解开的文件再调一次也不会坏事
        try:
            if r.decrypt(pwd) == 0:
                raise ValueError("密码不对，解不开这个 PDF")
        except ValueError:
            raise
        except Exception as e:
            raise ValueError("解密失败：%s" % e)
    elif not encrypt:
        log("  这个 PDF 本来就没有密码")
    w = PdfWriter()
    for page in r.pages:
        w.add_page(page)
    if encrypt:
        w.encrypt(pwd)
    dst = _unique(os.path.join(outdir, "%s-%s.pdf" % (_stem(src), "locked" if encrypt else "unlocked")))
    with open(dst, "wb") as f:
        w.write(f)
    return [dst]


# ---------------------------------------------------------------- LibreOffice
def _lo_convert(src, ext, outdir, opts, log, cancel, engines):
    so = (engines.get("libreoffice") or {}).get("path")
    if not so:
        raise RuntimeError("没找到 LibreOffice")
    spec = LO_FILTERS.get(ext, ext.lstrip("."))
    tmp = tempfile.mkdtemp(prefix="ghdl_lo_out_")
    profile = tempfile.mkdtemp(prefix="ghdl_lo_profile_")
    try:
        prof_uri = "file:///" + quote(os.path.abspath(profile).replace("\\", "/"), safe="/:")
        cmd = [so, "--headless", "--norestore", "--nolockcheck", "--nodefault", "--nologo",
               "-env:UserInstallation=" + prof_uri,
               "--convert-to", spec, "--outdir", tmp, os.path.abspath(src)]
        log("  LibreOffice 正在转 .%s…" % ext.lstrip("."))
        rc, out = _run(cmd, timeout=int(opts.get("timeout") or 300), cancel=cancel)
        files = []
        for fn in sorted(os.listdir(tmp)):
            fp = os.path.join(tmp, fn)
            if os.path.isfile(fp) and os.path.getsize(fp) > 0:
                files.append(fp)
        if not files:
            raise RuntimeError("LibreOffice 没产出文件（退出码 %s）：%s" % (rc, _tail(out, 240)))
        outs = []
        multi = len(files) > 1
        for fp in files:
            want = os.path.splitext(fp)[1]
            name = "%s%s" % (_stem(src), want)
            if multi:
                name = "%s-%s%s" % (_stem(src), _stem(fp), want)
            dst = _unique(os.path.join(outdir, name))
            shutil.move(fp, dst)
            outs.append(dst)
        return outs
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(profile, ignore_errors=True)


def _office_to_images(src, ext, outdir, opts, log, cancel, engines):
    """Office → PDF（临时）→ 图片。"""
    tmp = tempfile.mkdtemp(prefix="ghdl_lo_pdf_")
    try:
        pdfs = _lo_convert(src, ".pdf", tmp, opts, log, cancel, engines)
        if not pdfs:
            raise RuntimeError("Office 转 PDF 失败")
        return _pdf_render(pdfs[0], ext, outdir, opts, log, cancel)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- FFmpeg
def _ffmpeg_convert(src, ext, dst, opts, log, cancel, engines):
    ff = (engines.get("ffmpeg") or {}).get("path")
    if not ff:
        raise RuntimeError("没找到 ffmpeg")
    base = [ff, "-y", "-nostdin", "-hide_banner", "-loglevel", "error", "-i", src]
    if ext == "gif":
        vf = ("fps=12,scale='min(720,iw)':-1:flags=lanczos,"
              "split[s0][s1];[s0]palettegen=max_colors=192[p];[s1][p]paletteuse=dither=bayer")
        cmd = base + ["-vf", vf, "-loop", "0", dst]
    elif ext == "mp4":
        cmd = base + ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                      "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", dst]
    elif ext == "webm":
        cmd = base + ["-c:v", "libvpx-vp9", "-crf", "32", "-b:v", "0",
                      "-c:a", "libopus", dst]
    elif ext == "mkv":
        cmd = base + ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                      "-c:a", "aac", dst]
    elif ext == "mp3":
        cmd = base + ["-vn", "-c:a", "libmp3lame", "-q:a", "2", dst]
    elif ext == "wav":
        cmd = base + ["-vn", "-c:a", "pcm_s16le", dst]
    elif ext == "m4a":
        cmd = base + ["-vn", "-c:a", "aac", "-b:a", "192k", dst]
    elif ext == "flac":
        cmd = base + ["-vn", "-c:a", "flac", dst]
    elif ext == "ogg":
        cmd = base + ["-vn", "-c:a", "libvorbis", "-q:a", "5", dst]
    else:
        cmd = base + [dst]
    log("  ffmpeg 正在转 .%s…" % ext)
    rc, out = _run(cmd, timeout=int(opts.get("timeout") or 3600), cancel=cancel)
    if not os.path.exists(dst) or os.path.getsize(dst) == 0:
        if "does not contain any stream" in out or "matches no streams" in out:
            raise RuntimeError("这个文件里没有对应轨道（比如视频没音轨，抽不出音频）")
        raise RuntimeError("ffmpeg 没产出文件（退出码 %s）：%s" % (rc, _tail(out, 300)))
    return dst


# ---------------------------------------------------------------- ImageMagick / Pandoc
def _magick_convert(src, ext, dst, opts, log, cancel, engines):
    mg = (engines.get("magick") or {}).get("path")
    if not mg:
        raise RuntimeError("没找到 ImageMagick")
    cmd = [mg, src]
    if _norm_ext(src) == ".svg":
        cmd = [mg, "-background", "none", "-density", "192", src]
    if ext in ("jpg", "jpeg", "webp", "avif", "heic"):
        cmd += ["-quality", str(max(1, min(100, int(opts.get("quality") or 90))))]
    cmd.append(dst)
    log("  ImageMagick 正在转 .%s…" % ext)
    rc, out = _run(cmd, timeout=int(opts.get("timeout") or 300), cancel=cancel)
    if not os.path.exists(dst) or os.path.getsize(dst) == 0:
        raise RuntimeError("ImageMagick 没产出文件（退出码 %s）：%s" % (rc, _tail(out, 300)))
    return dst


def _pandoc_convert(src, ext, dst, opts, log, cancel, engines):
    pd = (engines.get("pandoc") or {}).get("path")
    if not pd:
        raise RuntimeError("没找到 Pandoc")
    target = {"html": "html5", "docx": "docx", "epub": "epub3", "md": "gfm",
              "rtf": "rtf", "txt": "plain"}.get(ext, ext)
    cmd = [pd, src, "-o", dst, "-t", target]
    if ext in ("html", "epub"):
        cmd.append("--standalone")
    log("  Pandoc 正在转 .%s…" % ext)
    rc, out = _run(cmd, timeout=int(opts.get("timeout") or 300), cancel=cancel)
    if not os.path.exists(dst) or os.path.getsize(dst) == 0:
        raise RuntimeError("Pandoc 没产出文件（退出码 %s）：%s" % (rc, _tail(out, 300)))
    return dst


def _pandoc_to_pdf(src, outdir, opts, log, cancel, engines):
    """Pandoc 没有 LaTeX 就出不了 PDF，这里走 md/html → html → LibreOffice → PDF。"""
    tmp = tempfile.mkdtemp(prefix="ghdl_pd_")
    try:
        html = _pandoc_convert(src, "html", os.path.join(tmp, "doc.html"),
                               opts, log, cancel, engines)
        return _lo_convert(html, ".pdf", outdir, opts, log, cancel, engines)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- 单文件分发
def convert_one(src, target, outdir, opts, log, cancel, engines):
    """
    target 是 targets_for() 里的一项（或一个像 'png' 的字符串）。
    返回写出的文件路径列表。
    """
    if isinstance(target, str):
        target = {"ext": target.lstrip("."), "op": "convert"}
    ext = "." + str(target.get("ext") or "").lstrip(".").lower()
    op = target.get("op") or "convert"
    engine = target.get("engine") or ""
    opts = opts or {}
    outdir = os.path.abspath(outdir)
    os.makedirs(outdir, exist_ok=True)

    # PDF 的操作类目标
    if op in ("split", "rotate", "extract", "text", "rewrite", "encrypt", "decrypt"):
        funcs = {"split": _pdf_split, "rotate": _pdf_rotate, "extract": _pdf_extract,
                 "text": _pdf_text, "rewrite": _pdf_rewrite}
        if op in ("encrypt", "decrypt"):
            return _pdf_password(src, outdir, opts, log, encrypt=(op == "encrypt"))
        return funcs[op](src, outdir, opts, log, cancel)

    src_ext = _norm_ext(src)
    dst = _unique(os.path.join(outdir, "%s%s" % (_stem(src), ext)))

    # 图片来源：SVG/PSD 这类 Pillow 读不了的，交给 ImageMagick / LibreOffice
    if src_ext in IMAGE_EXTS:
        if src_ext == ".svg":
            if _eng_ok(engines, "magick"):
                return [_magick_convert(src, ext.lstrip("."), dst, opts, log, cancel, engines)]
            if _eng_ok(engines, "libreoffice"):
                return _lo_convert(src, ext, outdir, opts, log, cancel, engines)
            raise RuntimeError("SVG 需要 ImageMagick 或 LibreOffice，点「下载便携版」或装一个 LibreOffice")
        if src_ext in (".psd", ".jxl", ".tga", ".dds") and _eng_ok(engines, "magick"):
            return [_magick_convert(src, ext.lstrip("."), dst, opts, log, cancel, engines)]
        if PILLOW:
            return [_pillow_convert(src, ext, dst, opts, log)]
        if _eng_ok(engines, "magick"):
            return [_magick_convert(src, ext.lstrip("."), dst, opts, log, cancel, engines)]
        raise RuntimeError(ENGINE_HELP["pillow"])

    if src_ext in PDF_EXTS:
        if ext == ".txt":
            return _pdf_text(src, outdir, opts, log, cancel)
        if ext in (".png", ".jpg", ".jpeg", ".webp"):
            if not _eng_ok(engines, "pdfium"):
                raise RuntimeError("缺 PDFium，没法把 PDF 渲染成图片")
            return _pdf_render(src, ext, outdir, opts, log, cancel)
        if ext == ".docx" and _eng_ok(engines, "libreoffice"):
            return _lo_convert(src, ext, outdir, opts, log, cancel, engines)
        raise RuntimeError("PDF 转 .%s 暂时没有可用引擎" % ext.lstrip("."))

    if src_ext in OFFICE_EXTS:
        if not _eng_ok(engines, "libreoffice"):
            raise RuntimeError(ENGINE_HELP["libreoffice"])
        if ext in (".png", ".jpg", ".jpeg", ".webp") and _eng_ok(engines, "pdfium"):
            return _office_to_images(src, ext, outdir, opts, log, cancel, engines)
        return _lo_convert(src, ext, outdir, opts, log, cancel, engines)

    if src_ext in VIDEO_EXTS or src_ext in AUDIO_EXTS:
        if not _eng_ok(engines, "ffmpeg"):
            raise RuntimeError(ENGINE_HELP["ffmpeg"])
        return [_ffmpeg_convert(src, ext.lstrip("."), dst, opts, log, cancel, engines)]

    if src_ext in MARKUP_EXTS:
        if ext == ".pdf" and _eng_ok(engines, "pandoc") and _eng_ok(engines, "libreoffice"):
            return _pandoc_to_pdf(src, outdir, opts, log, cancel, engines)
        if _eng_ok(engines, "pandoc"):
            return [_pandoc_convert(src, ext.lstrip("."), dst, opts, log, cancel, engines)]
        if _eng_ok(engines, "libreoffice"):
            return _lo_convert(src, ext, outdir, opts, log, cancel, engines)
        raise RuntimeError(ENGINE_HELP["pandoc"])

    # 兜底：试试 LibreOffice
    if _eng_ok(engines, "libreoffice"):
        return _lo_convert(src, ext, outdir, opts, log, cancel, engines)
    raise RuntimeError("不认识这种文件类型：%s" % src_ext)


# ---------------------------------------------------------------- 批量入口
def convert_batch(paths, target, outdir, opts=None, log=None, cancel=None,
                  engines=None, cfg=None, data_dir=None):
    """
    统一入口。返回 {'outputs': [...], 'items': [{src, ok, outputs, error}], 'note': ''}。
    - 多张图片 → PDF：合成一个 PDF
    - 多个 PDF → merge：合成一个 PDF
    - 其余：逐个转换
    """
    log = log or (lambda _m: None)
    opts = opts or {}
    paths = [os.path.abspath(p) for p in (paths or [])]
    outdir = os.path.abspath(outdir)
    os.makedirs(outdir, exist_ok=True)
    engines = engines or detect_engines(cfg, data_dir)
    if not paths:
        raise ValueError("还没选文件")
    target = target if isinstance(target, dict) else {"ext": str(target), "op": "convert"}
    ext = "." + str(target.get("ext") or "").lstrip(".").lower()
    op = target.get("op") or "convert"

    # 多文件合并类
    if len(paths) > 1:
        if op == "merge" or (ext == ".pdf" and all(_norm_ext(p) in IMAGE_EXTS for p in paths)):
            if ext == ".pdf" and all(_norm_ext(p) in IMAGE_EXTS for p in paths):
                if not PILLOW:
                    raise RuntimeError("缺 Pillow，没法把图片合成 PDF")
                dst = _unique(os.path.join(outdir, "%s.pdf" % _stem(paths[0])))
                log("把 %d 张图片合成一个 PDF" % len(paths))
                _images_to_pdf(paths, dst, opts, log)
                return {"outputs": [dst], "note": "",
                        "items": [{"src": p, "ok": True, "outputs": [dst] if i == 0 else [], "error": ""}
                                  for i, p in enumerate(paths)]}
            if not _eng_ok(engines, "pypdf"):
                raise RuntimeError("缺 pypdf，没法合并 PDF")
            outs = _pdf_merge(paths, outdir, opts, log)
            return {"outputs": outs, "note": "",
                    "items": [{"src": p, "ok": True, "outputs": outs if i == 0 else [], "error": ""}
                              for i, p in enumerate(paths)]}

    outputs, items = [], []
    total = len(paths)
    for i, src in enumerate(paths):
        if cancel and cancel():
            items.append({"src": src, "ok": False, "outputs": [], "error": "已取消"})
            break
        log("[%d/%d] %s" % (i + 1, total, os.path.basename(src)))
        try:
            got = convert_one(src, target, outdir, opts, log, cancel, engines)
        except Exception as e:
            items.append({"src": src, "ok": False, "outputs": [],
                          "error": "%s: %s" % (type(e).__name__, e)})
            log("  失败：%s" % e)
            continue
        outputs.extend(got)
        items.append({"src": src, "ok": True, "outputs": got, "error": ""})
        log("  完成：%s" % "、".join(os.path.basename(x) for x in got))
    return {"outputs": outputs, "items": items, "note": ""}


def engine_help(engine_id):
    return ENGINE_HELP.get(engine_id, "缺一个转换引擎。")
