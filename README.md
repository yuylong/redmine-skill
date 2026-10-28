# redmine-skill

> 让 AI 助手替你打理 Redmine：提票、回票、查票、关单、记工时，一句话搞定。

一个 [TRAE](https://www.trae.ai/) Agent Skill，通过 Redmine REST API 完成日常工单操作。核心是一个**零依赖**的 Python 命令行脚本（仅标准库，无需 pip 安装任何包），配合 `SKILL.md` 让 AI 助手能够稳定、正确地调用。

## 功能特性

- 提票、回票、查票、关单、记工时、上传附件、管理跟踪者，一站搞定
- 项目 / 跟踪类型 / 状态 / 优先级 / 负责人支持**名称模糊匹配**（中英文均可，如 `--status 进行中`、`--assignee me`）
- 一次调用可同时提交字段更新 + 评论 + 多个附件
- `create` 不指定跟踪类型时按标题/描述关键词自动选择（bug/报错 → 错误，功能/优化/建设 → 功能，需求变更 → 需求变更，默认支持）
- 长内容（多行、含双引号）写入 UTF-8 文件后经 `--notes-file` / `--description-file` 传入，规避 PowerShell 传参截断
- 跟踪者（Watchers）管理：查看工单跟踪者，支持 `me` / 用户 id / 姓名增删
- 兼容自签名证书服务器（`--insecure`）
- 凭据支持 命令行参数 / 环境变量 / 配置文件 三种方式，密钥不落盘到代码与仓库

## 安装

### 1. 放置 skill 文件

把本仓库克隆到 TRAE 的技能目录（以下路径以实际环境为准，也可以手动复制 `SKILL.md` 和 `scripts/`）：

```bash
git clone https://github.com/yuylong/redmine-skill.git ~/.trae/skills/redmine
```

环境要求：**Python 3.8+**，无任何第三方依赖。

### 2. 配置凭据

先在 Redmine 网页端「**我的账号 → API 访问密钥**」生成 API Key，然后任选一种方式配置：

**方式一：环境变量**

```bash
export REDMINE_URL="https://redmine.example.com"
export REDMINE_API_KEY="你的API访问密钥"
```

**方式二：配置文件** `~/.redmine/config.json`

```json
{
  "url": "https://redmine.example.com",
  "key": "你的API访问密钥",
  "insecure": false
}
```

**方式三：调用时传参**

```bash
python scripts/redmine.py --url https://redmine.example.com --key 你的KEY test
```

> 自签名证书的服务器加 `--insecure`（或配置文件 `"insecure": true` / 环境变量 `REDMINE_INSECURE=1`）。
>
> ⚠️ 请勿把 API Key 写进代码、skill 目录或任何仓库。

## 使用

### 对 AI 助手说（推荐）

安装本 skill 后，直接用自然语言驱动：

- 「查一下我名下所有进行中的工单」
- 「给 XX 项目提个票，标题是……，描述是……」
- 「给 #123 回票：问题已修复，附上日志」
- 「#123 关单，备注已完成」
- 「给 #123 记 2 小时工时」
- 「把张三加为 #123 的跟踪者」

### 直接调用脚本

```bash
python scripts/redmine.py test                                  # 验证连通性
python scripts/redmine.py projects                              # 列出项目
python scripts/redmine.py issues --assignee me --status open    # 查我的 open 工单
python scripts/redmine.py issue 123                             # 单票详情（含评论）
python scripts/redmine.py create --project 后台服务 --subject "登录失败" --description "现象：……"
python scripts/redmine.py note 123 -m "问题已修复" --file ./fix.log
python scripts/redmine.py update 123 --status 已解决 --priority 高 --progress 80
python scripts/redmine.py close 123 -m "已完成"
python scripts/redmine.py log 123 --hours 2 --comments "联调测试"
python scripts/redmine.py watchers 123                          # 查看跟踪者
python scripts/redmine.py watch 123 --add me                    # 添加跟踪者
python scripts/redmine.py watch 123 --remove zhangsan           # 移除跟踪者
```

> 多行长评论 / 长描述（尤其含 ASCII 双引号时）不要拼进命令行 — PowerShell 5.1 会截断参数。写入 UTF-8 文本文件后用 `--notes-file 评论.txt`（评论）或 `--description-file 描述.txt`（描述）传入。

## 命令一览

| 命令 | 用途 |
|---|---|
| `test` | 验证服务器与 API Key 连通性 |
| `projects` / `trackers` / `statuses` / `priorities` / `activities` | 列出项目与各类元数据 id |
| `issues` | 查票，支持 `--project` `--status` `--assignee` `--tracker` `--priority` `--query` `--limit` `--sort` |
| `issue <id>` | 单票详情（描述 + 评论历史） |
| `search --q 关键词` | 全站全文搜索 |
| `create` | 提票（`--project` `--subject` 必填；`--tracker` 缺省时按标题/描述关键词自动选择跟踪类型） |
| `update <id>` | 更新字段 / 状态 / 评论 / 附件（可一次全部提交；长评论可用 `--notes-file`） |
| `note <id>` | 回票（加评论，可同时传附件；长评论可用 `--notes-file`） |
| `close <id>` | 关单（自动选用服务器的关闭态状态） |
| `log <id>` | 记工时（`--hours` 必填，可选 `--activity` `--comments` `--spent-on`） |
| `attach <id>` | 上传附件到工单 |
| `watchers <id>` | 查看工单跟踪者 |
| `watch <id>` | 添加 / 移除跟踪者（`--add` / `--remove`，支持 `me` / 用户 id / 姓名） |

## 说明

- Redmine 工单默认使用 **Textile** 格式（不是 Markdown），回票与描述请使用 Textile 语法（`h3. 标题`、`*粗体*`、`"链接":https://...`）
- HTTP 422 表示字段校验失败，服务器会列出错误字段，按提示修正参数即可
- 批量或破坏性操作（关单、批量修改）请先与用户确认

## License

[BSD 3-Clause](LICENSE) © 2026 Yulong Yu
