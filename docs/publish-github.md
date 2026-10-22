# 发布到 GitHub

> **先说明一件事**：整理仓库的这台机器**无法启动任何进程**（`pwsh` 连 `Write-Output` 都返回 `0xC0000142`），
> 所以 `git init` / `git add` / `git push` **我一次都没有执行过**，下面所有命令都需要**你在自己的机器上**运行。
> 我完成的是：排除规则（`.gitignore` / `.gitattributes`）、README 与交付清单的同步、以及这份可自检的步骤。

## 0. 快速方式（推荐）

### 0.1 有 `gh`（GitHub CLI）且已登录

仓库根目录有 [publish.ps1](../publish.ps1)，它把下面第 3–6 步串起来，并在提交前做安全检查（发现 `.env`、`*.db`、`Academy课程/`、个人笔记就中止）。

```powershell
cd "D:\MySpace\LangChain学习路线"

# 先干跑，确认计划（不写任何东西）
pwsh -ExecutionPolicy Bypass -File .\publish.ps1 -DryRun

# 确认无误后正式执行（公开仓库）
pwsh -ExecutionPolicy Bypass -File .\publish.ps1 -RepoName langgraph-ops-assistant

# 私有仓库
pwsh -ExecutionPolicy Bypass -File .\publish.ps1 -RepoName langgraph-ops-assistant -Private
```

### 0.2 没有 `gh`（或提示「'gh' 不是内部或外部命令」）

`gh` 未安装、或装完没重开终端时，走**模式 B**：先在网页建空仓库，再把地址传给脚本。

```powershell
cd "D:\MySpace\LangChain学习路线"

# 1) 浏览器打开 https://github.com/new 建一个空仓库（不要勾 README / gitignore / license）
# 2) 把下面的地址换成你自己的，先干跑
pwsh -ExecutionPolicy Bypass -File .\publish.ps1 -RemoteUrl https://github.com/<你的用户名>/<仓库名>.git -DryRun

# 3) 正式执行
pwsh -ExecutionPolicy Bypass -File .\publish.ps1 -RemoteUrl https://github.com/<你的用户名>/<仓库名>.git
```

模式 B 不需要 `gh`，首次 push 会弹出 Git Credential Manager 让你登录 GitHub（或用 PAT 作为密码）。
如果确实想装 `gh`：`winget install --id GitHub.cli -e --source winget`，然后**关闭并重开终端**，再 `gh auth login`。

没有 PowerShell 7（`pwsh`）时，把命令里的 `pwsh` 换成 `powershell`；脚本已保存为 UTF-8 with BOM，Windows PowerShell 5.1 可直接解析。

> 脚本已在 Windows PowerShell 5.1 上用 `-DryRun` 验证通过（2026-10-20）；正式发布前仍建议先干跑确认计划。
> 如果脚本报错，直接走下面的手动步骤，两者等价。

## 1. 这次"整理"改了什么

| 动作 | 结果 | 原因 |
| --- | --- | --- |
| 新增根 `.gitignore` | 第三方课程、个人笔记、密钥、运行生成物、评估结果都不入库 | 版权 + 隐私 + 仓库体积 |
| 新增根 `.gitattributes` | 统一 LF，Markdown/Python/SQL 不再因 Windows 行尾产生整文件 diff | 减少 review 噪声 |
| 更新 `README.md` | 仓库地图去掉未入库目录；新增"这个仓库不包含什么" | 避免 GitHub 上出现死链 |
| 更新 `交付清单.md` | 目录树改为"公开仓库视角" | 与实际仓库一致 |
| 新增本文件 | 发布步骤 + 自检命令 + 故障排查 | 你只需要照着粘贴 |

**保持入库**（即使看起来"内部"）：`工程规格.md`、`LangGraph智能体开发完整教程.md`。
因为 `教程/` 与 `docs/verification.md` 多处引用它们，删掉会断链；旧教程已经明确标注"仅参考、不作验收证据"。

## 2. 上传前 30 秒确认（重要）

```powershell
cd "D:\MySpace\LangChain学习路线"
git --version
```

- 没有 git → 装 [Git for Windows](https://git-scm.com/download/win) 后重开终端。
- 可选：装了 [GitHub CLI](https://cli.github.com/) 的话 `gh --version`，第 5 步会更省事。

确认这三件事：

1. **没有真实密钥**：仓库里只有 `.env.example`（值全空）。检查：
   ```powershell
   Get-ChildItem -Recurse -Force -Include .env,*.pem,*.key | Select-Object FullName
   ```
   预期：没有输出（或只有 `.env.example`，它不会被这个命令匹配到）。
2. **没有数据库/导出/日志**：`ops-assistant/data/` 下当前只有 `knowledge/*.md`；
   即使你之前跑过 bootstrap，`.gitignore` 也会排除 `*.db`、`knowledge_index.json`、`exports/`、`logs/`。
3. **确认排除项**：`Academy课程/`（LangChain Academy 第三方 notebook，134 个文件）、`briefs/`、`MISSION.md`、`NOTES.md`、`RESOURCES.md` 不会入库。

> 如果你**就是想连 Academy 课程一起上传**：请把仓库设为 **private**，并删掉 `.gitignore` 顶部的
> `Academy课程/` 一行。第三方课程材料不适合放在公开仓库（版权与授权不明）。

## 3. 初始化仓库并提交

```powershell
cd "D:\MySpace\LangChain学习路线"

# 3.1 初始化（默认分支叫 main；若已存在 .git 会报错，可跳过这行）
git init -b main

# 3.2 如果这台机器第一次用 git，先配置身份（只需一次）
git config user.name  "你的名字"
git config user.email "你的邮箱@example.com"

# 3.3 暂存全部（.gitignore 会自动排除不该进的文件）
git add -A

# 3.4 自检：被暂存的文件里不应该出现下面这些（预期：没有输出）
git ls-files | Select-String -Pattern "Academy课程|^briefs/|MISSION\.md|NOTES\.md|RESOURCES\.md|\.env$|\.db$|knowledge_index\.json|data/exports|eval/results|__pycache__|\.venv"

# 3.5 看一眼将要提交的规模（文件数；不是行数统计）
(git ls-files).Count

# 3.6 提交
git commit -m "feat: 经营数据分析与知识库智能助手（LangGraph 工作流 + 22 单元教程）"
```

如果 3.4 有输出：**先别提交**，用 `git rm --cached <路径>` 把误入的文件移出暂存区，
再检查 `.gitignore` 规则是否覆盖它，然后重新 `git add -A`。

## 4. 在 GitHub 上创建空仓库

两种方式，任选其一。

### 方式 A：网页（最稳）

1. 打开 <https://github.com/new>；
2. Repository name：建议 `langgraph-ops-assistant`（或 `ops-assistant-course`）；
3. 选 **Public**（求职展示）或 **Private**；
4. **不要**勾选 "Add a README file / .gitignore / license"（否则会产生冲突提交）；
5. 点 Create repository，复制页面上的仓库地址。

### 方式 B：GitHub CLI（装了 gh 的话）

```powershell
cd "D:\MySpace\LangChain学习路线"
gh auth status                 # 未登录先 gh auth login
# 二选一：
gh repo create langgraph-ops-assistant --public  --source . --remote origin --push
gh repo create langgraph-ops-assistant --private --source . --remote origin --push
```

方式 B 会直接把第 5 步一起做完；做完就跳到第 6 步。

## 5. 关联远程并推送

```powershell
cd "D:\MySpace\LangChain学习路线"

# 把 <你的用户名> 和 <仓库名> 换成实际值
git remote add origin https://github.com/<你的用户名>/<仓库名>.git

# 如果默认分支不是 main（老版本 git）：
git branch -M main

git push -u origin main
```

首次推送会让你认证：

- **HTTPS**：用 Personal Access Token（GitHub → Settings → Developer settings → Tokens，勾 `repo`）当密码；
- **SSH**：先配好 key，再用 `git@github.com:<用户名>/<仓库名>.git` 作为地址。

## 6. 推送后验证（别只看"推送成功"）

```powershell
# 6.1 确认远程与分支
git remote -v
git branch -vv

# 6.2 确认工作区干净
git status --short        # 预期：没有输出

# 6.3 换一个目录重新 clone，验证"别人拿到的仓库"是完整的
cd $env:TEMP
git clone https://github.com/<你的用户名>/<仓库名>.git repo-check
cd repo-check
(Get-ChildItem -Recurse -File | Measure-Object).Count
Test-Path .\ops-assistant\src\ops_assistant\workflow\nodes.py    # 预期 True
Test-Path .\教程\21-求职材料、演示与复盘.md                        # 预期 True
Test-Path .\ops-assistant\data\knowledge\01_指标口径.md            # 预期 True
Test-Path .\uv.lock                                                # 预期 False（见下）
```

在 GitHub 页面上再确认三件事：

1. 根目录能看到 `README.md`、`教程/`、`docs/`、`ops-assistant/`；
2. **看不到** `Academy课程/`、`MISSION.md`、`.env`；
3. 点开 `ops-assistant/README.md`，相对链接可跳转。

## 7. 推送后再做两件事（提升可信度）

1. **复现已有锁文件**：在 `ops-assistant/` 执行 `uv sync --locked`。锁文件已提交，只有计划升级依赖时才重新解析，并复跑测试。
2. **按 `docs/verification.md` 复跑验收**，记录自己机器的环境和真实输出。仓库已有 Linux 实测记录；不要将其冒充自己的本机结果。远端 CI 状态以 Actions 为准。

## 8. 常见问题

| 现象 | 原因 | 解决 |
| --- | --- | --- |
| `'gh' 不是内部或外部命令` | gh 未安装，或安装后没重开终端 | 新开终端再 `where gh`；没有就用第 0.2 节模式 B（不需要 gh），或 `winget install --id GitHub.cli -e --source winget` 后重开终端 |
| `fatal: not a git repository` | 没在仓库根目录执行 | `cd "D:\MySpace\LangChain学习路线"` |
| `remote origin already exists` | 之前加过 | `git remote set-url origin <新地址>` |
| `Authentication failed` | 用账号密码走 HTTPS | 改用 PAT 或 SSH（见第 5 步） |
| 推送被拒 `non-fast-forward` | 远程已有提交（网页勾了 README） | `git pull --rebase origin main` 后再 push |
| 中文文件名在 GitHub 显示正常，但本地 `git status` 出现 `\344\270\255...` | 未开 `core.quotepath` | `git config core.quotepath false`（只是显示问题） |
| 整文件 diff 全是红绿 | 行尾被改 | `.gitattributes` 已处理；老仓库首次提交后一般稳定 |
| 超过 100 MB 单文件被拒 | GitHub 单文件限制 | 本仓库不该有；若误加，用 `git rm --cached` + 加入 `.gitignore` |
| 误提交了 `.env` | 规则前就 add 了 | 立刻换掉密钥 → `git rm --cached .env` → 提交；历史清理用 `git filter-repo`（不要只靠新提交删除） |
| 想追加 Academy 课程 | 之前排除了 | 删 `.gitignore` 顶部 `Academy课程/`，并确认仓库为 private |
| 想给仓库改名 | GitHub 上改名后地址变化 | 页面上改，然后 `git remote set-url origin <新地址>` |

## 9. 推荐仓库设置（面试向）

- **Description**：`LangGraph 经营数据分析助手：固定工作流 + 知识库检索 + SQLite 口径计算 + 人工审批（含 22 单元中文教程）`；
- **Topics**：`langchain`、`langgraph`、`fastapi`、`streamlit`、`sqlite`、`rag`、`human-in-the-loop`、`ai-agent`；
- 把仓库 **Pin** 到个人主页；
- 在 About 里填 demo 说明时，**不要**写"已实现 X 万条数据/高并发"这类没有证据的话——用 `docs/limitations.md` 的口径更稳。

## 11. 诚实声明

- 本文档的命令**没有在整理环境中执行过**（环境无法启动进程），因此可能受你的 git 版本、认证方式、网络代理影响；
- 第 3.4 与第 6 步的自检命令是刻意设计的——**它们比"push 成功"更能说明仓库是否干净**；
- 如果你执行时报错，把报错原文贴回来，我可以按错误类型给出针对性修法。
