---
name: redmine
description: File, query, reply to, and update Redmine issues and log time via REST API. Use when the user mentions Redmine or asks to 提票, 建票, 回票, 查票, 关单, or 记工时. Not for Jira or other trackers.
---

# Redmine Operations

用本 skill 目录下的 `scripts/redmine.py`（Python 3.8+，仅标准库，无需 pip）操作 Redmine 服务器。

## 凭据

服务器地址和 API Key，优先级从高到低：
- 参数 `--url <SERVER>` `--key <KEY>`（可在命令任意位置）
- 环境变量 `REDMINE_URL`、`REDMINE_API_KEY`（已设置就直接用，不要再问用户）
- 配置文件 `~/.redmine/config.json`，形如 `{"url": "...", "key": "...", "insecure": true}`（已配置就直接用，不要再问用户）

全都没有就向用户索要；API Key 在 Redmine 网页端「我的账号 → API 访问密钥」获取。不要把 key 写进 skill 目录、仓库或代码；输出时避免完整复述。

## 调用

```
python <skill目录>/scripts/redmine.py [--url ... --key ...] <command> [options]
```

Windows 下 `python` 不可用时改用 `py`。自签名证书服务器加 `--insecure`（或配置文件 `"insecure": true` / 环境变量 `REDMINE_INSECURE=1` 持久化）。

## 命令

| 命令 | 用途 |
|---|---|
| `test` | 验证连通性，显示当前用户 |
| `projects` / `trackers` / `statuses` / `priorities` / `activities` | 列出元数据 id 与名称 |
| `issues [--project P] [--status S] [--assignee A] [--tracker T] [--priority P] [--query Q] [--limit N] [--sort updated_on:desc]` | 查票；`--status` 支持 open/closed/all/名称/id，默认只列 open |
| `issue <id>` | 单票详情（描述 + 评论历史） |
| `search --q 关键词` | 全站搜索 |
| `create --project P --subject 标题 [--description 描述 \| --description-file 文件] [--tracker T] [--priority P] [--assignee A]` | 提票；--tracker 缺省时按内容关键词自动选择跟踪类型 |
| `update <id> [--status S] [-m 评论 \| --notes-file 文件] [--file 附件路径，可重复] [--priority P] [--assignee A] [--progress 0-100] [--subject S] [--description D \| --description-file 文件]` | 更新字段/状态/加评论/传附件（一次调用同时提交） |
| `note <id> (-m 评论 \| --notes-file 文件) [--file 附件路径，可重复]` | 回票（加评论，可同时传附件，一次调用同时提交） |
| `close <id> [-m 评论]` | 关单（自动使用服务器的关闭态） |
| `log <id> --hours 2 [--activity 活动] [--comments 说明] [--spent-on YYYY-MM-DD]` | 记工时 |
| `attach <id> --file 路径 [--name 附件名] [--description 描述]` | 上传附件到工单 |
| `watchers <id>` | 查看工单跟踪者 |
| `watch <id> (--add 用户 \| --remove 用户)` | 添加/移除跟踪者（me / 用户id / 姓名） |

项目/跟踪/优先级/状态/活动参数均接受数字 id 或名称（中英文均可，忽略大小写，支持子串匹配）。`--assignee` 支持 `me`、用户 id，或姓名（按姓名解析需要 key 有列出用户的权限，失败就改用数字 id）。

`create` 不带 `--tracker` 时按标题/描述关键词自动选择跟踪类型：bug/缺陷/报错/错误/异常/崩溃/无法/失败/修复等 → 「错误」；功能/特性/新增/开发/适配/建设/优化等 → 「功能」；需求变更 → 「需求变更」；都不命中 → 默认「支持」。识别结果显示在输出中，判断不对时用 `--tracker` 覆盖。

## 内容格式

Redmine 默认使用 Textile 格式（不是 Markdown）。回票评论与工单描述一律用 Textile 语法：
- 小节标题 `h3. 标题`（h2./h4. 类似，`h3.` 后必须有空格）
- 粗体 `*文字*`，链接 `"文字":https://...`，删除线 `-文字-`
- 无序列表 `* 条目`（二级 `** 条目`），有序列表 `# 条目`
- 不要写 `**加粗**`、`## 标题`、`[text](url)` 这类 Markdown 语法
- 长内容（多行、含 ASCII 双引号、Textile 表格）不要拼进命令行 — PowerShell 5.1 传参会因内容中的双引号截断参数（报 unrecognized arguments，内容丢失）。一律写入 UTF-8 文本文件，再用 `--notes-file`（评论）或 `--description-file`（描述）传入

## 工作流

1. 首次对接服务器：`test` → `projects` / `statuses` / `trackers` 摸清环境。各服务器状态名不同（如 新建/进行中/已关闭 或 New/In Progress/Closed），不要猜，先查。
2. 提票：确认项目与标题 → `create` → 把返回的工单 URL 告诉用户。
3. 回票：短评论 `note <id> -m "<内容>"`；长内容（多行/含双引号/表格）写 UTF-8 文件后 `note <id> --notes-file 文件`，需要带附件加 `--file 路径`。
4. 查票：`issues` 加筛选条件；`issue <id>` 看全量详情。
5. 改状态：`update <id> --status <名称>`；关单优先 `close <id>`。
6. 记工时：`log <id> --hours X`。

## 规则

- create/update/close 后把 `<server>/issues/<id>` 链接展示给用户。
- HTTP 422 是字段校验失败，Redmine 会列出错误字段 — 原样转告用户后修正参数，不要盲目重试。
- 错误输出以 `ERROR:` 开头（stderr）。`issues` / `issue` 加 `--json` 可取原始数据。
- 批量或破坏性操作（关单、删改多条）先向用户确认。
