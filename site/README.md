# 教程在线阅读站（site/）

仓库内容的静态阅读版，基于 [MkDocs Material](https://squidfunk.github.io/mkdocs-material/)，
发布到 GitHub Pages：<https://cellmook.github.io/langgraph-ops-assistant/>

## 设计原则

- **单一内容源**：站点不维护独立文案，全部页面由 `scripts/assemble.py` 从仓库内容生成
  （`教程/`、`docs/`、`reference/`、根级文档）。改教程 → 重跑脚本 → 站点同步，永不失同步。
- **仓库结构不动**：不为了建站移动或重命名任何源文件，映射与链接改写全部在生成阶段完成。
- **在线读概念、GitHub 看源码**：站内页面互相用相对链接；指向仓库文件/目录的链接一律
  改写为 GitHub 绝对地址（`blob`/`tree`），保证在线版永远指向最新 `main`。

## 目录

| 路径 | 内容 |
| --- | --- |
| `scripts/assemble.py` | 组装脚本：生成 `src/`（页面）与 `nav.generated.yml`（导航），改写站内链接 |
| `mkdocs.yml` | 主题与扩展配置（Material、mermaid、任务清单、代码复制、明暗切换） |
| `pyproject.toml` / `uv.lock` | 构建依赖（mkdocs + mkdocs-material），用 uv 管理 |
| `src/`、`nav.generated.yml`、`dist/` | **生成物**（已 gitignore，勿手改） |

## 本地预览

```bash
cd site
uv sync                        # 按 uv.lock 建环境
uv run python scripts/assemble.py
uv run mkdocs serve            # http://127.0.0.1:8000
uv run mkdocs build --strict   # CI 同款严格构建，零警告才通过
```

新增教程单元（`教程/NN-*.md`）后重跑 `assemble.py`，导航与阶段分组自动更新。

## 发布

`.github/workflows/pages.yml`：`main` 分支推送即构建部署到 GitHub Pages；
Pull Request 仅构建校验不部署。首次启用 Pages 需将 Source 设为
 **GitHub Actions**（`gh api -X POST repos/CellMook/langgraph-ops-assistant/pages -f build_type=workflow`）。
