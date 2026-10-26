#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SVN Manager — 多项目 Subversion 图形客户端
本地 Web 界面，零第三方依赖，直接调用系统 svn 命令行。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import webbrowser
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

APP = "SVN Manager"
CONF = os.path.expanduser("~/.svnmanager.json")
DEFAULT_PORT = 8765


IS_WIN = os.name == "nt"


def _pick_locale():
    if IS_WIN:
        return None  # Windows 上不设 LC_ALL，交给系统代码页
    try:
        out = subprocess.run(["locale", "-a"], capture_output=True, text=True).stdout
    except Exception:
        return "en_US.UTF-8"
    for c in ("zh_CN.UTF-8", "zh_CN.utf8", "en_US.UTF-8", "en_US.utf8"):
        if c in out:
            return c
    return "en_US.UTF-8"


SVN_ENV = dict(os.environ)
_loc = _pick_locale()
if _loc:
    SVN_ENV.update({"LC_ALL": _loc, "LANG": _loc})


def find_svn():
    """定位 svn 可执行文件。Windows 上 TortoiseSVN/SlikSVN 默认不在 PATH。"""
    for name in ("svn", "svn.exe"):
        exe = shutil.which(name)
        if exe:
            return exe
    if IS_WIN:
        for c in (
            r"C:\Program Files\TortoiseSVN\bin\svn.exe",
            r"C:\Program Files (x86)\TortoiseSVN\bin\svn.exe",
            r"C:\Program Files\SlikSvn\bin\svn.exe",
            r"C:\Program Files (x86)\SlikSvn\bin\SlikSvn\svn.exe",
            r"C:\Program Files (x86)\SlikSvn\bin\svn.exe",
            os.path.expandvars(r"%LOCALAPPDATA%\Programs\TortoiseSVN\bin\svn.exe"),
        ):
            if os.path.exists(c):
                return c
    return "svn"


SVN_BIN = find_svn()


def sibling_bin(name):
    """找同目录下的 svnversion 等配套命令"""
    d = os.path.dirname(SVN_BIN)
    fname = name + (".exe" if IS_WIN else "")
    if d:
        p = os.path.join(d, fname)
        if os.path.exists(p):
            return p
    return fname


def _dec(b):
    """svn 的文本输出：Windows 走系统 ANSI 代码页，类 Unix 是 UTF-8"""
    if isinstance(b, str):
        return b
    if IS_WIN:
        try:
            return b.decode("mbcs", "replace")
        except LookupError:
            return b.decode("gbk", "replace")
    return b.decode("utf-8", "replace")


CODE_LABEL = {
    "?": ("unversioned", "未纳管"),
    "A": ("added", "新增"),
    "C": ("conflicted", "冲突"),
    "D": ("deleted", "删除"),
    "I": ("ignored", "已忽略"),
    "M": ("modified", "修改"),
    "R": ("replaced", "替换"),
    "X": ("external", "外部"),
    "!": ("missing", "缺失"),
    "~": ("incomplete", "不完整"),
}
RANK = {"C": 0, "!": 1, "M": 2, "A": 3, "R": 4, "D": 5, "~": 6, "?": 7, "I": 8, "X": 9}

# svn --xml 里的 item 属性 → 单字符状态码
ITEM_CODE = {
    "added": "A", "conflicted": "C", "deleted": "D", "external": "X",
    "ignored": "I", "incomplete": "~", "missing": "!", "modified": "M",
    "replaced": "R", "unversioned": "?", "obstructed": "~", "merged": "M",
}


# ─────────────────────────── svn 调用 ───────────────────────────
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if IS_WIN else 0


def svn(args, cwd=None, timeout=180, raw=False):
    """调用 svn。raw=True 返回原始 bytes（XML 用），否则按平台编码解码成 str。"""
    try:
        p = subprocess.run(
            [SVN_BIN] + args, cwd=cwd, capture_output=True,
            env=SVN_ENV, timeout=timeout, creationflags=_NO_WINDOW,
        )
        if raw:
            return p.returncode, p.stdout, p.stderr
        return p.returncode, _dec(p.stdout), _dec(p.stderr)
    except subprocess.TimeoutExpired:
        return 124, "", "命令超时（%ds）" % timeout
    except FileNotFoundError:
        hint = ("请安装 TortoiseSVN（安装时勾选 command line client tools）或 SlikSVN"
                if IS_WIN else "请先安装 Subversion：brew install subversion")
        return 127, "", "找不到 svn 命令。" + hint
    except Exception as e:
        return 1, "", str(e)


# ─────────────────────────── 配置 ───────────────────────────
def load_conf():
    if os.path.exists(CONF):
        try:
            with open(CONF, encoding="utf-8") as f:
                d = json.load(f)
            d.setdefault("projects", [])
            return d
        except Exception:
            pass
    return {"projects": []}


def save_conf(d):
    tmp = CONF + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONF)


def find_project(name):
    for p in load_conf()["projects"]:
        if p["name"] == name:
            return p
    return None


def register_project(path, name=""):
    """注册一个工作副本，名字冲突自动加后缀。返回 (ok, name或错误信息)"""
    if not path or not os.path.isdir(path):
        return False, "目录不存在"
    if not os.path.isdir(os.path.join(path, ".svn")):
        return False, "这个目录不是 SVN 工作副本（没有 .svn）"
    path = os.path.realpath(path)
    d = load_conf()
    # 同一个路径已经注册过 → 直接返回，避免重复添加
    for x in d["projects"]:
        try:
            if os.path.realpath(x["path"]) == path:
                return True, x["name"]
        except OSError:
            continue
    if not name:
        name = os.path.basename(path.rstrip("/\\")) or "project"
    if any(x["name"] == name for x in d["projects"]):
        i = 2
        while any(x["name"] == "%s-%d" % (name, i) for x in d["projects"]):
            i += 1
        name = "%s-%d" % (name, i)
    d["projects"].append({"name": name, "path": path})
    save_conf(d)
    return True, name


# ─────────────────────────── 检出（checkout）任务 ───────────────────────────
CHECKOUT_TASKS = {}
CHECKOUT_PROCS = {}
TASK_LOCK = threading.Lock()

DEPTHS = ("infinity", "immediates", "files", "empty")

# svn 错误码 → 人话
ERR_HINTS = [
    ("E155037", "上次下载没做完就被中断了，文件夹被锁住。再点一次「开始检出」会自动清理并接着下。"),
    ("E155004", "文件夹被锁住了（可能上次操作没做完）。再点一次「开始检出」会自动清理。"),
    ("E170013", "连不上 SVN 服务器。检查一下网络，或者地址里的主机名有没有写错。"),
    ("E210005", "服务器上没有这个仓库。地址里的仓库名可能写错了。"),
    ("E170001", "账号或密码不对，也可能是这个账号没有这个项目的权限。"),
    ("E215004", "这个地址需要登录才能访问，请把用户名和密码填上。"),
    ("E175002", "服务器拒绝了请求，或者网络中途断了。"),
    ("E670002", "网络中途断开了，检查一下网络再试。"),
    ("E730054", "没有权限写到这个文件夹。换一个你有权限的位置，比如桌面或文稿。"),
    ("E000111", "磁盘空间不够了。清理一下再试。"),
    ("E200030", "文件被别的程序占用着，关掉正在用它的软件再试。"),
]


def diagnose(path, text):
    """把 svn 的原始报错翻译成普通人能看懂的一句话"""
    t = (text or "").lower()
    for code, hint in ERR_HINTS:
        if code.lower() in t:
            return hint
    if "locked" in t or "锁" in t:
        return "文件夹被锁住了，再点一次「开始检出」会自动清理。"
    if not t.strip():
        return "下载中途被中断了（可能是关掉了窗口或网络断了）。再点一次「开始检出」会接着下。"
    if os.path.isdir(os.path.join(path, ".svn")):
        return "目标文件夹里已经有上次没下完的内容。再点一次「开始检出」会接着下。"
    return ""


def _task_update(tid, **kw):
    with TASK_LOCK:
        t = CHECKOUT_TASKS.get(tid)
        if t:
            t.update(kw)


def get_task(tid):
    with TASK_LOCK:
        t = CHECKOUT_TASKS.get(tid)
        return dict(t) if t else None


def start_checkout(url, path, name="", username="", password="", depth="infinity", mode="checkout"):
    tid = uuid.uuid4().hex[:10]
    with TASK_LOCK:
        CHECKOUT_TASKS[tid] = {
            "id": tid, "status": "running", "url": url, "path": path,
            "name": name, "count": 0, "lines": [], "error": "", "hint": "",
            "mode": mode, "started": time.time(), "returncode": None, "project": "",
        }
    threading.Thread(
        target=_checkout_worker,
        args=(tid, url, path, name, username, password, depth, mode),
        daemon=True,
    ).start()
    return tid


def _checkout_worker(tid, url, path, name, username, password, depth, mode="checkout"):
    # 目标已经是工作副本（上次没下完）→ 先清锁，否则任何操作都会 E155037
    already_wc = os.path.isdir(os.path.join(path, ".svn"))
    if already_wc:
        _task_update(tid, lines=["正在清理上次没下完留下的锁…"])
        svn(["cleanup", "--non-interactive"], cwd=path, timeout=1800)

    if mode == "update":
        args = ["update", "--non-interactive", "--accept", "postpone", path]
    else:
        args = ["checkout", "--non-interactive", "--depth", depth, url, path]
    if username:
        args += ["--username", username]
    if password:
        args += ["--password", password]

    lines, count = [], 0
    try:
        proc = subprocess.Popen(
            [SVN_BIN] + args,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            env=SVN_ENV, creationflags=_NO_WINDOW,
        )
        with TASK_LOCK:
            CHECKOUT_PROCS[tid] = proc

        for raw in iter(proc.stdout.readline, b""):
            line = _dec(raw).rstrip()
            if not line:
                continue
            lines.append(line)
            if line[:1] in ("A", "D", "U", "C", "E"):
                count += 1
            if len(lines) > 300:
                del lines[:150]
            _task_update(tid, lines=list(lines), count=count)

        proc.wait()
        with TASK_LOCK:
            CHECKOUT_PROCS.pop(tid, None)

        if proc.returncode == 0:
            ok, res = register_project(path, name)
            if ok:
                _task_update(tid, status="done", project=res,
                             returncode=0, lines=lines[-40:])
            else:
                _task_update(tid, status="error", returncode=0,
                             error="下载完成了，但加入项目列表失败：%s" % res,
                             lines=lines[-40:])
        else:
            raw = "\n".join(lines[-40:])
            hint = diagnose(path, raw) or "下载没成功，具体原因见下面的原始信息。"
            # 即使失败/中断，只要已经下出了一份有效的工作副本，也要加进列表，
            # 否则用户下了半天，左侧却什么都没有
            registered = ""
            rc_i, u_i, _ = svn(["info", "--show-item", "url"], cwd=path, timeout=30)
            if rc_i == 0 and (u_i or "").strip():
                ok_i, res_i = register_project(path, name)
                if ok_i:
                    registered = res_i
            _task_update(tid, status="error", returncode=proc.returncode,
                         error="下载失败 —— " + hint,
                         hint=hint, raw=raw, lines=lines[-60:],
                         project=registered)
    except Exception as e:
        _task_update(tid, status="error",
                     error="下载过程中出了意外：%s" % e, lines=lines[-40:])


def safe_join(root, rel):
    """确保 rel 不逃出 root"""
    p = os.path.normpath(os.path.join(root, rel))
    if not (p == root or p.startswith(root + os.sep)):
        raise ValueError("路径越界: %s" % rel)
    return p


def reveal_path(target):
    """在文件管理器里定位到某个文件"""
    if IS_WIN:
        subprocess.Popen('explorer /select,"%s"' % target.replace("/", "\\"), shell=True)
    else:
        subprocess.Popen(["open", "-R", target])


def open_path(target):
    """用文件管理器打开某个目录"""
    if IS_WIN:
        os.startfile(target)
    else:
        subprocess.Popen(["open", target])


# ─────────────────────────── 核心操作 ───────────────────────────
def parse_status(root):
    # 用 --xml：编码由 XML 声明保证（Windows 上中文路径不会乱码），结构也比文本模式稳
    rc, out, err = svn(["status", "--xml"], cwd=root, timeout=300, raw=True)
    if rc not in (0, 1):
        return [], _dec(err).strip() or "svn status 失败"
    if not out or not out.strip():
        return [], None
    try:
        doc = ET.fromstring(out)
    except Exception as e:
        return [], "解析 svn 输出失败: %s" % e

    items = []
    for entry in doc.iter("entry"):
        rel = (entry.get("path") or "").replace("\\", "/")
        wc = entry.find("wc-status")
        if wc is None or not rel:
            continue
        item = wc.get("item") or "none"
        props = wc.get("props") or "none"
        # 1.14 里是 wc-status 的属性 tree-conflicted="true"；
        # 旧版本或某些构建可能用 <tree-conflict> 子元素，两种都认
        tree_c = ((wc.get("tree-conflicted") or "").strip() == "true"
                  or wc.find("tree-conflict") is not None)

        code = ITEM_CODE.get(item)
        if code is None:
            # item=normal/none 但属性有改动 → 仍算修改
            if item in ("normal", "none") and props not in ("normal", "none", None):
                code = "M"
            else:
                continue
        if tree_c:
            code = "C"

        kind, label = CODE_LABEL[code]
        full = os.path.join(root, rel.replace("/", os.sep))
        try:
            mtime = int(os.stat(full).st_mtime)
        except OSError:
            mtime = None  # 缺失/删除的条目没有本地文件
        items.append({
            "path": rel, "code": code, "kind": kind,
            "label": label, "tree": tree_c,
            "is_dir": os.path.isdir(full), "mtime": mtime,
        })
    # 冲突/缺失置顶（需要处理），其余按本地修改时间倒序
    items.sort(key=lambda x: (
        0 if x["code"] in ("C", "!") else 1,
        -(x["mtime"] or 0),
        x["path"],
    ))
    return items, None


def project_rev(root):
    """工作副本根的 svn info revision 可能是 0，用 svnversion 取真实版本"""
    try:
        p = subprocess.run([sibling_bin("svnversion"), root], capture_output=True,
                           env=SVN_ENV, timeout=30, creationflags=_NO_WINDOW)
        s = _dec(p.stdout).strip()
    except Exception:
        return "?"
    if not s or "Unversioned" in s or "exported" in s:
        return "?"
    tail = s.split(":")[-1]
    digits = "".join(ch for ch in tail if ch.isdigit())
    return digits or "?"


def _looks_locked(text):
    """判断这次失败是不是"工作副本被上次中断留下的锁挡住" """
    t = (text or "").lower()
    if "e155004" in t or "e155037" in t or "working copy locked" in t:
        return True
    if "工作副本" in t and "锁定" in t:
        return True
    return "locked" in t and "svn" in t


def _cleanup(root):
    return svn(["cleanup", "--non-interactive"], cwd=root, timeout=1800)


def do_update(root):
    rc, out, err = svn(["update", "--non-interactive", "--accept", "postpone"],
                       cwd=root, timeout=1800)
    joined = out + err
    if rc != 0 and _looks_locked(joined):
        _cleanup(root)
        rc, out, err = svn(["update", "--non-interactive", "--accept", "postpone"],
                           cwd=root, timeout=1800)
        joined = "（已自动清理上次中断留下的锁并重试）\n" + out + err
    return {"ok": rc == 0, "output": joined.strip()}


# ─────────────────── 提交：逐行进度 + 自动解堵 ───────────────────
_ACTIONS = [
    ("adding", "新增"), ("正在增加", "新增"),
    ("sending", "上传"), ("正在发送", "上传"),
    ("deleting", "删除"), ("正在删除", "删除"),
    ("replacing", "替换"), ("正在替换", "替换"),
    ("updating", "更新"), ("正在更新", "更新"),
    ("restoring", "恢复"), ("restored", "恢复"), ("正在恢复", "恢复"),
]


def _parse_action(line):
    """从 svn 输出行里认出「正在处理哪个文件」，用于进度"""
    low = line.lower()
    for key, label in _ACTIONS:
        if low.startswith(key) and len(low) > len(key) and low[len(key)] in " \t":
            return label, line[len(key):].strip()
    return None, None


def _stream_svn(args, cwd, timeout=1800, on_line=None):
    """跑 svn 并把每一行输出实时回调（stdout/stderr 合并）。返回 (rc, lines)"""
    try:
        p = subprocess.Popen(
            [SVN_BIN] + args, cwd=cwd, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, env=SVN_ENV,
            creationflags=_NO_WINDOW, bufsize=0,
        )
    except FileNotFoundError:
        return 127, ["找不到 svn 命令"]
    except Exception as e:
        return 1, [str(e)]
    lines = []
    try:
        for raw in iter(p.stdout.readline, b""):
            seg = _dec(raw).replace("\r", "\n")
            for part in seg.split("\n"):
                part = part.strip()
                if not part:
                    continue
                lines.append(part)
                if on_line:
                    try:
                        on_line(part)
                    except Exception:
                        pass
        p.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        p.kill()
        p.wait()
        lines.append("命令超时（%ds）" % timeout)
        return 124, lines
    return p.returncode, lines


def start_commit(root, paths, message, add_unversioned=True):
    """启动一个后台提交任务，返回任务 id（前端轮询拿进度）"""
    tid = uuid.uuid4().hex[:10]
    with TASK_LOCK:
        CHECKOUT_TASKS[tid] = {
            "id": tid, "kind": "commit", "status": "running",
            "total": len(paths), "done": 0, "lines": [], "sent": [],
            "error": "", "hint": "", "output": "", "project": "",
            "started": time.time(),
        }
    threading.Thread(target=_commit_worker,
                     args=(tid, root, list(paths), message, add_unversioned),
                     daemon=True).start()
    return tid


def _commit_worker(tid, root, paths, message, add_unversioned):
    def state(**kw):
        with TASK_LOCK:
            t = CHECKOUT_TASKS.get(tid)
            if t:
                t.update(kw)

    def log(s):
        with TASK_LOCK:
            t = CHECKOUT_TASKS.get(tid)
            if not t:
                return
            t["lines"].append(s)
            if len(t["lines"]) > 500:
                del t["lines"][:300]

    def bump(path=""):
        with TASK_LOCK:
            t = CHECKOUT_TASKS.get(tid)
            if t:
                t["done"] = t.get("done", 0) + 1

    def finish(ok, joined, note=""):
        state(status="done" if ok else "failed",
              error="" if ok else (note or "提交失败"),
              output=(joined or "").strip()[-6000:])

    try:
        # 0) 先解堵：上次被打断留下的写锁会让所有写操作报「工作副本被锁定」
        log("检查工作副本状态…")
        rc, out, err = _cleanup(root)
        log("工作副本就绪" if rc == 0 else "清理提示：" + (out + err).strip()[:200])

        # 1) 未纳管项先 add 进版本控制
        if add_unversioned:
            items, _ = parse_status(root)
            unver = {i["path"] for i in items if i["code"] == "?"}
            to_add = [p for p in paths if p in unver]
            if to_add:
                log("正在加入版本控制：%d 项" % len(to_add))
                fd, tf = tempfile.mkstemp(prefix="svnmg-", suffix=".txt")
                os.close(fd)
                try:
                    with open(tf, "w", encoding="utf-8") as f:
                        for p in to_add:
                            f.write(os.path.join(root, p) + "\n")
                    add_args = ["add", "--parents", "--non-interactive", "--targets", tf]
                    rc, lines = _stream_svn(add_args, cwd=root, timeout=900, on_line=log)
                    if rc != 0 and _looks_locked("\n".join(lines)):
                        log("遇到残留锁，自动清理后重试…")
                        _cleanup(root)
                        rc, lines = _stream_svn(add_args, cwd=root, timeout=900, on_line=log)
                    if rc != 0:
                        finish(False, "\n".join(lines), "添加失败（未上传）")
                        return
                finally:
                    try:
                        os.unlink(tf)
                    except OSError:
                        pass

        # 2) 真正提交
        abs_paths = [safe_join(root, p) for p in paths]
        fd, tf = tempfile.mkstemp(prefix="svnmg-", suffix=".txt")
        os.close(fd)
        try:
            with open(tf, "w", encoding="utf-8") as f:
                for p in abs_paths:
                    f.write(p + "\n")

            def on_line(s):
                log(s)
                act, path = _parse_action(s)
                if act:
                    bump()
                    with TASK_LOCK:
                        t = CHECKOUT_TASKS.get(tid)
                        if t and path:
                            t["sent"].append({"action": act, "path": path})

            log("开始上传：共 %d 项（大文件可能较慢，别关窗口）" % len(abs_paths))
            commit_args = ["commit", "-m", message, "--encoding", "UTF-8",
                           "--non-interactive", "--targets", tf]
            rc, lines = _stream_svn(commit_args, cwd=root, timeout=7200, on_line=on_line)
            joined = "\n".join(lines)
            if rc != 0 and _looks_locked(joined):
                log("工作副本被上次中断留下的锁挡住，自动清理后重试…")
                _cleanup(root)
                rc, lines = _stream_svn(commit_args, cwd=root, timeout=7200, on_line=on_line)
                joined = "（已自动清理残留锁并重试）\n" + "\n".join(lines)
            ok = rc == 0
            if ok:
                state(done=len(abs_paths))
            finish(ok, joined, "提交失败（服务器没收到）")
        finally:
            try:
                os.unlink(tf)
            except OSError:
                pass
    except Exception as e:
        finish(False, str(e), "提交出错")


def do_commit(root, paths, message, add_unversioned=True):
    if not paths:
        return {"ok": False, "output": "没有选中任何文件"}
    if not message.strip():
        return {"ok": False, "output": "提交信息不能为空"}
    abs_paths = []
    for rel in paths:
        abs_paths.append(safe_join(root, rel))

    to_add = []
    if add_unversioned:
        items, _ = parse_status(root)
        unver = {i["path"] for i in items if i["code"] == "?"}
        to_add = [p for p in paths if p in unver]

    fd, tf = tempfile.mkstemp(prefix="svnmg-", suffix=".txt")
    os.close(fd)
    try:
        if to_add:
            # 必须先落盘再调用，否则 svn 读到空文件
            with open(tf, "w", encoding="utf-8") as f:
                for p in to_add:
                    f.write(os.path.join(root, p) + "\n")
            rc, out, err = svn(["add", "--parents", "--non-interactive",
                                "--targets", tf], cwd=root, timeout=600)
            if rc != 0:
                return {"ok": False, "output": ("添加失败:\n" + (out + err)).strip()}
        with open(tf, "w", encoding="utf-8") as f:
            for p in abs_paths:
                f.write(p + "\n")
        rc, out, err = svn(["commit", "-m", message, "--encoding", "UTF-8",
                            "--non-interactive", "--targets", tf],
                           cwd=root, timeout=1800)
        return {"ok": rc == 0, "output": (out + err).strip()}
    finally:
        try:
            os.unlink(tf)
        except OSError:
            pass


def do_delete(root, paths):
    """删除：已纳管的走 svn delete（标记为 D，可提交），未纳管的直接删磁盘"""
    if not paths:
        return {"ok": False, "output": "没有选中任何文件"}
    items, _ = parse_status(root)
    unver = {i["path"] for i in items if i["code"] == "?"}

    abs_paths = []
    for rel in paths:
        try:
            abs_paths.append(safe_join(root, rel))
        except ValueError as e:
            return {"ok": False, "output": str(e)}

    to_svn, to_fs = [], []
    for rel, ap in zip(paths, abs_paths):
        (to_fs if rel in unver else to_svn).append(ap)

    msgs = []
    if to_svn:
        fd, tf = tempfile.mkstemp(prefix="svnmg-", suffix=".txt")
        os.close(fd)
        try:
            with open(tf, "w", encoding="utf-8") as f:
                for p in to_svn:
                    f.write(p + "\n")
            rc, out, err = svn(["delete", "--force", "--non-interactive",
                                "--targets", tf], cwd=root, timeout=300)
            msgs.append((out + err).strip())
            if rc != 0:
                return {"ok": False, "output": "\n".join(msgs)}
        finally:
            try:
                os.unlink(tf)
            except OSError:
                pass

    for ap in to_fs:
        try:
            if os.path.isdir(ap) and not os.path.islink(ap):
                shutil.rmtree(ap)
            elif os.path.exists(ap) or os.path.islink(ap):
                os.remove(ap)
            msgs.append("已删除（未纳管，直接移除文件）: " + os.path.relpath(ap, root))
        except Exception as e:
            msgs.append("删除失败 %s: %s" % (os.path.relpath(ap, root), e))

    return {"ok": True, "output": "\n".join(m for m in msgs if m)}


def do_log(root, limit=30):
    # 工作副本根的 BASE rev 可能为 0，必须显式指定范围才能查到历史
    # raw=True：XML 带 encoding 声明，必须交给 ET 解析 bytes
    rc, out, err = svn(["log", "-v", "-r", "HEAD:1", "-l", str(limit), "--xml"],
                       cwd=root, timeout=120, raw=True)
    if rc != 0 or b"<logentry" not in out:
        # 离线兜底：用本地缓存的历史
        rc, out, err = svn(["log", "-v", "-l", str(limit), "--xml"],
                           cwd=root, timeout=120, raw=True)
    if rc != 0:
        return []
    try:
        tree = ET.fromstring(out)
    except Exception:
        return []
    res = []
    for e in tree.findall("logentry"):
        paths = []
        for p in e.findall("paths/path"):
            paths.append({"action": p.get("action", ""), "path": (p.text or "")})
        res.append({
            "rev": e.get("revision", ""),
            "author": (e.findtext("author") or "").strip(),
            "date": (e.findtext("date") or "").strip(),
            "msg": (e.findtext("msg") or "").strip(),
            "paths": paths[:40],
            "path_total": len(paths),
        })
    return res


def do_info(root):
    rc, out, err = svn(["info"], cwd=root, timeout=60)
    if rc != 0:
        return {"error": (err or out).strip()}
    d = {}
    for line in out.splitlines():
        if ":" in line and not line.startswith(" "):
            k, v = line.split(":", 1)
            d[k.strip()] = v.strip()
    return d


# ─────────────────────────── HTTP ───────────────────────────
class Handler(BaseHTTPRequestHandler):
    server_version = "SVNManager/1.0"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        raw = body.encode("utf-8") if isinstance(body, str) else body
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            pass  # 浏览器预连接断开，忽略

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        p = u.path

        if p in ("/", "/index.html"):
            return self._send(200, HTML, "text/html; charset=utf-8")

        if p == "/api/projects":
            out = []
            for pr in load_conf()["projects"]:
                root = pr["path"]
                ok = os.path.isdir(os.path.join(root, ".svn"))
                n_change = 0
                if ok:
                    items, _ = parse_status(root)
                    n_change = len([i for i in items if i["code"] not in ("I", "X")])
                out.append({
                    "name": pr["name"], "path": root, "valid": ok,
                    "rev": project_rev(root) if ok else "?",
                    "changes": n_change,
                })
            return self._json({"projects": out})

        if p == "/api/status":
            pr = find_project((q.get("name") or [""])[0])
            if not pr:
                return self._json({"error": "项目不存在"}, 404)
            items, err = parse_status(pr["path"])
            if err:
                return self._json({"error": err, "items": []})
            return self._json({"items": items, "rev": project_rev(pr["path"])})

        if p == "/api/checkout/status":
            t = get_task((q.get("id") or [""])[0])
            if not t:
                return self._json({"error": "任务不存在"}, 404)
            return self._json(t)

        if p == "/api/commit/status":
            t = get_task((q.get("id") or [""])[0])
            if not t:
                return self._json({"error": "任务不存在"}, 404)
            return self._json(t)

        if p == "/api/log":
            pr = find_project((q.get("name") or [""])[0])
            if not pr:
                return self._json({"error": "项目不存在"}, 404)
            lim = int((q.get("limit") or ["30"])[0])
            return self._json({"entries": do_log(pr["path"], lim)})

        if p == "/api/info":
            pr = find_project((q.get("name") or [""])[0])
            if not pr:
                return self._json({"error": "项目不存在"}, 404)
            return self._json(do_info(pr["path"]))

        if p == "/api/diff":
            pr = find_project((q.get("name") or [""])[0])
            if not pr:
                return self._json({"error": "项目不存在"}, 404)
            rel = (q.get("path") or [""])[0]
            try:
                target = safe_join(pr["path"], rel)
            except ValueError as e:
                return self._json({"error": str(e)}, 400)
            rc, out, err = svn(["diff", target], cwd=pr["path"], timeout=120)
            txt = out if out else err
            if rc != 0 and "E155010" in err:
                txt = ("该文件/目录在本地已被删除或缺失（状态 D 或 !），"
                       "没有可比对的本地内容。\n\n" + err)
            if len(txt) > 300000:
                txt = txt[:300000] + "\n\n…（内容过长，已截断）"
            return self._json({"diff": txt, "ok": rc == 0})

        if p == "/api/fs":
            cur = (q.get("path") or [os.path.expanduser("~")])[0]
            cur = os.path.expanduser(cur)
            if not os.path.isdir(cur):
                return self._json({"error": "目录不存在"}, 400)
            try:
                names = sorted(os.listdir(cur))
            except PermissionError:
                return self._json({"error": "没有权限读取该目录"}, 403)
            dirs = []
            for n in names:
                if n.startswith("."):
                    continue
                full = os.path.join(cur, n)
                if os.path.isdir(full):
                    dirs.append({"name": n, "path": full,
                                 "is_wc": os.path.isdir(os.path.join(full, ".svn"))})
            parent = os.path.dirname(cur)
            return self._json({"path": cur, "parent": parent if parent != cur else None,
                               "is_wc": os.path.isdir(os.path.join(cur, ".svn")),
                               "dirs": dirs})

        return self._json({"error": "not found"}, 404)

    def do_POST(self):
        p = urlparse(self.path).path
        b = self._body()

        if p == "/api/project/add":
            path = os.path.expanduser((b.get("path") or "").strip())
            ok, res = register_project(path, (b.get("name") or "").strip())
            if not ok:
                return self._json({"ok": False, "error": res})
            return self._json({"ok": True, "name": res})

        if p == "/api/checkout":
            url = (b.get("url") or "").strip()
            path = os.path.expanduser((b.get("path") or "").strip())
            name = (b.get("name") or "").strip()
            depth = b.get("depth") or "infinity"
            if depth not in DEPTHS:
                depth = "infinity"
            if not url:
                return self._json({"ok": False, "error": "请填写 SVN 地址"})
            if not path:
                return self._json({"ok": False, "error": "请填写保存路径"})
            mode = "checkout"
            wc = os.path.isdir(os.path.join(path, ".svn"))
            if wc:
                # 已经是工作副本：同一项目就清理后接着下，别的项目就拒绝
                _, cur_url, _ = svn(["info", "--show-item", "url"], cwd=path, timeout=30)
                cur_url = (cur_url or "").strip()
                if cur_url and cur_url.rstrip("/") != url.rstrip("/"):
                    return self._json({"ok": False, "error":
                        "这个文件夹里已经是另一个项目的副本（%s）。\n请换个文件夹，或者先把那个副本挪走。" % cur_url})
                mode = "update"
            elif os.path.exists(path):
                if not os.path.isdir(path):
                    return self._json({"ok": False, "error": "这个路径已经是一个文件了，请换一个文件夹"})
                try:
                    if os.listdir(path):
                        return self._json({"ok": False, "error":
                            "文件夹里已经有别的东西了，请换一个空文件夹。"})
                except OSError as e:
                    return self._json({"ok": False, "error": "无法读取这个文件夹：%s" % e})
            else:
                parent = os.path.dirname(os.path.abspath(path))
                if not os.path.isdir(parent):
                    return self._json({"ok": False, "error": "上一级文件夹不存在：%s" % parent})
            tid = start_checkout(url, path, name,
                                 b.get("username") or "", b.get("password") or "",
                                 depth, mode)
            return self._json({"ok": True, "id": tid, "mode": mode})

        if p == "/api/checkout/cancel":
            tid = b.get("id") or ""
            with TASK_LOCK:
                proc = CHECKOUT_PROCS.get(tid)
            if proc and proc.poll() is None:
                proc.terminate()
                _task_update(tid, status="cancelled", error="已取消")
                return self._json({"ok": True})
            return self._json({"ok": False, "error": "任务不在运行中"})

        if p == "/api/project/remove":
            name = b.get("name")
            d = load_conf()
            before = len(d["projects"])
            d["projects"] = [x for x in d["projects"] if x["name"] != name]
            save_conf(d)
            return self._json({"ok": len(d["projects"]) < before})

        if p in ("/api/update", "/api/cleanup"):
            pr = find_project(b.get("name"))
            if not pr:
                return self._json({"ok": False, "output": "项目不存在"})
            if p == "/api/update":
                return self._json(do_update(pr["path"]))
            rc, out, err = svn(["cleanup", "--non-interactive"], cwd=pr["path"], timeout=600)
            return self._json({"ok": rc == 0, "output": (out + err).strip()})

        if p == "/api/delete":
            pr = find_project(b.get("name"))
            if not pr:
                return self._json({"ok": False, "output": "项目不存在"})
            return self._json(do_delete(pr["path"], b.get("paths") or []))

        if p == "/api/commit":
            pr = find_project(b.get("name"))
            if not pr:
                return self._json({"ok": False, "output": "项目不存在"})
            paths = b.get("paths") or []
            msg = b.get("message") or ""
            if not paths:
                return self._json({"ok": False, "output": "没有选中任何文件"})
            if not msg.strip():
                return self._json({"ok": False, "output": "提交信息不能为空"})
            tid = start_commit(pr["path"], paths, msg, b.get("add_unversioned", True))
            return self._json({"ok": True, "id": tid, "total": len(paths)})

        if p == "/api/reveal":
            pr = find_project(b.get("name"))
            if not pr:
                return self._json({"ok": False}, 404)
            try:
                target = safe_join(pr["path"], b.get("path") or "")
            except ValueError:
                return self._json({"ok": False}, 400)
            if os.path.exists(target):
                reveal_path(target)
            else:
                open_path(pr["path"])
            return self._json({"ok": True})

        if p == "/api/open":
            pr = find_project(b.get("name"))
            if pr:
                open_path(pr["path"])
            return self._json({"ok": True})

        return self._json({"error": "not found"}, 404)


HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>SVN Manager</title>
<style>
:root{
  --bg:#f5f5f7; --panel:#ffffff; --card:#ffffff; --line:#e3e3e8;
  --fg:#1d1d1f; --muted:#86868b; --accent:#0a84ff; --accent-soft:#e8f2ff;
  --green:#28a745; --red:#ff3b30; --orange:#ff9500; --purple:#af52de; --gray:#8e8e93;
  --hover:#f0f0f3;
}
@media(prefers-color-scheme:dark){
  :root{
    --bg:#1c1c1e; --panel:#242426; --card:#2c2c2e; --line:#3a3a3c;
    --fg:#f5f5f7; --muted:#98989d; --accent:#0a84ff; --accent-soft:#0d2a4a;
    --green:#32d74b; --red:#ff453a; --orange:#ff9f0a; --purple:#bf5af2; --gray:#8e8e93;
    --hover:#323234;
  }
}
*{box-sizing:border-box}
html,body{height:100%}
body{margin:0;font:13px/1.5 -apple-system,BlinkMacSystemFont,"SF Pro Text","PingFang SC","Microsoft YaHei","Segoe UI",sans-serif;
  background:var(--bg);color:var(--fg);overflow:hidden;-webkit-font-smoothing:antialiased}
button{font:inherit;cursor:pointer;border:none;border-radius:7px;padding:6px 12px;
  background:var(--card);color:var(--fg);border:1px solid var(--line);transition:.15s}
button:hover:not(:disabled){background:var(--hover)}
button:disabled{opacity:.4;cursor:not-allowed}
button.primary{background:var(--accent);color:#fff;border-color:transparent;font-weight:600}
button.primary:hover:not(:disabled){filter:brightness(1.1);background:var(--accent)}
button.danger{color:var(--red)}
input,textarea{font:inherit;background:var(--card);color:var(--fg);border:1px solid var(--line);
  border-radius:7px;padding:7px 10px;outline:none;width:100%}
input:focus,textarea:focus{border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft)}
.app{display:flex;height:100vh}

/* sidebar */
aside{width:270px;flex:none;background:var(--panel);border-right:1px solid var(--line);
  display:flex;flex-direction:column}
.brand{padding:14px 16px 10px;font-weight:700;font-size:14px;letter-spacing:.2px;
  display:flex;align-items:center;gap:8px}
.dot{width:9px;height:9px;border-radius:50%;background:var(--green);box-shadow:0 0 0 3px rgba(50,215,75,.18)}
.plist{flex:1;overflow-y:auto;padding:4px 8px 8px}
.pitem{padding:8px 10px;border-radius:8px;cursor:pointer;margin-bottom:2px;
  display:flex;align-items:center;gap:8px}
.pitem:hover{background:var(--hover)}
.pitem.on{background:var(--accent);color:#fff}
.pitem .nm{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-weight:500}
.pitem .cnt{font-size:11px;padding:1px 6px;border-radius:9px;background:var(--line);color:var(--muted);font-weight:600}
.pitem.on .cnt{background:rgba(255,255,255,.25);color:#fff}
.pitem.on .rv{color:rgba(255,255,255,.8)}
.pitem .rv{font-size:11px;color:var(--muted);font-variant-numeric:tabular-nums}
.side-foot{padding:8px;border-top:1px solid var(--line);display:flex;gap:6px}
.side-foot button{flex:1;font-size:12px}

/* main */
main{flex:1;display:flex;flex-direction:column;min-width:0}
.topbar{height:52px;flex:none;display:flex;align-items:center;gap:10px;padding:0 16px;
  border-bottom:1px solid var(--line);background:var(--panel)}
.ptitle{font-weight:600;font-size:14px}
.ppath{color:var(--muted);font-size:11px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:44vw}
.spacer{flex:1}
.chip{font-size:11px;padding:2px 8px;border-radius:20px;background:var(--accent-soft);color:var(--accent);font-weight:600}
.tabs{display:flex;gap:2px;padding:8px 16px 0;border-bottom:1px solid var(--line);background:var(--panel)}
.tab{padding:7px 14px;border-radius:7px 7px 0 0;cursor:pointer;color:var(--muted);font-weight:500;font-size:12px}
.tab.on{color:var(--fg);background:var(--bg);border:1px solid var(--line);border-bottom-color:var(--bg);margin-bottom:-1px}
.body{flex:1;overflow:auto;padding:12px 16px 28px}
.empty{color:var(--muted);text-align:center;padding:60px 20px;font-size:13px}

table{width:100%;border-collapse:collapse;background:var(--card);border-radius:10px;overflow:hidden;
  border:1px solid var(--line)}
th{text-align:left;font-size:11px;color:var(--muted);font-weight:600;padding:8px 12px;
  border-bottom:1px solid var(--line);background:var(--panel);position:sticky;top:0;z-index:1;
  white-space:nowrap}
td{padding:6px 12px;border-bottom:1px solid var(--line);vertical-align:middle}
tr:last-child td{border-bottom:none}
tr.row:hover{background:var(--hover)}
tr.row.sel{background:var(--accent-soft)}
td.cb{width:34px}
td.pth{font-family:"SF Mono",Menlo,Consolas,monospace;font-size:12px;word-break:break-all}
td.act{width:1%;white-space:nowrap;text-align:right}
td.tm{width:1%;white-space:nowrap;text-align:right;color:var(--muted);
  font-size:11px;font-variant-numeric:tabular-nums;padding-left:0}
td.act button{padding:2px 8px;font-size:11px;background:transparent;border-color:transparent;color:var(--accent)}
td.act button:hover{background:var(--accent-soft)}
td.act button.del{color:var(--red)}
td.act button.del:hover{background:rgba(255,59,48,.12)}
.badge{display:inline-block;min-width:22px;text-align:center;font-size:11px;font-weight:700;
  padding:1px 6px;border-radius:5px;font-family:"SF Mono",Menlo,Consolas,monospace}
.badge.lbl{font-family:inherit;font-weight:600;margin-left:6px;min-width:auto;white-space:nowrap}
.b-M{background:rgba(255,149,0,.16);color:var(--orange)}
.b-A{background:rgba(40,167,69,.16);color:var(--green)}
.b-D{background:rgba(255,59,48,.16);color:var(--red)}
.b-C{background:rgba(255,59,48,.2);color:var(--red)}
.b-\?{background:rgba(142,142,147,.18);color:var(--gray)}
.b-{background:rgba(142,142,147,.18);color:var(--gray)}
.b-{background:rgba(175,82,222,.16);color:var(--purple)}
.badge.lbl{font-family:inherit;font-weight:600;margin-left:6px;min-width:auto}
.dir{color:var(--muted);margin-right:3px}

/* commit bar */
.commitbar{flex:none;border-top:1px solid var(--line);background:var(--panel);padding:10px 16px;
  display:flex;gap:10px;align-items:flex-end}
.commitbar textarea{resize:none;height:52px;font-size:12px}
.commitbar .side{display:flex;flex-direction:column;gap:6px;align-items:stretch;min-width:210px}
.btnrow{display:flex;gap:6px}
.btnrow button{flex:1;white-space:nowrap}
.hint{font-size:11px;color:var(--muted)}
.rowtop{display:flex;align-items:center;gap:8px;margin-bottom:8px}
.selall{display:flex;align-items:center;gap:6px;font-size:12px;color:var(--muted);cursor:pointer;user-select:none}
input[type=checkbox]{width:15px;height:15px;accent-color:var(--accent);cursor:pointer;margin:0}

/* log */
.le{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 14px;margin-bottom:8px}
.le .h{display:flex;gap:10px;align-items:center;font-size:11px;color:var(--muted);margin-bottom:4px}
.le .r{font-family:"SF Mono",Menlo,Consolas,monospace;color:var(--accent);font-weight:700}
.le .m{white-space:pre-wrap;font-size:12.5px}
.le .ps{margin-top:6px;font-family:"SF Mono",Menlo,Consolas,monospace;font-size:11px;color:var(--muted)}

/* modal */
.mask{position:fixed;inset:0;background:rgba(0,0,0,.35);display:none;align-items:center;justify-content:center;z-index:50}
.mask.on{display:flex}
.modal{background:var(--card);border-radius:14px;width:560px;max-height:76vh;display:flex;flex-direction:column;
  box-shadow:0 20px 60px rgba(0,0,0,.35);border:1px solid var(--line)}
.modal h3{margin:0;padding:14px 16px;font-size:14px;border-bottom:1px solid var(--line)}
.seg{display:flex;gap:6px;padding:12px 16px 0}
.segbtn{flex:1;padding:7px 10px;font-size:12px;border-radius:8px;background:var(--card);border:1px solid var(--line)}
.segbtn.on{background:var(--accent);color:#fff;border-color:transparent;font-weight:600}
.segbtn.on:hover{background:var(--accent);filter:brightness(1.08)}
select{font:inherit;background:var(--card);color:var(--fg);border:1px solid var(--line);
  border-radius:7px;padding:7px 10px;width:100%;outline:none;cursor:pointer}
select:focus{border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft)}
.modal .c{padding:14px 16px;overflow:auto;flex:1}
.modal .f{padding:12px 16px;border-top:1px solid var(--line);display:flex;gap:8px;justify-content:flex-end}
.browse{max-height:280px;overflow:auto;border:1px solid var(--line);border-radius:8px;margin-top:8px}
.bi{padding:6px 10px;cursor:pointer;display:flex;gap:8px;align-items:center;font-size:12px;border-bottom:1px solid var(--line)}
.bi:last-child{border-bottom:none}
.bi:hover{background:var(--hover)}
.bi.wc{color:var(--green);font-weight:600}
.crumb{font-family:"SF Mono",Menlo,Consolas,monospace;font-size:11px;color:var(--muted);word-break:break-all}

/* toast */
#toast{position:fixed;bottom:20px;left:50%;transform:translateX(-50%) translateY(80px);
  background:var(--fg);color:var(--bg);padding:9px 18px;border-radius:20px;font-size:12px;
  transition:.25s;opacity:0;max-width:80vw;z-index:99;pointer-events:none;font-weight:500}
#toast.on{transform:translateX(-50%) translateY(0);opacity:1}

/* output */
pre.out{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px;
  font-family:"SF Mono",Menlo,Consolas,monospace;font-size:11.5px;white-space:pre-wrap;word-break:break-all;
  max-height:60vh;overflow:auto;margin:8px 0}
pre.diff{padding:0}
pre.diff .add{color:var(--green)}
pre.diff .del{color:var(--red)}
pre.diff .hunk{color:var(--accent)}
.errmsg{color:var(--red);font-weight:600;font-size:12.5px;line-height:1.65;white-space:pre-wrap}
.oknote{margin-top:8px;padding:8px 10px;border-radius:8px;background:rgba(40,167,69,.13);
  color:var(--green);font-size:12px;line-height:1.6;font-weight:500}
#coLog details summary{cursor:pointer;margin-top:8px;outline:none}
.loading{opacity:.5;pointer-events:none}
/* 提交进度 */
#cProgWrap{flex:none;padding:8px 16px 0;display:none}
.cprog{border:1px solid var(--line);background:var(--card);border-radius:10px;padding:9px 12px;font-size:12px}
.cprog .top{display:flex;justify-content:space-between;gap:10px;align-items:baseline}
.cprog .top b{font-size:12.5px}
.cprog .top span{color:var(--muted);font-size:11.5px}
.cprog .bar{height:7px;border-radius:5px;background:var(--hover);overflow:hidden;margin:7px 0 6px}
.cprog .bar i{display:block;height:100%;width:0;background:var(--accent);border-radius:5px;
  transition:width .25s ease}
.cprog.ok .bar i{background:var(--green)}
.cprog.bad .bar i{background:var(--red)}
.cprog pre{max-height:96px;overflow:auto;margin:0;font-family:"SF Mono",Menlo,Consolas,monospace;
  font-size:11px;line-height:1.5;color:var(--muted);white-space:pre-wrap;word-break:break-all}
.cprog .cur{color:var(--fg);font-size:11.5px;margin-bottom:4px;word-break:break-all}
tr.row.sent td{opacity:.45}
tr.row.sent td::after{content:""}
</style>
</head>
<body>
<div class="app">
  <aside>
    <div class="brand"><span class="dot"></span>SVN Manager</div>
    <div class="plist" id="plist"></div>
    <div class="side-foot">
      <button onclick="openAdd()">＋ 添加项目</button>
      <button onclick="refreshAll()" title="刷新全部">↻</button>
    </div>
  </aside>

  <main>
    <div class="topbar">
      <div style="min-width:0;flex:1">
        <div class="ptitle" id="pname">未选择项目</div>
        <div class="ppath" id="ppath"></div>
      </div>
      <span class="chip" id="prev" style="display:none"></span>
      <button id="btnUp" onclick="doUpdate()" disabled>更新</button>
      <button id="btnReveal" onclick="revealRoot()" disabled>访达</button>
      <button id="btnClean" onclick="doCleanup()" disabled>清理</button>
    </div>

    <div class="tabs">
      <div class="tab on" data-tab="changes" onclick="setTab('changes')">改动</div>
      <div class="tab" data-tab="log" onclick="setTab('log')">历史</div>
      <div class="tab" data-tab="info" onclick="setTab('info')">详情</div>
    </div>

    <div id="cProgWrap"></div>

    <div class="body" id="body">
      <div class="empty">← 从左侧选择一个项目</div>
    </div>

    <div class="commitbar" id="cbar" style="display:none">
      <div style="flex:1">
        <textarea id="msg" placeholder="提交信息（必填，例如：修复金龙副本掉落数值）"></textarea>
      </div>
      <div class="side">
        <div class="hint" id="selhint">已选 0 项</div>
        <div class="btnrow">
          <button class="primary" id="btnCommit" onclick="doCommit()" disabled>提交选中</button>
          <button class="danger" id="btnDel" onclick="delSelected()" disabled>删除选中</button>
        </div>
      </div>
    </div>
  </main>
</div>

<div class="mask" id="mask" onclick="if(event.target===this)closeModal()">
  <div class="modal">
    <h3>添加 SVN 项目</h3>
    <div class="seg">
      <button class="segbtn on" id="segExist" onclick="setAddTab('exist')">本机已有的项目</button>
      <button class="segbtn" id="segCo" onclick="setAddTab('checkout')">从服务器下载</button>
    </div>
    <div class="c">
      <div id="paneExist">
        <label class="hint">项目文件夹（选你电脑上已经在用的那个 SVN 文件夹）</label>
        <input id="newpath" placeholder="/Users/you/Desktop/项目名" style="margin:6px 0 8px">
        <label class="hint">显示名称（留空则用目录名）</label>
        <input id="newname" placeholder="例如 newSVN" style="margin:6px 0 10px">
        <div class="rowtop">
          <button onclick="browse(document.getElementById('newpath').value||'~','newpath')">浏览…</button>
          <span class="crumb" id="crumb"></span>
        </div>
        <div class="browse" id="browse" style="display:none"></div>
      </div>

      <div id="paneCo" style="display:none">
        <label class="hint">SVN 地址（问同事要，或者从别的电脑上的仓库信息里复制）</label>
        <input id="coUrl" placeholder="svn://svn.example.com/repo/项目名" style="margin:6px 0 8px">
        <label class="hint">保存到（目录必须为空或不存在）</label>
        <input id="coPath" placeholder="/Users/you/Desktop/项目名" style="margin:6px 0 6px">
        <div class="rowtop">
          <button onclick="browse(document.getElementById('coPath').value||'~','coPath')">浏览…</button>
          <span class="crumb" id="coCrumb"></span>
        </div>
        <div class="browse" id="coBrowse" style="display:none"></div>

        <label class="hint" style="display:block;margin-top:12px">下载多少内容</label>
        <select id="coDepth" style="margin:6px 0 0">
          <option value="immediates">只下这一层（推荐，里面的子文件夹先留空）</option>
          <option value="infinity">全部下载（整个文件夹树，仓库大时会很久）</option>
          <option value="files">只要这一层的文件（不要子文件夹）</option>
          <option value="empty">只建空的文件夹结构（不下载任何文件）</option>
        </select>

        <div class="rowtop" style="margin-top:12px">
          <input id="coUser" placeholder="用户名（留空用已保存凭据）" style="flex:1">
          <input id="coPass" type="password" placeholder="密码（留空用已保存凭据）" style="flex:1">
        </div>

        <pre class="out" id="coLog" style="display:none;max-height:190px;margin-top:10px"></pre>
      </div>
    </div>
    <div class="f">
      <button onclick="closeModal()">取消</button>
      <button class="primary" id="btnAddOk" onclick="addProject()">添加</button>
      <button class="primary" id="btnCoStart" onclick="startCheckout()" style="display:none">开始下载</button>
    </div>
  </div>
</div>

<div id="toast"></div>

<script>
const $ = s => document.querySelector(s);
let S = {projects:[], cur:null, items:[], sel:new Set(), tab:'changes', rev:'?', showUnver:true};

function toast(t, ms=2200){
  const el = $('#toast'); el.textContent = t; el.classList.add('on');
  clearTimeout(el._t); el._t = setTimeout(()=>el.classList.remove('on'), ms);
}
async function api(path, opt){
  const r = await fetch(path, opt ? {
    method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(opt)
  } : undefined);
  try { return await r.json(); } catch(e){ return {error:'响应解析失败'}; }
}
function esc(s){ return String(s).replace(/[&<>"']/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }

/* 本地修改时间：相对时间显示，鼠标悬停看完整时间 */
function fmtTime(ts){
  if(!ts) return '—';
  const d = new Date(ts*1000), diff = (Date.now() - d.getTime())/1000;
  if(diff < 60) return '刚刚';
  if(diff < 3600) return Math.floor(diff/60) + ' 分钟前';
  if(diff < 86400) return Math.floor(diff/3600) + ' 小时前';
  if(diff < 86400*7) return Math.floor(diff/86400) + ' 天前';
  const p = n => String(n).padStart(2,'0');
  const sameYear = d.getFullYear() === new Date().getFullYear();
  return sameYear ? `${p(d.getMonth()+1)}-${p(d.getDate())}`
                  : `${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())}`;
}
function fullTime(ts){
  if(!ts) return '本地文件不存在（已删除或缺失）';
  const d = new Date(ts*1000), p = n => String(n).padStart(2,'0');
  return `${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

async function refreshAll(){
  const d = await api('/api/projects');
  S.projects = d.projects || [];
  if(S.cur && !S.projects.find(p=>p.name===S.cur)) S.cur = null;
  renderProjects();
  if(S.cur) loadStatus();
}
function renderProjects(){
  const el = $('#plist');
  if(!S.projects.length){ el.innerHTML = '<div class="empty" style="padding:24px 12px;font-size:12px">还没有项目<br>点下面「添加项目」</div>'; return; }
  el.innerHTML = S.projects.map(p=>`
    <div class="pitem ${p.name===S.cur?'on':''}" onclick="selectProject('${esc(p.name)}')">
      <span class="nm">${esc(p.name)}</span>
      <span class="rv">r${esc(p.rev)}</span>
      ${p.changes?`<span class="cnt">${p.changes}</span>`:''}
    </div>`).join('');
}
function selectProject(name){
  S.cur = name; S.sel.clear(); S.tab='changes';
  renderProjects(); setTab('changes'); loadStatus(); loadTop();
}
function loadTop(){
  const p = S.projects.find(x=>x.name===S.cur);
  if(!p) return;
  $('#pname').textContent = p.name;
  $('#ppath').textContent = p.path;
  $('#btnUp').disabled = $('#btnReveal').disabled = $('#btnClean').disabled = false;
  $('#cbar').style.display = 'flex';
}
function setTab(t){
  S.tab = t;
  document.querySelectorAll('.tab').forEach(e=>e.classList.toggle('on', e.dataset.tab===t));
  $('#cbar').style.display = (t==='changes' && S.cur) ? 'flex' : 'none';
  if(!S.cur){ $('#body').innerHTML = '<div class="empty">← 从左侧选择一个项目</div>'; return; }
  if(t==='changes') renderStatus();
  if(t==='log') renderLog();
  if(t==='info') renderInfo();
}
async function loadStatus(){
  if(!S.cur) return;
  $('#body').classList.add('loading');
  const d = await api('/api/status?name='+encodeURIComponent(S.cur));
  $('#body').classList.remove('loading');
  if(d.error){ $('#body').innerHTML = `<div class="empty">✗ ${esc(d.error)}</div>`; return; }
  S.items = d.items || []; S.rev = d.rev;
  const p = S.projects.find(x=>x.name===S.cur); if(p){ p.rev = d.rev; p.changes = S.items.length; }
  $('#prev').style.display='inline-block'; $('#prev').textContent = 'r' + d.rev;
  renderProjects();
  if(S.tab==='changes') renderStatus();
}
function visibleItems(){
  const all = S.items.filter(i=>i.code!=='I' && i.code!=='X');
  return S.showUnver === false ? all.filter(i=>i.code!=='?') : all;
}
function unverCount(){ return S.items.filter(i=>i.code==='?').length; }
function topBar(shown, hidden){
  const ckAll = shown.length>0 && shown.every(i=>S.sel.has(i.path));
  return `
    <div class="rowtop">
      <label class="selall"><input type="checkbox" id="ckall" ${ckAll?'checked':''} ${shown.length?'':'disabled'} onchange="toggleAll(this.checked)"> 全选</label>
      <label class="selall" title="未纳管 = 服务器上还没有的新文件；取消勾选后此开关依然保留，可随时勾回">
        <input type="checkbox" ${S.showUnver?'checked':''} onchange="S.showUnver=this.checked;S.sel.clear();renderStatus()"> 含未纳管
      </label>
      <span class="hint">共 ${shown.length} 项 · 冲突 ${shown.filter(i=>i.code==='C').length}${hidden?` · 已隐藏 ${hidden} 个未纳管`:''}</span>
    </div>`;
}
function renderStatus(){
  const el = $('#body');
  const items = visibleItems();
  const hidden = S.showUnver ? 0 : unverCount();
  if(!items.length){
    const msg = hidden
      ? `已隐藏 ${hidden} 个未纳管文件 —— 勾选上方「含未纳管」即可重新显示`
      : '✓ 工作副本干净，没有待提交的改动';
    el.innerHTML = topBar([], hidden) + `<div class="empty">${msg}</div>`;
    S.sel.clear();
    const sh = $('#selhint'); if(sh) sh.textContent='已选 0 项';
    const bc = $('#btnCommit'); if(bc) bc.disabled=true;
    const bd = $('#btnDel'); if(bd) bd.disabled=true;
    return;
  }
  el.innerHTML = topBar(items, hidden) + `
    <table>
      <thead><tr><th class="cb"></th><th style="width:140px">状态</th><th>路径</th><th style="width:92px">修改时间</th><th></th></tr></thead>
      <tbody>
      ${items.map((it,i)=>`
        <tr class="row ${S.sel.has(it.path)?'sel':''}" data-p="${esc(it.path)}">
          <td class="cb"><input type="checkbox" ${S.sel.has(it.path)?'checked':''}
              onchange="toggleOne(${i},this.checked)"></td>
          <td><span class="badge b-${esc(it.code)}">${esc(it.code)}</span><span class="badge lbl" style="color:var(--muted)">${esc(it.label)}</span></td>
          <td class="pth">${it.is_dir?'<span class="dir">▸</span>':''}${esc(it.path)}</td>
          <td class="tm" title="${fullTime(it.mtime)}">${fmtTime(it.mtime)}</td>
          <td class="act">
            ${it.code==='?'?'':`<button onclick="showDiff('${esc(it.path).replace(/'/g,"\\'")}')">差异</button>`}
            <button onclick="reveal('${esc(it.path).replace(/'/g,"\\'")}')">定位</button>
            <button class="del" onclick="delOne('${esc(it.path).replace(/'/g,"\\'")}')">删除</button>
          </td>
        </tr>`).join('')}
      </tbody>
    </table>`;
  updateSelHint();
}
function toggleAll(on){
  visibleItems().forEach(i=> on?S.sel.add(i.path):S.sel.delete(i.path));
  renderStatus();
}
function toggleOne(i, on){
  const it = visibleItems()[i];
  if(!it) return;
  on?S.sel.add(it.path):S.sel.delete(it.path);
  renderStatus();
}
function updateSelHint(){
  const n = S.sel.size;
  const el = $('#selhint'); if(el) el.textContent = '已选 ' + n + ' 项';
  const bc = $('#btnCommit'); if(bc) bc.disabled = n===0;
  const bd = $('#btnDel'); if(bd) bd.disabled = n===0;
}
async function showDiff(path){
  $('#body').classList.add('loading');
  const d = await api('/api/diff?name='+encodeURIComponent(S.cur)+'&path='+encodeURIComponent(path));
  $('#body').classList.remove('loading');
  const txt = d.diff || '（无差异）';
  const html = txt.split('\n').map(l=>{
    if(l.startsWith('+')&&!l.startsWith('+++')) return `<span class="add">${esc(l)}</span>`;
    if(l.startsWith('-')&&!l.startsWith('---')) return `<span class="del">${esc(l)}</span>`;
    if(l.startsWith('@@')) return `<span class="hunk">${esc(l)}</span>`;
    return esc(l);
  }).join('\n');
  $('#body').innerHTML = `<div class="rowtop"><button onclick="setTab('changes')">← 返回列表</button>
    <span class="crumb">${esc(path)}</span></div><pre class="out diff">${html||'（空）'}</pre>`;
  $('#cbar').style.display='none';
}
async function doUpdate(){
  $('#body').classList.add('loading');
  toast('正在更新…', 60000);
  const d = await api('/api/update', {name:S.cur});
  $('#body').classList.remove('loading');
  toast(d.ok?'更新完成':'更新出错');
  $('#body').innerHTML = `<pre class="out">${esc(d.output||'')}</pre>`;
  await refreshAll(); await loadStatus();
}
async function doCleanup(){
  if(!confirm('对「'+S.cur+'」执行 svn cleanup？\n（用于解除锁定的工作副本）')) return;
  toast('清理中…');
  const d = await api('/api/cleanup', {name:S.cur});
  toast(d.ok?'清理完成':'清理出错');
  $('#body').innerHTML = `<pre class="out">${esc(d.output||'')}</pre>`;
  await loadStatus();
}
async function delOne(p){
  if(!confirm('确定删除？\n\n' + p + '\n\n· 已纳入版本控制 → 标记为删除，提交后生效（可撤销）\n· 未纳管 → 直接从磁盘移除文件')) return;
  await doDelete([p]);
}
async function delSelected(){
  const paths = [...S.sel];
  if(!paths.length) return;
  const list = paths.slice(0, 8).join('\n') + (paths.length > 8 ? '\n… 共 ' + paths.length + ' 项' : '');
  if(!confirm('确定删除选中的 ' + paths.length + ' 项？\n\n' + list)) return;
  await doDelete(paths);
}
async function doDelete(paths){
  $('#body').classList.add('loading');
  toast('正在删除…', 30000);
  const d = await api('/api/delete', {name:S.cur, paths});
  $('#body').classList.remove('loading');
  if(d.ok){
    toast('✓ 已删除 ' + paths.length + ' 项', 3000);
    S.sel.clear();
    await refreshAll(); await loadStatus();
  } else {
    toast('✗ 删除失败', 4000);
    $('#body').innerHTML = `<pre class="out">${esc(d.output||'')}</pre>`;
  }
}
let commitTaskId = null;
function hideCommitProg(){
  const w = $('#cProgWrap'); if(w){ w.style.display='none'; w.innerHTML=''; }
}
function markSent(sent){
  const ps = (sent||[]).map(x=>x.path||'').filter(Boolean);
  if(!ps.length) return;
  document.querySelectorAll('#body tr.row').forEach(tr=>{
    const p = tr.getAttribute('data-p')||'';
    if(p && ps.some(s=> s===p || s.endsWith('/'+p) || s.endsWith(p))) tr.classList.add('sent');
  });
}
function renderCommitProg(t, total){
  const w = $('#cProgWrap'); if(!w) return;
  w.style.display = 'block';
  const tot = t.total || total || 0;
  const done = Math.min(t.done||0, tot || (t.done||0));
  const pct = tot ? Math.min(100, Math.round(done/tot*100)) : (t.status==='running'?8:100);
  const cls = t.status==='running' ? '' : (t.status==='done' ? 'ok' : 'bad');
  const title = t.status==='running' ? '正在上传…' : (t.status==='done' ? '✓ 上传完成' : '✗ 上传失败');
  const sent = t.sent||[];
  const last = sent.length ? (sent[sent.length-1].action + '  ' + sent[sent.length-1].path) : '';
  const lines = (t.lines||[]).slice(-4).join('\n');
  w.innerHTML = `<div class="cprog ${cls}">
    <div class="top"><b>${esc(title)}</b><span>已完成 ${done} / ${tot} 项${t.status==='running'?'（别关窗口）':''}</span></div>
    <div class="bar"><i style="width:${pct}%"></i></div>
    ${last?`<div class="cur">正在处理：${esc(last)}</div>`:''}
    <pre>${esc(lines)}</pre>
  </div>`;
  markSent(sent);
}
async function doCommit(){
  const paths = [...S.sel];
  const msg = $('#msg').value.trim();
  if(!paths.length) return toast('先选中要提交的文件');
  if(!msg) { toast('提交信息不能为空'); $('#msg').focus(); return; }
  if(!confirm(`确认提交 ${paths.length} 项到「${S.cur}」？\n\n${msg}`)) return;
  $('#btnCommit').disabled = true;
  const d = await api('/api/commit', {name:S.cur, paths, message:msg, add_unversioned:true});
  if(!d.ok || !d.id){
    toast('✗ ' + (d.output || '提交没能开始'), 5000);
    $('#btnCommit').disabled = false;
    return;
  }
  commitTaskId = d.id;
  renderCommitProg({status:'running', done:0, total:d.total||paths.length, sent:[], lines:['正在检查工作副本…']}, paths.length);
  pollCommit(paths.length);
}
async function pollCommit(total){
  if(!commitTaskId) return;
  const t = await api('/api/commit/status?id='+commitTaskId);
  if(t.error){
    hideCommitProg(); toast('✗ '+t.error, 4000); commitTaskId = null;
    $('#btnCommit').disabled = false; return;
  }
  renderCommitProg(t, total);
  if(t.status === 'running'){ setTimeout(()=>pollCommit(total), 800); return; }
  commitTaskId = null;
  if(t.status === 'done'){
    toast('✓ 提交成功', 3000);
    $('#msg').value = ''; S.sel.clear();
    await refreshAll(); await loadStatus();
    setTimeout(hideCommitProg, 6000);
  } else {
    toast('✗ ' + (t.error || '提交失败'), 6000);
    const w = $('#cProgWrap');
    const raw = (t.output||'').split('\n').slice(-8).join('\n');
    if(w && raw){
      w.style.display='block';
      w.innerHTML += `<div class="cprog bad" style="margin-top:8px"><div class="top"><b>${esc(t.error||'提交失败')}</b><span>服务器没收到这批文件</span></div><pre>${esc(raw)}</pre></div>`;
    }
    await refreshAll(); await loadStatus();
  }
}
async function reveal(p){ await api('/api/reveal', {name:S.cur, path:p||''}); }
function revealRoot(){ api('/api/reveal', {name:S.cur, path:''}); }
async function renderLog(){
  $('#body').innerHTML = '<div class="empty">加载中…</div>';
  const d = await api('/api/log?name='+encodeURIComponent(S.cur)+'&limit=40');
  const es = d.entries || [];
  if(!es.length){ $('#body').innerHTML='<div class="empty">没有历史记录</div>'; return; }
  $('#body').innerHTML = es.map(e=>`
    <div class="le">
      <div class="h"><span class="r">r${esc(e.rev)}</span><span>${esc(e.author||'?')}</span>
        <span>${esc((e.date||'').replace('T',' ').slice(0,19))}</span></div>
      <div class="m">${esc(e.msg||'（无提交信息）')}</div>
      ${e.paths&&e.paths.length?`<div class="ps">${e.paths.slice(0,8).map(p=>
        `<div>${esc(p.action)} ${esc(p.path)}</div>`).join('')}${e.path_total>8?`<div>… 另外 ${e.path_total-8} 个</div>`:''}</div>`:''}
    </div>`).join('');
}
async function renderInfo(){
  $('#body').innerHTML = '<div class="empty">加载中…</div>';
  const d = await api('/api/info?name='+encodeURIComponent(S.cur));
  if(d.error){ $('#body').innerHTML=`<div class="empty">✗ ${esc(d.error)}</div>`; return; }
  const keep = ['Path','URL','Repository Root','Repository UUID','Revision','Node Kind','Last Changed Author','Last Changed Rev','Last Changed Date'];
  $('#body').innerHTML = `<table><tbody>${keep.filter(k=>d[k]).map(k=>
    `<tr><td style="width:150px;color:var(--muted)">${esc(k)}</td><td class="pth">${esc(d[k])}</td></tr>`).join('')}</tbody></table>`;
}
/* ── 添加项目 / 从服务器检出 ── */
let addTab = 'exist';
let coTaskId = null;

function openAdd(){
  $('#mask').classList.add('on');
  setAddTab('exist');
  setTimeout(()=>{ try{ $('#newpath').focus(); }catch(e){} }, 50);
}
function setAddTab(t){
  addTab = t;
  $('#paneExist').style.display  = t==='exist'    ? 'block' : 'none';
  $('#paneCo').style.display     = t==='checkout' ? 'block' : 'none';
  $('#btnAddOk').style.display   = t==='exist'    ? '' : 'none';
  $('#btnCoStart').style.display = t==='checkout' ? '' : 'none';
  $('#segExist').classList.toggle('on', t==='exist');
  $('#segCo').classList.toggle('on', t==='checkout');
}
async function closeModal(){
  if(coTaskId){
    if(!confirm('还在下载中，现在关掉会中断下载。确定要取消吗？')) return;
    await api('/api/checkout/cancel', {id: coTaskId});
    coTaskId = null;
  }
  $('#mask').classList.remove('on');
  $('#browse').style.display='none';
  $('#coBrowse').style.display='none';
}
async function addProject(){
  const path = $('#newpath').value.trim();
  const name = $('#newname').value.trim();
  if(!path) return toast('请填路径');
  const d = await api('/api/project/add', {path, name});
  if(!d.ok) return toast('✗ ' + (d.error||'添加失败'));
  closeModal(); $('#newpath').value=''; $('#newname').value='';
  toast('✓ 已添加：'+d.name);
  await refreshAll(); selectProject(d.name);
}

/* 目录浏览：target 决定把路径写进哪个输入框 */
async function browse(path, target){
  target = target || 'newpath';
  const isCo = target === 'coPath';
  const d = await api('/api/fs?path='+encodeURIComponent(path||'~'));
  if(d.error) return toast(d.error);
  $(isCo ? '#coCrumb' : '#crumb').textContent = d.path;
  const box = $(isCo ? '#coBrowse' : '#browse');
  box.style.display = 'block';
  const q = s => String(s).replace(/\\/g,'/').replace(/'/g,"\\'");
  let h = '';
  if(d.parent) h += `<div class="bi" onclick="browse('${q(d.parent)}','${target}')">⬆︎ 上级目录</div>`;
  h += `<div class="bi wc" onclick="useWC('${q(d.path)}','${target}')">✓ 使用当前目录${(!isCo && d.is_wc) ? '（这里有 .svn）' : ''}</div>`;
  h += d.dirs.map(x=>`<div class="bi ${x.is_wc?'wc':''}" onclick="browse('${q(x.path)}','${target}')">
      ${x.is_wc?'📦':'📁'} ${esc(x.name)}${x.is_wc?' <span class="hint">SVN 工作副本</span>':''}</div>`).join('');
  if(!d.dirs.length) h += '<div class="bi" style="color:var(--muted)">（没有子目录）</div>';
  box.innerHTML = h;
}
function useWC(p, target){
  target = target || 'newpath';
  $('#'+target).value = p;
  $(target==='coPath' ? '#coBrowse' : '#browse').style.display='none';
}

/* 检出：启动后轮询进度 */
async function startCheckout(){
  const url  = $('#coUrl').value.trim();
  const path = $('#coPath').value.trim();
  if(!url)  return toast('请填写 SVN 地址');
  if(!path) return toast('请填写保存路径');
  const log = $('#coLog');
  log.style.display = 'block';
  log.textContent = '正在开始下载…';
  $('#btnCoStart').disabled = true;
  const d = await api('/api/checkout', {
    url, path,
    depth: $('#coDepth').value,
    username: $('#coUser').value.trim(),
    password: $('#coPass').value
  });
  if(!d.ok){
    log.innerHTML = '<div class="errmsg">' + esc(d.error || '启动失败') + '</div>';
    $('#btnCoStart').disabled = false;
    return;
  }
  if(d.mode === 'update'){
    log.textContent = '发现这个文件夹里已经有上次没下完的内容，正在清理并接着下载…';
  }
  coTaskId = d.id;
  pollCheckout();
}
async function pollCheckout(){
  if(!coTaskId) return;
  const t = await api('/api/checkout/status?id='+coTaskId);
  const log = $('#coLog');
  if(t.error){
    log.textContent = '✗ ' + t.error;
    $('#btnCoStart').disabled = false;
    coTaskId = null;
    return;
  }
  const lines = (t.lines||[]).slice(-14);
  if(t.status === 'running'){
    log.textContent = lines.join('\n') + '\n\n… 已经下载 ' + (t.count||0) + ' 个文件';
    log.scrollTop = log.scrollHeight;
    setTimeout(pollCheckout, 1200);
    return;
  }
  $('#btnCoStart').disabled = false;
  if(t.status === 'done'){
    log.textContent = '✓ 检出完成，已添加项目「' + t.project + '」';
    const pj = t.project;
    coTaskId = null;
    setTimeout(async ()=>{
      $('#mask').classList.remove('on');
      await refreshAll();
      selectProject(pj);
    }, 1000);
  } else if(t.status === 'cancelled'){
    log.textContent = '已取消下载';
    coTaskId = null;
  } else {
    const raw = t.raw || lines.join('\n');
    let html = '<div class="errmsg">' + esc(t.error || '下载失败') + '</div>';
    if(t.project){
      html += '<div class="oknote">已经下到本地的内容，已经加到左边的项目列表里了（名字：'
            + esc(t.project) + '）。再点一次「开始下载」会接着把剩下的下完。</div>';
    }
    if(raw){
      html += '<details><summary class="hint">原始信息（技术细节）</summary>'
            + '<pre class="out" style="max-height:150px;overflow:auto;margin-top:6px">'
            + esc(raw) + '</pre></details>';
    }
    log.innerHTML = html;
    coTaskId = null;
  }
}

/* init */
(async () => {
  await refreshAll();
  const want = new URLSearchParams(location.search).get('p');
  if (want && S.projects.some(x => x.name === want)) selectProject(want);
  else if (S.projects.length) selectProject(S.projects[0].name);

  // 支持 #checkout / #add 直接打开添加弹窗
  const hash = location.hash.replace('#', '');
  if (hash === 'checkout' || hash === 'add') {
    openAdd();
    setAddTab(hash === 'checkout' ? 'checkout' : 'exist');
  }
})();
setInterval(()=>{ if(S.cur && S.tab==='changes' && !document.querySelector('#msg')?.matches(':focus')) loadStatus(); }, 30000);
</script>
</body>
</html>
"""


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        pass  # 客户端断开不打印堆栈


def main():
    # Windows 上用 pythonw 启动时没有 stdout/stderr，print 会抛异常导致崩掉
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = sys.stdout

    port = DEFAULT_PORT
    no_open = False
    for i, a in enumerate(sys.argv):
        if a == "--port" and i + 1 < len(sys.argv):
            port = int(sys.argv[i + 1])
        if a == "--no-browser":
            no_open = True

    srv = Server(("127.0.0.1", port), Handler)
    url = "http://127.0.0.1:%d" % port
    print("%s 已启动: %s" % (APP, url))
    print("按 Ctrl+C 退出")
    if not no_open:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出")
        srv.shutdown()


if __name__ == "__main__":
    main()
