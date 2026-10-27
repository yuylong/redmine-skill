#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Redmine CLI — 提票/回票/查票/更新状态/记工时（Redmine REST API，仅标准库）.

凭据来源（优先级从高到低）：
  1. 命令行参数（位置任意）: --url https://redmine.example.com  --key <API访问密钥>
  2. 环境变量: REDMINE_URL / REDMINE_API_KEY（自签名证书再加 REDMINE_INSECURE=1）
  3. 配置文件: ~/.redmine/config.json — {"url": "...", "key": "...", "insecure": true}
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


class Redmine:
    def __init__(self, url, key, insecure=False):
        if not url:
            die("missing server URL — 传 --url <SERVER>、设置环境变量 REDMINE_URL，"
                "或在 ~/.redmine/config.json 里配置 url")
        if not key:
            die("missing API key — 传 --key <KEY>、设置环境变量 REDMINE_API_KEY，"
                "或在 ~/.redmine/config.json 里配置 key"
                "（Redmine 网页端 我的账号 → API 访问密钥）")
        self.base = url.rstrip("/")
        self.key = key
        ctx = ssl.create_default_context()
        if insecure:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=ctx),
            urllib.request.HTTPHandler(),
        )

    def req(self, method, path, params=None, payload=None):
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


def cmd_create(rm, a):
    issue = {"project_id": resolve_project(rm, a.project), "subject": a.subject}
    if a.description:
        issue["description"] = a.description
    if a.tracker:
        issue["tracker_id"] = match_name(list_trackers(rm), a.tracker, "跟踪类型")
    if a.priority:
        issue["priority_id"] = match_name(list_priorities(rm), a.priority, "优先级")
    if a.assignee:
        issue["assigned_to_id"] = resolve_assignee(rm, a.assignee)
    if a.status:
        issue["status_id"] = match_name(list_statuses(rm), a.status, "状态")
    data = rm.req("POST", "/issues.json", payload={"issue": issue})
    i = data.get("issue", {})
    out("已创建工单 #%s — %s" % (i.get("id"), i.get("subject")))
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


def cmd_update(rm, a):
    issue = {}
    if a.status:
        issue["status_id"] = match_name(list_statuses(rm), a.status, "状态")
    if a.tracker:
        issue["tracker_id"] = match_name(list_trackers(rm), a.tracker, "跟踪类型")
    if a.priority:
        issue["priority_id"] = match_name(list_priorities(rm), a.priority, "优先级")
    if a.assignee:
        issue["assigned_to_id"] = resolve_assignee(rm, a.assignee)
    if a.progress is not None:
        if not 0 <= a.progress <= 100:
            die("--progress 取值 0-100")
        issue["done_ratio"] = a.progress
    if a.subject:
        issue["subject"] = a.subject
    if a.description is not None:
        issue["description"] = a.description
    if a.notes:
        issue["notes"] = a.notes
    if a.file:
        issue["uploads"] = upload_tokens(rm, a.file)
    if not issue:
        die("没有要更新的字段 — 至少传一个（--status/--notes/--priority/--assignee/--progress/--subject/--description/--tracker/--file）")
    rm.req("PUT", "/issues/%s.json" % a.id, payload={"issue": issue})
    extra = []
    if a.notes:
        extra.append("含新评论")
    if a.file:
        extra.append("含 %d 个附件" % len(a.file))
    out("已更新工单 #%s%s" % (a.id, "（%s）" % "、".join(extra) if extra else ""))


def cmd_note(rm, a):
    issue = {"notes": a.notes}
    if a.file:
        issue["uploads"] = upload_tokens(rm, a.file)
    rm.req("PUT", "/issues/%s.json" % a.id, payload={"issue": issue})
    out("已在工单 #%s 添加评论%s" % (a.id, "并上传 %d 个附件" % len(a.file) if a.file else ""))


def cmd_close(rm, a):
    if a.status:
        sid = match_name(list_statuses(rm), a.status, "状态")
    else:
        closed = [s for s in list_statuses(rm) if s.get("is_closed")]
        if not closed:
            die("服务器未定义任何关闭态状态 — 用 update --status <名称> 指定")
        sid = closed[0]["id"]
    issue = {"status_id": sid}
    if a.notes:
        issue["notes"] = a.notes
    rm.req("PUT", "/issues/%s.json" % a.id, payload={"issue": issue})
    out("已关闭工单 #%s (status_id=%s)%s" % (a.id, sid, "，并附评论" if a.notes else ""))


def cmd_log(rm, a):
    entry = {"issue_id": as_int(a.id), "hours": a.hours}
    if a.activity:
        entry["activity_id"] = match_name(list_activities(rm), a.activity, "工时活动")
    else:
        acts = list_activities(rm)
        default = next((x for x in acts if x.get("is_default")), acts[0] if acts else None)
        if default:
            entry["activity_id"] = default["id"]
    if a.comments:
        entry["comments"] = a.comments
    if a.spent_on:
        entry["spent_on"] = a.spent_on
    rm.req("POST", "/time_entries.json", payload={"time_entry": entry})
    out("已给工单 #%s 记录工时 %s 小时" % (a.id, a.hours))


def cmd_attach(rm, a):
    entry = upload_tokens(rm, [a.file])[0]
    if a.name:
        entry["filename"] = a.name
    if a.type:
        entry["content_type"] = a.type
    if a.description:
        entry["description"] = a.description
    rm.req("PUT", "/issues/%s.json" % a.id, payload={"issue": {"uploads": [entry]}})
    out("已上传附件 %s 到工单 #%s" % (entry["filename"], a.id))


# ---------------------------------------------------------------- 入口

def build_parser():
    p = argparse.ArgumentParser(
        prog="redmine.py",
        description="Redmine 命令行 — 提票/回票/查票/更新状态/记工时"
                    "（凭据: --url/--key 任意位置，或环境变量 REDMINE_URL/REDMINE_API_KEY）")
    sub = p.add_subparsers(dest="cmd", metavar="command")
    sub.required = True

    s = sub.add_parser("test", help="验证服务器与 API key")
    s.set_defaults(func=cmd_test)

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
    s.add_argument("--file", action="append", help="附件文件路径，可重复传多个")
    s.set_defaults(func=cmd_update)

    s = sub.add_parser("note", help="回票（只加评论）")
    s.add_argument("id")
    s.add_argument("-m", "--notes", required=True)
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

    return p


def load_config():
    """读取 ~/.redmine/config.json（存在时），用于凭据持久化配置。"""
    path = os.path.join(os.path.expanduser("~"), ".redmine", "config.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        return {}
    return cfg if isinstance(cfg, dict) else {}


def extract_globals(argv):
    """把 --url/--key/--insecure 从任意位置抽出来，交给环境变量逻辑合并。"""
    url = key = None
    insecure = False
    rest = []
    i = 0
    while i < len(argv):
        t = argv[i]
        if t == "--url" and i + 1 < len(argv):
            url = argv[i + 1]; i += 2
        elif t == "--key" and i + 1 < len(argv):
            key = argv[i + 1]; i += 2
        elif t == "--insecure":
            insecure = True; i += 1
        elif t.startswith("--url="):
            url = t.split("=", 1)[1]; i += 1
        elif t.startswith("--key="):
            key = t.split("=", 1)[1]; i += 1
        else:
            rest.append(t); i += 1
    return url, key, insecure, rest


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    url, key, insecure, rest = extract_globals(argv)
    a = build_parser().parse_args(rest)
    cfg = load_config()
    env_insecure = os.environ.get("REDMINE_INSECURE", "").strip().lower() in ("1", "true", "yes")
    rm = Redmine(url or os.environ.get("REDMINE_URL") or cfg.get("url"),
                 key or os.environ.get("REDMINE_API_KEY") or cfg.get("key"),
                 insecure or env_insecure or bool(cfg.get("insecure")))
    try:
        a.func(rm, a)
    except RedmineError as e:
        die(e)


if __name__ == "__main__":
    main()
