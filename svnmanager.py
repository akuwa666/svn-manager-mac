#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SVN Manager — 多项目 Subversion 图形客户端
本地 Web 界面，零第三方依赖，直接调用系统 svn 命令行。
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

APP = "SVN Manager"
CONF = os.path.expanduser("~/.svnmanager.json")
DEFAULT_PORT = 8765


def _pick_locale():
    try:
        out = subprocess.run(["locale", "-a"], capture_output=True, text=True).stdout
    except Exception:
        return "en_US.UTF-8"
    for c in ("zh_CN.UTF-8", "zh_CN.utf8", "en_US.UTF-8", "en_US.utf8"):
        if c in out:
            return c
    return "en_US.UTF-8"


SVN_ENV = dict(os.environ)
SVN_ENV.update({"LC_ALL": _pick_locale(), "LANG": _pick_locale()})

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


# ─────────────────────────── svn 调用 ───────────────────────────
def svn(args, cwd=None, timeout=180):
    try:
        p = subprocess.run(
            ["svn"] + args, cwd=cwd, capture_output=True, env=SVN_ENV, timeout=timeout
        )
        return p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        return 124, "", "命令超时（%ds）" % timeout
    except FileNotFoundError:
        return 127, "", "找不到 svn 命令，请先安装 Subversion"
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


def safe_join(root, rel):
    """确保 rel 不逃出 root"""
    p = os.path.normpath(os.path.join(root, rel))
    if not (p == root or p.startswith(root + os.sep)):
        raise ValueError("路径越界: %s" % rel)
    return p


# ─────────────────────────── 核心操作 ───────────────────────────
def parse_status(root):
    rc, out, err = svn(["status"], cwd=root, timeout=300)
    if rc not in (0, 1):
        return [], err.strip() or "svn status 失败"
    items = []
    for line in out.splitlines():
        if len(line) < 9:
            continue
        if line[:7].strip() == ">" or line.startswith("Summary"):
            continue
        code = line[0]
        if code == " ":
            code = line[1]          # 只有属性变化
        tree_c = line[6] == "C"
        if tree_c:
            code = "C"
        if code not in CODE_LABEL:
            continue
        rel = line[8:]
        if not rel:
            continue
        kind, label = CODE_LABEL[code]
        items.append({
            "path": rel, "code": code, "kind": kind,
            "label": label, "tree": tree_c,
            "is_dir": os.path.isdir(os.path.join(root, rel)),
        })
    items.sort(key=lambda x: (RANK.get(x["code"], 99), x["path"]))
    return items, None


def project_rev(root):
    """工作副本根的 svn info revision 可能是 0，用 svnversion 取真实版本"""
    try:
        p = subprocess.run(["svnversion", root], capture_output=True,
                           env=SVN_ENV, timeout=30)
        s = p.stdout.decode("utf-8", "replace").strip()
    except Exception:
        return "?"
    if not s or "Unversioned" in s or "exported" in s:
        return "?"
    tail = s.split(":")[-1]
    digits = "".join(ch for ch in tail if ch.isdigit())
    return digits or "?"


def do_update(root):
    rc, out, err = svn(["update", "--non-interactive", "--accept", "postpone"],
                       cwd=root, timeout=1800)
    return {"ok": rc == 0, "output": (out + err).strip()}


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


def do_log(root, limit=30):
    # 工作副本根的 BASE rev 可能为 0，必须显式指定范围才能查到历史
    rc, out, err = svn(["log", "-v", "-r", "HEAD:1", "-l", str(limit), "--xml"],
                       cwd=root, timeout=120)
    if rc != 0 or "<logentry" not in out:
        # 离线兜底：用本地缓存的历史
        rc, out, err = svn(["log", "-v", "-l", str(limit), "--xml"], cwd=root, timeout=120)
    if rc != 0:
        return []
    import xml.etree.ElementTree as ET
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
            name = (b.get("name") or "").strip()
            if not path or not os.path.isdir(path):
                return self._json({"ok": False, "error": "目录不存在"})
            if not os.path.isdir(os.path.join(path, ".svn")):
                return self._json({"ok": False, "error": "这个目录不是 SVN 工作副本（没有 .svn）"})
            path = os.path.realpath(path)
            if not name:
                name = os.path.basename(path)
            d = load_conf()
            if any(x["name"] == name for x in d["projects"]):
                i = 2
                while any(x["name"] == "%s-%d" % (name, i) for x in d["projects"]):
                    i += 1
                name = "%s-%d" % (name, i)
            d["projects"].append({"name": name, "path": path})
            save_conf(d)
            return self._json({"ok": True, "name": name})

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

        if p == "/api/commit":
            pr = find_project(b.get("name"))
            if not pr:
                return self._json({"ok": False, "output": "项目不存在"})
            return self._json(do_commit(pr["path"], b.get("paths") or [],
                                        b.get("message") or "", b.get("add_unversioned", True)))

        if p == "/api/reveal":
            pr = find_project(b.get("name"))
            if not pr:
                return self._json({"ok": False}, 404)
            try:
                target = safe_join(pr["path"], b.get("path") or "")
            except ValueError:
                return self._json({"ok": False}, 400)
            if os.path.exists(target):
                subprocess.Popen(["open", "-R", target])
            else:
                subprocess.Popen(["open", pr["path"]])
            return self._json({"ok": True})

        if p == "/api/open":
            pr = find_project(b.get("name"))
            if pr:
                subprocess.Popen(["open", pr["path"]])
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
body{margin:0;font:13px/1.5 -apple-system,BlinkMacSystemFont,"SF Pro Text","PingFang SC",sans-serif;
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
  border-bottom:1px solid var(--line);background:var(--panel);position:sticky;top:0;z-index:1}
td{padding:6px 12px;border-bottom:1px solid var(--line);vertical-align:middle}
tr:last-child td{border-bottom:none}
tr.row:hover{background:var(--hover)}
tr.row.sel{background:var(--accent-soft)}
td.cb{width:34px}
td.pth{font-family:"SF Mono",Menlo,monospace;font-size:12px;word-break:break-all}
td.act{width:1%;white-space:nowrap;text-align:right}
td.act button{padding:2px 8px;font-size:11px;background:transparent;border-color:transparent;color:var(--accent)}
td.act button:hover{background:var(--accent-soft)}
.badge{display:inline-block;min-width:22px;text-align:center;font-size:11px;font-weight:700;
  padding:1px 6px;border-radius:5px;font-family:"SF Mono",Menlo,monospace}
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
.commitbar .side{display:flex;flex-direction:column;gap:6px;align-items:stretch;min-width:130px}
.hint{font-size:11px;color:var(--muted)}
.rowtop{display:flex;align-items:center;gap:8px;margin-bottom:8px}
.selall{display:flex;align-items:center;gap:6px;font-size:12px;color:var(--muted);cursor:pointer;user-select:none}
input[type=checkbox]{width:15px;height:15px;accent-color:var(--accent);cursor:pointer;margin:0}

/* log */
.le{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 14px;margin-bottom:8px}
.le .h{display:flex;gap:10px;align-items:center;font-size:11px;color:var(--muted);margin-bottom:4px}
.le .r{font-family:"SF Mono",Menlo,monospace;color:var(--accent);font-weight:700}
.le .m{white-space:pre-wrap;font-size:12.5px}
.le .ps{margin-top:6px;font-family:"SF Mono",Menlo,monospace;font-size:11px;color:var(--muted)}

/* modal */
.mask{position:fixed;inset:0;background:rgba(0,0,0,.35);display:none;align-items:center;justify-content:center;z-index:50}
.mask.on{display:flex}
.modal{background:var(--card);border-radius:14px;width:560px;max-height:76vh;display:flex;flex-direction:column;
  box-shadow:0 20px 60px rgba(0,0,0,.35);border:1px solid var(--line)}
.modal h3{margin:0;padding:14px 16px;font-size:14px;border-bottom:1px solid var(--line)}
.modal .c{padding:14px 16px;overflow:auto;flex:1}
.modal .f{padding:12px 16px;border-top:1px solid var(--line);display:flex;gap:8px;justify-content:flex-end}
.browse{max-height:280px;overflow:auto;border:1px solid var(--line);border-radius:8px;margin-top:8px}
.bi{padding:6px 10px;cursor:pointer;display:flex;gap:8px;align-items:center;font-size:12px;border-bottom:1px solid var(--line)}
.bi:last-child{border-bottom:none}
.bi:hover{background:var(--hover)}
.bi.wc{color:var(--green);font-weight:600}
.crumb{font-family:"SF Mono",Menlo,monospace;font-size:11px;color:var(--muted);word-break:break-all}

/* toast */
#toast{position:fixed;bottom:20px;left:50%;transform:translateX(-50%) translateY(80px);
  background:var(--fg);color:var(--bg);padding:9px 18px;border-radius:20px;font-size:12px;
  transition:.25s;opacity:0;max-width:80vw;z-index:99;pointer-events:none;font-weight:500}
#toast.on{transform:translateX(-50%) translateY(0);opacity:1}

/* output */
pre.out{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px;
  font-family:"SF Mono",Menlo,monospace;font-size:11.5px;white-space:pre-wrap;word-break:break-all;
  max-height:60vh;overflow:auto;margin:8px 0}
pre.diff{padding:0}
pre.diff .add{color:var(--green)}
pre.diff .del{color:var(--red)}
pre.diff .hunk{color:var(--accent)}
.loading{opacity:.5;pointer-events:none}
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

    <div class="body" id="body">
      <div class="empty">← 从左侧选择一个项目</div>
    </div>

    <div class="commitbar" id="cbar" style="display:none">
      <div style="flex:1">
        <textarea id="msg" placeholder="提交信息（必填，例如：修复金龙副本掉落数值）"></textarea>
      </div>
      <div class="side">
        <div class="hint" id="selhint">已选 0 项</div>
        <button class="primary" id="btnCommit" onclick="doCommit()" disabled>提交选中</button>
      </div>
    </div>
  </main>
</div>

<div class="mask" id="mask" onclick="if(event.target===this)closeModal()">
  <div class="modal">
    <h3>添加 SVN 项目</h3>
    <div class="c">
      <label class="hint">项目路径（必须是已有 .svn 的工作副本目录）</label>
      <input id="newpath" placeholder="/Users/you/Desktop/项目名" style="margin:6px 0 8px">
      <label class="hint">显示名称（留空则用目录名）</label>
      <input id="newname" placeholder="例如 newSVN" style="margin:6px 0 10px">
      <div class="rowtop">
        <button onclick="browse(document.getElementById('newpath').value||'~')">浏览…</button>
        <span class="crumb" id="crumb"></span>
      </div>
      <div class="browse" id="browse" style="display:none"></div>
    </div>
    <div class="f">
      <button onclick="closeModal()">取消</button>
      <button class="primary" onclick="addProject()">添加</button>
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
function renderStatus(){
  const el = $('#body');
  const items = visibleItems();
  if(!items.length){
    el.innerHTML = '<div class="empty">✓ 工作副本干净，没有待提交的改动</div>';
    $('#selhint').textContent='已选 0 项'; $('#btnCommit').disabled=true; return;
  }
  const hidden = S.items.filter(i=>i.code==='?').length;
  el.innerHTML = `
    <div class="rowtop">
      <label class="selall"><input type="checkbox" id="ckall" onchange="toggleAll(this.checked)"> 全选</label>
      <label class="selall" title="未纳管 = 服务器上还没有的新文件">
        <input type="checkbox" ${S.showUnver?'checked':''} onchange="S.showUnver=this.checked;S.sel.clear();renderStatus()"> 含未纳管
      </label>
      <span class="hint">共 ${items.length} 项 · 冲突 ${items.filter(i=>i.code==='C').length}${(!S.showUnver&&hidden)?` · 已隐藏 ${hidden} 个未纳管`:''}</span>
    </div>
    <table>
      <thead><tr><th class="cb"></th><th style="width:140px">状态</th><th>路径</th><th></th></tr></thead>
      <tbody>
      ${items.map((it,i)=>`
        <tr class="row ${S.sel.has(it.path)?'sel':''}" data-p="${esc(it.path)}">
          <td class="cb"><input type="checkbox" ${S.sel.has(it.path)?'checked':''}
              onchange="toggleOne(${i},this.checked)"></td>
          <td><span class="badge b-${esc(it.code)}">${esc(it.code)}</span><span class="badge lbl" style="color:var(--muted)">${esc(it.label)}</span></td>
          <td class="pth">${it.is_dir?'<span class="dir">▸</span>':''}${esc(it.path)}</td>
          <td class="act">
            ${it.code==='?'?'':`<button onclick="showDiff('${esc(it.path).replace(/'/g,"\\'")}')">差异</button>`}
            <button onclick="reveal('${esc(it.path).replace(/'/g,"\\'")}')">定位</button>
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
  $('#selhint').textContent = '已选 ' + n + ' 项';
  $('#btnCommit').disabled = n===0;
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
async function doCommit(){
  const paths = [...S.sel];
  const msg = $('#msg').value.trim();
  if(!paths.length) return toast('先选中要提交的文件');
  if(!msg) { toast('提交信息不能为空'); $('#msg').focus(); return; }
  if(!confirm(`确认提交 ${paths.length} 项到「${S.cur}」？\n\n${msg}`)) return;
  $('#body').classList.add('loading'); $('#btnCommit').disabled = true;
  toast('正在提交…', 120000);
  const d = await api('/api/commit', {name:S.cur, paths, message:msg, add_unversioned:true});
  $('#body').classList.remove('loading');
  toast(d.ok?'✓ 提交成功':'✗ 提交失败', 4000);
  $('#body').innerHTML = `<pre class="out">${esc(d.output||'')}</pre>`;
  if(d.ok){ $('#msg').value=''; S.sel.clear(); }
  await refreshAll(); await loadStatus();
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
/* ── 添加项目 ── */
function openAdd(){ $('#mask').classList.add('on'); $('#newpath').focus(); }
function closeModal(){ $('#mask').classList.remove('on'); $('#browse').style.display='none'; }
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
async function browse(path){
  const d = await api('/api/fs?path='+encodeURIComponent(path||'~'));
  if(d.error) return toast(d.error);
  $('#crumb').textContent = d.path;
  const box = $('#browse'); box.style.display='block';
  let h = '';
  if(d.parent) h += `<div class="bi" onclick="browse('${d.parent.replace(/'/g,"\\'")}')">⬆︎ 上级目录</div>`;
  if(d.is_wc) h += `<div class="bi wc" onclick="useWC('${d.path.replace(/'/g,"\\'")}')">✓ 使用当前目录（这里有 .svn）</div>`;
  h += d.dirs.map(x=>`<div class="bi ${x.is_wc?'wc':''}" onclick="browse('${x.path.replace(/'/g,"\\'")}')">
      ${x.is_wc?'📦':'📁'} ${esc(x.name)}${x.is_wc?' <span class="hint">SVN 工作副本</span>':''}</div>`).join('');
  if(!d.dirs.length && !d.is_wc) h += '<div class="bi" style="color:var(--muted)">（没有子目录）</div>';
  box.innerHTML = h;
}
function useWC(p){ $('#newpath').value = p; $('#browse').style.display='none'; }

/* init */
(async () => {
  await refreshAll();
  const want = new URLSearchParams(location.search).get('p');
  if (want && S.projects.some(x => x.name === want)) selectProject(want);
  else if (S.projects.length) selectProject(S.projects[0].name);
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
