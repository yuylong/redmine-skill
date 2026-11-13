#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Redmine CLI — 提票/回票/查票/更新状态/记工时（Redmine REST API，仅标准库）.

凭据来源（优先级从高到低）：
  1. 命令行参数（位置任意）: --url https://redmine.example.com  --key <API访问密钥>
  2. --server <名字> 选中配置文件里的某个服务器（此时该服务器配置优先于环境变量）
  3. 环境变量: REDMINE_URL / REDMINE_API_KEY（自签名证书再加 REDMINE_INSECURE=1）
  4. 配置文件: ~/.redmine/config.json
     {"servers": {"work": {"url": "...", "key": "...", "insecure": true,
                           "mode": "readwrite"}}, "default": "work"}
     旧格式 {"url": "...", "key": "..."} 仍可用，等价于名为 default 的服务器

写权限 mode（按服务器配置，缺省 readwrite）：
  readonly   只读，所有写命令直接拒绝（退出码 2），不发任何写请求
  confirm    弱可读写，写命令先输出变更预览并以退出码 3 结束，加 --confirm 才真正提交
  readwrite  可读写，不做额外拦截
可用 --mode 或环境变量 REDMINE_MODE 覆盖，优先级与 url/key 一致。
"""

import argparse
import json
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request

if hasattr(sys.stdout, "reconfigure"):
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


class RedmineError(Exception):
    """API 或连接失败，附带可直接展示的错误信息。"""


def die(msg):
    print("ERROR: %s" % msg, file=sys.stderr)
    sys.exit(1)


def out(msg=""):
    print(msg)


MODE_READONLY = "readonly"
MODE_READWRITE = "readwrite"
MODE_CONFIRM = "confirm"
VALID_MODES = (MODE_READONLY, MODE_READWRITE, MODE_CONFIRM)

WRITE_COMMAND_LABELS = {
    "create": "create（提票）",
    "update": "update（更新工单）",
    "note": "note（回票）",
    "close": "close（关单）",
    "log": "log（记工时）",
    "attach": "attach（上传附件）",
    "watch": "watch（增删跟踪者）",
}


def normalize_mode(value):
    """归一化为 readonly / readwrite / confirm，缺省 readwrite。"""
    v = str(value or "").strip().lower()
    if not v:
        return MODE_READWRITE
    if v not in VALID_MODES:
        die("无效 mode %r — 可选: %s" % (value, " / ".join(VALID_MODES)))
    return v


def ensure_writable(rm, cmd):
    """readonly 服务器在发出任何写请求前直接拒绝（退出码 2）。"""
    if rm.mode == MODE_READONLY:
        print("[拒绝] 服务器 %s 为 readonly 模式，不允许执行 %s"
              % (rm.server_name or "-", WRITE_COMMAND_LABELS.get(cmd, cmd)),
              file=sys.stderr)
        sys.exit(2)


def confirm_gate(rm, cmd, confirm, preview_lines):
    """confirm 模式且未带 --confirm 时，只输出变更预览并以退出码 3 结束。"""
    if rm.mode != MODE_CONFIRM or confirm:
        return
    out("[待确认] 服务器 %s 为 confirm 模式，即将执行 %s："
        % (rm.server_name or "-", WRITE_COMMAND_LABELS.get(cmd, cmd)))
    out("  服务器: %s  %s" % (rm.server_name or "-", rm.base))
    for line in preview_lines:
        out("  " + line)
    out("确认无误后请加 --confirm 重新执行")
    sys.exit(3)


def name_of(items, item_id):
    """按 id 反查名称，用于预览展示。"""
    for it in items:
        if it.get("id") == item_id:
            return str(it.get("name"))
    return str(item_id)


def old_value(cur, key):
    """取工单当前字段值（预览里的「旧值」）。"""
    if not cur:
        return "?"
    v = cur.get(key)
    if isinstance(v, dict):
        return str(v.get("name") or v.get("id") or "-")
    if v is None:
        return "-"
    return str(v)


def short_text(text, limit=40):
    """预览里压缩长文本：只留首行，超出加省略号并提示总行数。"""
    s = str(text if text is not None else "").strip()
    if not s:
        return "(空)"
    lines = s.splitlines()
    first = lines[0]
    if len(first) > limit:
        first = first[:limit] + "…"
    return first + ("（共 %d 行）" % len(lines) if len(lines) > 1 else "")


class Redmine:
    def __init__(self, url, key, insecure=False, mode=MODE_READWRITE, server_name=None):
        self.base = (url or "").rstrip("/")
        self.key = key or ""
        self.mode = mode
        self.server_name = server_name
        ctx = ssl.create_default_context()
        if insecure:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=ctx),
            urllib.request.HTTPHandler(),
        )

    def req(self, method, path, params=None, payload=None):
        if not self.base:
            die("missing server URL — 传 --url <SERVER> 或 --server <名字>、设置环境变量 "
                "REDMINE_URL，或在 %s 里配置" % config_path())
        if not self.key:
            die("missing API key — 传 --key <KEY>、设置环境变量 REDMINE_API_KEY，"
                "或在 %s 里配置"
                "（Redmine 网页端 我的账号 → API 访问密钥）" % config_path())
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode(params, doseq=True)
        if isinstance(payload, bytes):
            data = payload
            ctype = "application/octet-stream"
        elif payload is not None:
            data = json.dumps(payload).encode("utf-8")
            ctype = "application/json; charset=utf-8"
        else:
            data = None
            ctype = None
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("X-Redmine-API-Key", self.key)
        if data is not None:
            request.add_header("Content-Type", ctype)
        try:
            with self.opener.open(request, timeout=30) as resp:
                body = resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            try:
                raw = e.read().decode("utf-8", "replace")
            except Exception:
                raw = ""
            errors = None
            if raw.strip().startswith("{"):
                try:
                    errors = json.loads(raw).get("errors")
                except Exception:
                    pass
            detail = "; ".join(str(x) for x in errors) if errors else (raw.strip()[:300] or e.reason)
            raise RedmineError("HTTP %s on %s %s — %s" % (e.code, method, path, detail))
        except urllib.error.URLError as e:
            raise RedmineError(
                "cannot reach %s — %s（检查 URL/网络；自签名证书可加 --insecure）"
                % (self.base, getattr(e, "reason", e))
            )
        return json.loads(body) if body.strip() else {}


# ---------------------------------------------------------------- 元数据与解析

def as_int(value):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def match_name(items, value, label):
    """接受数字 id 或名称（先精确、后子串，忽略大小写）。"""
    n = as_int(value)
    if n is not None:
        for it in items:
            if it.get("id") == n:
                return n
        die("%s %r 未找到 — 先运行列表命令（projects/trackers/statuses/priorities/activities）查看有效值" % (label, value))
    v = str(value).strip().lower()
    for it in items:
        if str(it.get("name", "")).strip().lower() == v:
            return it["id"]
    for it in items:
        if v in str(it.get("name", "")).lower():
            return it["id"]
    die("%s %r 未找到 — 先运行列表命令查看有效值" % (label, value))


def list_projects(rm):
    """翻页取全量项目（服务器单页上限 100，总数可能更多）。"""
    projects = []
    offset = 0
    while True:
        data = rm.req("GET", "/projects.json", {"limit": 100, "offset": offset})
        batch = data.get("projects", [])
        projects += batch
        offset += len(batch)
        if len(batch) < 100 or offset >= data.get("total_count", 0):
            break
    return projects


def list_trackers(rm):
    return rm.req("GET", "/trackers.json").get("trackers", [])


def list_statuses(rm):
    return rm.req("GET", "/issue_statuses.json").get("issue_statuses", [])


def list_priorities(rm):
    return rm.req("GET", "/enumerations/issue_priorities.json").get("issue_priorities", [])


def list_activities(rm):
    return rm.req("GET", "/enumerations/time_entry_activities.json").get("time_entry_activities", [])


def resolve_project(rm, value):
    projects = list_projects(rm)
    n = as_int(value)
    if n is not None:
        for p in projects:
            if p.get("id") == n:
                return n
    else:
        v = str(value).strip().lower()
        for p in projects:
            if v in (str(p.get("name", "")).lower(), str(p.get("identifier", "")).lower()):
                return p["id"]
        for p in projects:
            if v in (str(p.get("name", "")).lower() + " " + str(p.get("identifier", "")).lower()):
                return p["id"]
    die("项目 %r 未找到 — 运行 projects 查看有效项目" % value)


def resolve_assignee(rm, value):
    v = str(value).strip()
    if v.lower() == "me":
        return rm.req("GET", "/users/current.json").get("user", {}).get("id")
    n = as_int(v)
    if n is not None:
        return n
    try:
        users = rm.req("GET", "/users.json", {"limit": 100}).get("users", [])
    except RedmineError:
        die("无法按姓名解析用户（该 API key 无权列出用户）— 请传数字 user id 或 me")
    for u in users:
        if v.lower() in (str(u.get("name", "")).lower(), str(u.get("login", "")).lower()):
            return u["id"]
    die("用户 %r 未找到 — 请传数字 user id 或 me" % value)


# ---------------------------------------------------------------- 展示

def user_ref(u):
    if not u:
        return "-"
    return str(u.get("name") or u.get("login") or ("id %s" % u.get("id")))


def fmt_issue_line(i):
    parts = ["#%s" % i.get("id"),
             "[%s]" % ((i.get("status") or {}).get("name", "?")),
             (i.get("tracker") or {}).get("name", ""),
             i.get("subject", "")]
    line = " ".join(p for p in parts if p)
    meta = []
    if i.get("project"):
        meta.append("project: %s" % i["project"].get("name"))
    if i.get("assigned_to"):
        meta.append("assignee: %s" % user_ref(i["assigned_to"]))
    if i.get("updated_on"):
        meta.append("updated: %s" % str(i["updated_on"])[:16].replace("T", " "))
    if meta:
        line += " | " + " | ".join(meta)
    return line


def indent(text, pad="  "):
    return "\n".join(pad + ln for ln in str(text).splitlines())


# ---------------------------------------------------------------- 命令

def cmd_test(rm, a):
    u = rm.req("GET", "/users/current.json").get("user", {})
    out("OK — 已连接 %s，当前用户: %s (id %s)"
        % (rm.base, u.get("name") or u.get("login"), u.get("id")))


def cmd_servers(rm, a):
    profiles, default = config_profiles(load_config())
    if not profiles:
        out("未配置任何服务器 — 在 %s 里写：\n"
            '  {"servers": {"work": {"url": "https://...", "key": "<API Key>"}}, "default": "work"}'
            % config_path())
        return
    out("已配置 %d 个服务器（默认: %s，配置文件: %s）:"
        % (len(profiles), default or "-", config_path()))
    for name in sorted(profiles):
        p = profiles[name]
        out("  %s %s  url: %s  key: %s  mode: %s%s"
            % ("*" if name == default else " ", name,
               p.get("url") or "-",
               "已配置" if p.get("key") else "缺失",
               normalize_mode(p.get("mode")),
               "  [insecure]" if p.get("insecure") else ""))
    out("切换: 加 --server <名字>；未指定时用 default（非 --server 场景下环境变量 REDMINE_URL/REDMINE_API_KEY 优先）")
    out("mode: readonly 只读 / confirm 每次写操作需加 --confirm / readwrite 可读写（缺省）")


def cmd_projects(rm, a):
    ps = list_projects(rm)
    out("共 %d 个项目:" % len(ps))
    for p in ps:
        out("  #%s %s (identifier: %s)" % (p.get("id"), p.get("name"), p.get("identifier")))


def cmd_trackers(rm, a):
    out("trackers: " + " | ".join("#%s %s" % (t["id"], t["name"]) for t in list_trackers(rm)))


def cmd_statuses(rm, a):
    out("状态:")
    for s in list_statuses(rm):
        out("  #%s %s%s" % (s["id"], s["name"], " [关闭态]" if s.get("is_closed") else ""))


def cmd_priorities(rm, a):
    out("优先级: " + " | ".join(
        "#%s %s%s" % (p["id"], p["name"], " (默认)" if p.get("is_default") else "")
        for p in list_priorities(rm)))


def cmd_activities(rm, a):
    out("工时活动:")
    for x in list_activities(rm):
        out("  #%s %s%s" % (x["id"], x["name"], " (默认)" if x.get("is_default") else ""))


def cmd_issues(rm, a):
    params = {"limit": min(max(a.limit, 1), 100), "sort": a.sort}
    if a.project:
        params["project_id"] = resolve_project(rm, a.project)
    if a.tracker:
        params["tracker_id"] = match_name(list_trackers(rm), a.tracker, "跟踪类型")
    if a.priority:
        params["priority_id"] = match_name(list_priorities(rm), a.priority, "优先级")
    if a.query:
        params["subject"] = "~" + a.query
    if a.assignee:
        params["assigned_to_id"] = "me" if a.assignee.strip().lower() == "me" else resolve_assignee(rm, a.assignee)
    if a.status:
        v = a.status.strip().lower()
        if v in ("open", "closed"):
            params["status_id"] = v
        elif v in ("*", "all"):
            params["status_id"] = "*"
        else:
            params["status_id"] = match_name(list_statuses(rm), a.status, "状态")
    data = rm.req("GET", "/issues.json", params)
    if a.json:
        out(json.dumps(data, ensure_ascii=False, indent=2))
        return
    issues = data.get("issues", [])
    if not issues:
        out("无匹配工单（默认只查 open 状态；试试 --status all 或放宽筛选）")
        return
    offset = data.get("offset", 0)
    out("共 %s 条，显示 %d-%d（默认仅 open，全部用 --status all）:"
        % (data.get("total_count", len(issues)), offset + 1, offset + len(issues)))
    for i in issues:
        out("  " + fmt_issue_line(i))


def cmd_issue(rm, a):
    data = rm.req("GET", "/issues/%s.json" % a.id, {"include": "journals"})
    if a.json:
        out(json.dumps(data, ensure_ascii=False, indent=2))
        return
    i = data.get("issue", {})
    out("#%s %s" % (i.get("id"), i.get("subject")))
    out("状态: %s | 跟踪: %s | 优先级: %s | 项目: %s" % (
        (i.get("status") or {}).get("name", "-"),
        (i.get("tracker") or {}).get("name", "-"),
        (i.get("priority") or {}).get("name", "-"),
        (i.get("project") or {}).get("name", "-")))
    out("负责人: %s | 创建人: %s | 进度: %s%%" % (
        user_ref(i.get("assigned_to")), user_ref(i.get("author")), i.get("done_ratio", 0)))
    out("创建: %s | 更新: %s | 开始: %s | 截止: %s" % (
        str(i.get("created_on", "-"))[:10], str(i.get("updated_on", "-"))[:10],
        str(i.get("start_date") or "-"), str(i.get("due_date") or "-")))
    out("URL: %s/issues/%s" % (rm.base, i.get("id")))
    out("描述:")
    desc = i.get("description")
    out(indent(desc) if desc else "  (空)")
    journals = [j for j in i.get("journals", []) if j.get("notes")]
    out("评论 (%d):" % len(journals))
    for j in journals:
        out("  [%s %s] %s" % (str(j.get("created_on", ""))[:16].replace("T", " "),
                              user_ref(j.get("user")),
                              j.get("notes", "").replace("\n", "\n    ")))
    if not journals:
        out("  (无)")


def cmd_search(rm, a):
    data = rm.req("GET", "/search.json", {"q": a.q, "limit": min(max(a.limit, 1), 25)})
    results = data.get("results", [])
    if not results:
        out("没有与 %r 匹配的结果" % a.q)
        return
    out("%d 条结果:" % len(results))
    for r in results:
        out("  [%s] %s — %s (%s)" % (r.get("type"), r.get("title"), r.get("url"),
                                     str(r.get("datetime", ""))[:10]))


def guess_tracker(rm, subject, description):
    """未指定 --tracker 时按标题/描述关键词自动选择；都不中默认「支持」。返回 (id, name)。"""
    text = ("%s %s" % (subject, description or "")).lower()
    trackers = list_trackers(rm)
    by_name = {t["name"]: t["id"] for t in trackers}
    rules = [
        ("错误", ["bug", "缺陷", "报错", "错误", "异常", "崩溃", "闪退", "失败", "无法",
                  "不能", "不生效", "不正确", "无效", "错乱", "修复", "故障", "黑屏",
                  "白屏", "卡死", "卡顿", "死机", "泄露", "漏洞"]),
        ("需求变更", ["需求变更", "需求修改", "需求调整"]),
        ("功能", ["功能", "特性", "新增", "增加", "新需求", "开发", "适配", "迭代",
                  "建设", "实现", "优化", "增强", "完善"]),
    ]
    for name, kws in rules:
        if name in by_name and any(k in text for k in kws):
            return by_name[name], name
    if "支持" in by_name:
        return by_name["支持"], "支持"
    return (trackers[0]["id"], trackers[0]["name"]) if trackers else (None, None)


def cmd_create(rm, a):
    ensure_writable(rm, "create")
    issue = {"project_id": resolve_project(rm, a.project), "subject": a.subject}
    if a.description and a.description_file:
        die("--description 与 --description-file 只能用一个")
    if a.description:
        issue["description"] = a.description
    elif a.description_file:
        issue["description"] = read_text_file(a.description_file, "描述")
    tracker_name = None
    if a.tracker:
        trackers = list_trackers(rm)
        issue["tracker_id"] = match_name(trackers, a.tracker, "跟踪类型")
        tracker_name = name_of(trackers, issue["tracker_id"])
    else:
        tid, tracker_name = guess_tracker(rm, a.subject, issue.get("description"))
        if tid:
            issue["tracker_id"] = tid
    if a.priority:
        issue["priority_id"] = match_name(list_priorities(rm), a.priority, "优先级")
    if a.assignee:
        issue["assigned_to_id"] = resolve_assignee(rm, a.assignee)
    if a.status:
        issue["status_id"] = match_name(list_statuses(rm), a.status, "状态")
    preview = [
        "项目: %s（#%s）" % (a.project, issue["project_id"]),
        "标题: %s" % issue["subject"],
        "跟踪: %s" % (tracker_name or "-"),
    ]
    if "priority_id" in issue:
        preview.append("优先级: %s" % a.priority)
    if "assigned_to_id" in issue:
        preview.append("负责人: %s（id %s）" % (a.assignee, issue["assigned_to_id"]))
    if "status_id" in issue:
        preview.append("状态: %s" % a.status)
    if "description" in issue:
        preview.append("描述: %s" % short_text(issue["description"]))
    confirm_gate(rm, "create", a.confirm, preview)
    data = rm.req("POST", "/issues.json", payload={"issue": issue})
    i = data.get("issue", {})
    tname = (i.get("tracker") or {}).get("name") or tracker_name
    out("已创建工单 #%s — %s（跟踪: %s）" % (i.get("id"), i.get("subject"), tname or "-"))
    out("URL: %s/issues/%s" % (rm.base, i.get("id")))


def upload_tokens(rm, files):
    """上传文件取 token 列表（Redmine 附件两步流程：POST /uploads.json → PUT 携带 token）。"""
    tokens = []
    for path in files:
        if not os.path.isfile(path):
            die("文件不存在: %s" % path)
        name = os.path.basename(path)
        with open(path, "rb") as f:
            body = f.read()
        data = rm.req("POST", "/uploads.json", {"filename": name}, payload=body)
        token = (data.get("upload") or {}).get("token")
        if not token:
            die("上传失败 — 未取得 upload token: %s" % path)
        tokens.append({"token": token, "filename": name,
                       "content_type": "application/octet-stream"})
    return tokens


def read_text_file(path, label):
    """读取 UTF-8 文本文件作为长内容（兼容 BOM），规避 shell 对引号/多行的传参截断。"""
    if not os.path.isfile(path):
        die("%s文件不存在: %s" % (label, path))
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            return f.read()
    except OSError as e:
        die("读取失败: %s — %s" % (path, e))


def cmd_update(rm, a):
    ensure_writable(rm, "update")
    # 仅 confirm 预览时多取一次当前工单，用于展示字段新旧值
    cur = None
    if rm.mode == MODE_CONFIRM and not a.confirm:
        cur = rm.req("GET", "/issues/%s.json" % a.id).get("issue", {})
    issue = {}
    changes = []
    if a.status:
        statuses = list_statuses(rm)
        sid = match_name(statuses, a.status, "状态")
        issue["status_id"] = sid
        changes.append(("状态", old_value(cur, "status"), name_of(statuses, sid)))
    if a.tracker:
        trackers = list_trackers(rm)
        tid = match_name(trackers, a.tracker, "跟踪类型")
        issue["tracker_id"] = tid
        changes.append(("跟踪", old_value(cur, "tracker"), name_of(trackers, tid)))
    if a.priority:
        priorities = list_priorities(rm)
        pid = match_name(priorities, a.priority, "优先级")
        issue["priority_id"] = pid
        changes.append(("优先级", old_value(cur, "priority"), name_of(priorities, pid)))
    if a.assignee:
        uid = resolve_assignee(rm, a.assignee)
        issue["assigned_to_id"] = uid
        changes.append(("负责人", old_value(cur, "assigned_to"), "%s（id %s）" % (a.assignee, uid)))
    if a.progress is not None:
        if not 0 <= a.progress <= 100:
            die("--progress 取值 0-100")
        issue["done_ratio"] = a.progress
        changes.append(("进度", old_value(cur, "done_ratio"), "%s%%" % a.progress))
    if a.subject:
        issue["subject"] = a.subject
        changes.append(("标题", old_value(cur, "subject"), a.subject))
    if a.description and a.description_file:
        die("--description 与 --description-file 只能用一个")
    if a.description is not None:
        issue["description"] = a.description
        changes.append(("描述", short_text(old_value(cur, "description")), short_text(a.description)))
    elif a.description_file:
        text = read_text_file(a.description_file, "描述")
        issue["description"] = text
        changes.append(("描述", short_text(old_value(cur, "description")), short_text(text)))
    if a.notes and a.notes_file:
        die("-m 与 --notes-file 只能用一个")
    notes = a.notes
    if not notes and a.notes_file:
        notes = read_text_file(a.notes_file, "评论")
    if notes:
        issue["notes"] = notes
        changes.append(("评论", "-", short_text(notes)))
    if a.file:
        changes.append(("附件", "-", "、".join(os.path.basename(p) for p in a.file)))
    if not issue:
        die("没有要更新的字段 — 至少传一个（--status/--notes/--priority/--assignee/--progress/--subject/--description/--tracker/--file）")
    confirm_gate(rm, "update", a.confirm,
                 ["工单: #%s %s" % (a.id, (cur or {}).get("subject", ""))]
                 + ["%s: %s → %s" % (label, old, new) for label, old, new in changes])
    if a.file:
        issue["uploads"] = upload_tokens(rm, a.file)
    rm.req("PUT", "/issues/%s.json" % a.id, payload={"issue": issue})
    extra = []
    if a.notes:
        extra.append("含新评论")
    if a.file:
        extra.append("含 %d 个附件" % len(a.file))
    out("已更新工单 #%s%s" % (a.id, "（%s）" % "、".join(extra) if extra else ""))


def cmd_note(rm, a):
    ensure_writable(rm, "note")
    notes = a.notes
    if a.notes_file:
        if notes:
            die("-m 与 --notes-file 只能用一个")
        notes = read_text_file(a.notes_file, "评论")
    if not notes or not notes.strip():
        die("评论内容为空 — 用 -m 短评论或 --notes-file 文件提供")
    preview = ["工单: #%s" % a.id, "评论: %s" % short_text(notes)]
    if a.file:
        preview.append("附件: %s" % "、".join(os.path.basename(p) for p in a.file))
    confirm_gate(rm, "note", a.confirm, preview)
    issue = {"notes": notes}
    if a.file:
        issue["uploads"] = upload_tokens(rm, a.file)
    rm.req("PUT", "/issues/%s.json" % a.id, payload={"issue": issue})
    extra = []
    if a.file:
        extra.append("上传 %d 个附件" % len(a.file))
    if a.notes_file:
        extra.append("内容来自文件")
    out("已在工单 #%s 添加评论%s" % (a.id, "（%s）" % "、".join(extra) if extra else ""))


def cmd_close(rm, a):
    ensure_writable(rm, "close")
    statuses = list_statuses(rm)
    if a.status:
        sid = match_name(statuses, a.status, "状态")
    else:
        closed = [s for s in statuses if s.get("is_closed")]
        if not closed:
            die("服务器未定义任何关闭态状态 — 用 update --status <名称> 指定")
        sid = closed[0]["id"]
    preview = ["工单: #%s" % a.id, "状态: %s" % name_of(statuses, sid)]
    if a.notes:
        preview.append("评论: %s" % short_text(a.notes))
    confirm_gate(rm, "close", a.confirm, preview)
    issue = {"status_id": sid}
    if a.notes:
        issue["notes"] = a.notes
    rm.req("PUT", "/issues/%s.json" % a.id, payload={"issue": issue})
    out("已关闭工单 #%s (status_id=%s)%s" % (a.id, sid, "，并附评论" if a.notes else ""))


def cmd_log(rm, a):
    ensure_writable(rm, "log")
    entry = {"issue_id": as_int(a.id), "hours": a.hours}
    activity_name = None
    if a.activity:
        acts = list_activities(rm)
        entry["activity_id"] = match_name(acts, a.activity, "工时活动")
        activity_name = name_of(acts, entry["activity_id"])
    else:
        acts = list_activities(rm)
        default = next((x for x in acts if x.get("is_default")), acts[0] if acts else None)
        if default:
            entry["activity_id"] = default["id"]
            activity_name = default.get("name")
    if a.comments:
        entry["comments"] = a.comments
    if a.spent_on:
        entry["spent_on"] = a.spent_on
    preview = ["工单: #%s" % a.id,
               "工时: %s 小时" % a.hours,
               "活动: %s" % (activity_name or "-")]
    if a.comments:
        preview.append("说明: %s" % short_text(a.comments))
    if a.spent_on:
        preview.append("日期: %s" % a.spent_on)
    confirm_gate(rm, "log", a.confirm, preview)
    rm.req("POST", "/time_entries.json", payload={"time_entry": entry})
    out("已给工单 #%s 记录工时 %s 小时" % (a.id, a.hours))


def cmd_attach(rm, a):
    ensure_writable(rm, "attach")
    if not os.path.isfile(a.file):
        die("文件不存在: %s" % a.file)
    name = a.name or os.path.basename(a.file)
    preview = ["工单: #%s" % a.id,
               "附件: %s（%d 字节）" % (name, os.path.getsize(a.file))]
    if a.description:
        preview.append("说明: %s" % short_text(a.description))
    confirm_gate(rm, "attach", a.confirm, preview)
    entry = upload_tokens(rm, [a.file])[0]
    if a.name:
        entry["filename"] = a.name
    if a.type:
        entry["content_type"] = a.type
    if a.description:
        entry["description"] = a.description
    rm.req("PUT", "/issues/%s.json" % a.id, payload={"issue": {"uploads": [entry]}})
    out("已上传附件 %s 到工单 #%s" % (entry["filename"], a.id))


def cmd_watchers(rm, a):
    data = rm.req("GET", "/issues/%s.json" % a.id, {"include": "watchers"})
    ws = (data.get("issue") or {}).get("watchers", [])
    if not ws:
        out("工单 #%s 没有跟踪者" % a.id)
        return
    out("工单 #%s 跟踪者 (%d):" % (a.id, len(ws)))
    for w in ws:
        out("  #%s %s" % (w.get("id"), w.get("name")))


def cmd_watch(rm, a):
    ensure_writable(rm, "watch")
    if not a.add and not a.remove:
        die("请指定操作 — --add 添加跟踪者 / --remove 移除跟踪者（me / 用户id / 姓名）")
    preview = ["工单: #%s" % a.id]
    if a.add:
        preview.append("添加跟踪者: %s" % a.add)
    if a.remove:
        preview.append("移除跟踪者: %s" % a.remove)
    confirm_gate(rm, "watch", a.confirm, preview)
    if a.add:
        uid = resolve_assignee(rm, a.add)
        rm.req("POST", "/issues/%s/watchers.json" % a.id, payload={"user_id": uid})
        out("已添加跟踪者 %s 到工单 #%s" % (a.add, a.id))
    if a.remove:
        uid = resolve_assignee(rm, a.remove)
        rm.req("DELETE", "/issues/%s/watchers/%s.json" % (a.id, uid))
        out("已从工单 #%s 移除跟踪者 %s" % (a.id, a.remove))


# ---------------------------------------------------------------- 入口

def build_parser():
    p = argparse.ArgumentParser(
        prog="redmine.py",
        description="Redmine 命令行 — 提票/回票/查票/更新状态/记工时"
                    "（凭据: --url/--key、--server <名字> 任意位置，"
                    "或环境变量 REDMINE_URL/REDMINE_API_KEY，或 ~/.redmine/config.json；"
                    "写权限: --mode readonly/readwrite/confirm 或 REDMINE_MODE，"
                    "confirm 模式的写命令需加 --confirm 才真正提交）")
    sub = p.add_subparsers(dest="cmd", metavar="command")
    sub.required = True

    s = sub.add_parser("test", help="验证服务器与 API key")
    s.set_defaults(func=cmd_test)

    s = sub.add_parser("servers", help="列出配置文件中的服务器（不联网，不显示 key）")
    s.set_defaults(func=cmd_servers)

    for name, func, help_ in (
            ("projects", cmd_projects, "列出项目"),
            ("trackers", cmd_trackers, "列出跟踪类型"),
            ("statuses", cmd_statuses, "列出工单状态"),
            ("priorities", cmd_priorities, "列出优先级"),
            ("activities", cmd_activities, "列出工时活动")):
        s = sub.add_parser(name, help=help_)
        s.set_defaults(func=func)

    s = sub.add_parser("issues", help="查询工单列表")
    s.add_argument("--project", help="项目 id/名称/identifier")
    s.add_argument("--status", help="open/closed/all 或状态名称/id（默认 open）")
    s.add_argument("--tracker")
    s.add_argument("--priority")
    s.add_argument("--assignee", help="me / 用户id / 姓名")
    s.add_argument("--query", help="标题关键词（模糊匹配）")
    s.add_argument("--limit", type=int, default=25)
    s.add_argument("--sort", default="id:desc", help="如 updated_on:desc")
    s.add_argument("--json", action="store_true", help="输出原始 JSON")
    s.set_defaults(func=cmd_issues)

    s = sub.add_parser("issue", help="查看单个工单详情（含评论）")
    s.add_argument("id")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_issue)

    s = sub.add_parser("search", help="全站全文搜索")
    s.add_argument("--q", required=True)
    s.add_argument("--limit", type=int, default=10)
    s.set_defaults(func=cmd_search)

    s = sub.add_parser("create", help="提票（新建工单）")
    s.add_argument("--project", required=True, help="项目 id/名称/identifier")
    s.add_argument("--subject", required=True)
    s.add_argument("--description")
    s.add_argument("--description-file", dest="description_file",
                   help="描述长内容从 UTF-8 文件读入（多行/含双引号时用，避免 PowerShell 截断）")
    s.add_argument("--tracker")
    s.add_argument("--priority")
    s.add_argument("--assignee", help="me / 用户id / 姓名")
    s.add_argument("--status")
    s.set_defaults(func=cmd_create)

    s = sub.add_parser("update", help="更新工单字段/状态")
    s.add_argument("id")
    s.add_argument("--status")
    s.add_argument("-m", "--notes", help="追加评论")
    s.add_argument("--tracker")
    s.add_argument("--priority")
    s.add_argument("--assignee", help="me / 用户id / 姓名")
    s.add_argument("--progress", type=int, help="完成度 0-100")
    s.add_argument("--subject")
    s.add_argument("--description")
    s.add_argument("--notes-file", dest="notes_file", help="评论长内容从 UTF-8 文件读入（与 -m 互斥）")
    s.add_argument("--description-file", dest="description_file", help="描述长内容从 UTF-8 文件读入（与 --description 互斥）")
    s.add_argument("--file", action="append", help="附件文件路径，可重复传多个")
    s.set_defaults(func=cmd_update)

    s = sub.add_parser("note", help="回票（只加评论）")
    s.add_argument("id")
    s.add_argument("-m", "--notes", help="短评论（与 --notes-file 二选一）")
    s.add_argument("--notes-file", dest="notes_file",
                   help="评论长内容从 UTF-8 文件读入（多行/含双引号时必用，避免 PowerShell 截断）")
    s.add_argument("--file", action="append", help="附件文件路径，可重复传多个")
    s.set_defaults(func=cmd_note)

    s = sub.add_parser("close", help="关闭工单")
    s.add_argument("id")
    s.add_argument("-m", "--notes")
    s.add_argument("--status", help="指定关闭态名称（默认取第一个关闭态）")
    s.set_defaults(func=cmd_close)

    s = sub.add_parser("log", help="记工时")
    s.add_argument("id")
    s.add_argument("--hours", type=float, required=True)
    s.add_argument("--activity", help="工时活动名称/id（默认取服务器默认活动）")
    s.add_argument("--comments")
    s.add_argument("--spent-on", dest="spent_on", help="YYYY-MM-DD，默认今天")
    s.set_defaults(func=cmd_log)

    s = sub.add_parser("attach", help="上传附件到工单")
    s.add_argument("id")
    s.add_argument("--file", required=True, help="本地文件路径")
    s.add_argument("--name", help="附件名（默认取文件名）")
    s.add_argument("--type", help="Content-Type，默认 application/octet-stream")
    s.add_argument("--description")
    s.set_defaults(func=cmd_attach)

    s = sub.add_parser("watchers", help="查看工单跟踪者")
    s.add_argument("id")
    s.set_defaults(func=cmd_watchers)

    s = sub.add_parser("watch", help="添加/移除跟踪者")
    s.add_argument("id")
    s.add_argument("--add", help="添加跟踪者（me / 用户id / 姓名）")
    s.add_argument("--remove", help="移除跟踪者（me / 用户id / 姓名）")
    s.set_defaults(func=cmd_watch)

    return p


def config_path():
    return os.path.join(os.path.expanduser("~"), ".redmine", "config.json")


def load_config():
    """读取 ~/.redmine/config.json（存在时），用于凭据持久化配置。"""
    try:
        # utf-8-sig: 兼容记事本等编辑器写入的 BOM
        with open(config_path(), "r", encoding="utf-8-sig") as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        return {}
    return cfg if isinstance(cfg, dict) else {}


def config_profiles(cfg):
    """把配置归一化为 ({名字: {url,key,insecure}}, 默认名字)。

    - 新格式: {"servers": {"work": {...}}, "default": "work"}
    - 旧格式: {"url": ..., "key": ...} 视为名为 default 的服务器
    """
    profiles = {}
    servers = cfg.get("servers")
    if isinstance(servers, dict):
        for name, item in servers.items():
            if isinstance(item, dict):
                profiles[str(name)] = item
    legacy = {k: cfg[k] for k in ("url", "key", "insecure") if k in cfg}
    if legacy:
        profiles.setdefault("default", legacy)
    default = cfg.get("default")
    if default not in profiles:
        default = "default" if "default" in profiles else next(iter(profiles), None)
    return profiles, default


def extract_globals(argv):
    """把 --url/--key/--server/--mode/--insecure/--confirm 从任意位置抽出来。"""
    url = key = server = mode = None
    insecure = False
    confirm = False
    rest = []
    i = 0
    while i < len(argv):
        t = argv[i]
        if t == "--url" and i + 1 < len(argv):
            url = argv[i + 1]; i += 2
        elif t == "--key" and i + 1 < len(argv):
            key = argv[i + 1]; i += 2
        elif t == "--server" and i + 1 < len(argv):
            server = argv[i + 1]; i += 2
        elif t == "--mode" and i + 1 < len(argv):
            mode = argv[i + 1]; i += 2
        elif t == "--insecure":
            insecure = True; i += 1
        elif t == "--confirm":
            confirm = True; i += 1
        elif t.startswith("--url="):
            url = t.split("=", 1)[1]; i += 1
        elif t.startswith("--key="):
            key = t.split("=", 1)[1]; i += 1
        elif t.startswith("--server="):
            server = t.split("=", 1)[1]; i += 1
        elif t.startswith("--mode="):
            mode = t.split("=", 1)[1]; i += 1
        else:
            rest.append(t); i += 1
    return url, key, server, insecure, mode, confirm, rest


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    url, key, server, insecure, cli_mode, confirm, rest = extract_globals(argv)
    a = build_parser().parse_args(rest)
    a.confirm = confirm
    profiles, default = config_profiles(load_config())
    env_url = os.environ.get("REDMINE_URL")
    env_key = os.environ.get("REDMINE_API_KEY")
    env_mode = os.environ.get("REDMINE_MODE")
    env_insecure = os.environ.get("REDMINE_INSECURE", "").strip().lower() in ("1", "true", "yes")
    if server:
        if server not in profiles:
            die("未知服务器 %r — 已配置: %s（运行 servers 查看）"
                % (server, "、".join(sorted(profiles)) or "（无）"))
        p = profiles[server]
        # 显式 --server 时，该服务器配置优先于环境变量
        url = url or p.get("url") or env_url
        key = key or p.get("key") or env_key
        insecure = insecure or bool(p.get("insecure")) or env_insecure
        mode = cli_mode or p.get("mode") or env_mode
        server_name = server
    else:
        p = profiles.get(default) or {}
        url = url or env_url or p.get("url")
        key = key or env_key or p.get("key")
        insecure = insecure or env_insecure or bool(p.get("insecure"))
        mode = cli_mode or env_mode or p.get("mode")
        server_name = default
    rm = Redmine(url, key, insecure, normalize_mode(mode), server_name)
    try:
        a.func(rm, a)
    except RedmineError as e:
        die(e)


if __name__ == "__main__":
    main()
