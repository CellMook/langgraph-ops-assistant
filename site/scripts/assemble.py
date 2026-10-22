#!/usr/bin/env python3
"""组装 MkDocs 站点源目录（幂等，可重复运行）。

职责：
1. 收集仓库内容到 ``site/src/``：教程单元 → ``course/NN.md``，项目文档 → ``project/*.md``，
   离线速查表 → ``reference/*.html``；重命名为 ASCII 短路径，URL 更友好；
2. 改写站内链接：映射页 → 新的相对路径；仓库文件/目录 → GitHub 绝对地址（锚点保留）；
3. 生成导航 ``site/nav.generated.yml``（由 ``mkdocs.yml`` 的 ``INHERIT`` 引入）、
   首页 ``index.md`` 与速查表页 ``reference/index.md``。

改写只作用于正文中的 Markdown 链接，逐行跳过 ``` / ~~~ 围栏代码块（含更长围栏嵌套）。

用法（在 ``site/`` 目录执行）::

    uv run python scripts/assemble.py          # 生成 src/ 与导航
    uv run python scripts/assemble.py --check  # 只校验源文件映射完备性，不写盘

单元编号取自文件名/标题；新增单元后重跑本脚本即可，导航自动更新。
"""

from __future__ import annotations

import argparse
import posixpath
import re
import shutil
import sys
from pathlib import Path, PurePosixPath

SITE_DIR = Path(__file__).resolve().parents[1]
REPO = SITE_DIR.parent
SRC = SITE_DIR / "src"

GITHUB = "https://github.com/CellMook/langgraph-ops-assistant"

#: 项目文档（仓库相对路径 → 站内路径）
DOC_PAGES: dict[str, str] = {
    "docs/architecture.md": "project/architecture.md",
    "docs/audit-fixes-2026-10-20.md": "project/audit-fixes.md",
    "docs/demo-script.md": "project/demo-script.md",
    "docs/interview.md": "project/interview.md",
    "docs/limitations.md": "project/limitations.md",
    "docs/publish-github.md": "project/publish-github.md",
    "docs/verification.md": "project/verification.md",
    "工程规格.md": "project/spec.md",
    "交付清单.md": "project/deliverables.md",
    "README.md": "project/repo-readme.md",
    "ops-assistant/README.md": "project/ops-readme.md",
    # 单文件草稿：仓库定位为“仅参考”，在线版保留该定位
    "LangGraph智能体开发完整教程.md": "project/draft.md",
}

#: 目录链接（仓库相对 → 站内页）；未列出的目录一律跳转 GitHub
DIR_LINKS: dict[str, str] = {"教程": "course/index.md"}

#: 课程阶段划分（用于导航分组；编号含两端）
STAGES: list[tuple[str, int, int]] = [
    ("阶段一 · 起步", 0, 1),
    ("阶段二 · 单 Agent", 2, 5),
    ("阶段三 · 图编排", 6, 9),
    ("阶段四 · 知识与数据", 10, 14),
    ("阶段五 · 生产化", 15, 18),
    ("阶段六 · 交付", 19, 21),
]

_UNIT_H1 = re.compile(r"^#\s*单元\s*(\d+)(?:（[^）]*）)?[：:]\s*(.+?)\s*$")
_LINK = re.compile(r"\[([^\]]*)\]\(([^)\s]+)(#[^)]*)?\)")
_FENCE = re.compile(r"^(\s*)(`{3,}|~{3,})")


def relative_link(from_dest: str, to_dest: str) -> str:
    """从 from_dest 页面指向 to_dest 页面的相对链接。

    直接给出目标 .md 路径（如 ``../course/00.md``），由 MkDocs 在构建时
    改写为 use_directory_urls 风格的 ``../course/00/``——这是链接校验器
    唯一认可的写法，尾斜杠形式会被判为 unrecognized。
    """
    base = posixpath.dirname(from_dest)
    return posixpath.relpath(to_dest, base or ".")


def fence_toggle(fence: str | None, line: str) -> str | None:
    """按 CommonMark 语义维护围栏状态：足够长的同字符围栏才关闭，否则视为开启/内容。"""
    match = _FENCE.match(line)
    if not match:
        return fence
    marker = match.group(2)
    if fence is None:
        return marker
    if marker[0] == fence[0] and len(marker) >= len(fence):
        return None
    return fence


def rewrite_links(text: str, src_repo_path: str, current_dest: str, link_map: dict[str, str]) -> tuple[str, int]:
    """改写正文 Markdown 链接（跳过围栏代码块）；返回 (新文本, 改写数)。"""
    src_dir = posixpath.dirname(src_repo_path)
    state = {"count": 0}

    def replace(match: re.Match) -> str:
        label, target, anchor = match.group(1), match.group(2), match.group(3) or ""
        if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", target):  # http(s)/mailto 等绝对链接
            return match.group(0)
        if target.startswith("#"):  # 同页锚点，保持原样
            return match.group(0)
        repo_rel = posixpath.normpath(posixpath.join(src_dir, target))
        if repo_rel.startswith("./"):
            repo_rel = repo_rel[2:]
        dest = link_map.get(repo_rel) or DIR_LINKS.get(repo_rel)
        if dest is not None:
            url = relative_link(current_dest, dest)
        else:
            gh_kind = "tree" if not PurePosixPath(repo_rel).suffix else "blob"
            url = f"{GITHUB}/{gh_kind}/main/{repo_rel}"
        state["count"] += 1
        return f"[{label}]({url}{anchor})"

    out: list[str] = []
    fence: str | None = None
    for line in text.splitlines(keepends=True):
        fence = fence_toggle(fence, line)
        if fence is None:
            line = _LINK.sub(replace, line)
        out.append(line)
    return "".join(out), state["count"]


def collect_units() -> list[tuple[str, str, str, str]]:
    """扫描 教程/，返回 [(编号, 仓库路径, 站内路径, 标题)]，按编号排序。"""
    units: list[tuple[str, str, str, str]] = []
    for path in sorted((REPO / "教程").glob("*.md")):
        if path.name == "README.md":
            continue
        first_line = path.read_text(encoding="utf-8").splitlines()[0]
        match = _UNIT_H1.match(first_line)
        if not match:
            raise SystemExit(f"无法识别单元标题：{path}（首行应为「# 单元 NN：标题」）")
        num, title = match.group(1), match.group(2)
        if f"course/{num}.md" in {dest for _, _, dest, _ in units}:
            raise SystemExit(f"单元编号重复：{num}（{path}）")
        units.append((num, f"教程/{path.name}", f"course/{num}.md", title))
    return sorted(units, key=lambda item: int(item[0]))


def build_link_map(units: list[tuple[str, str, str, str]]) -> dict[str, str]:
    mapping = {"教程/README.md": "course/index.md"}
    mapping.update({src: dest for _, src, dest, _ in units})
    mapping.update(DOC_PAGES)
    return mapping


def render_landing(units: list[tuple[str, str, str, str]]) -> str:
    stage_rows = "\n".join(
        f"| {name} | 单元 {lo:02d}–{hi:02d} | [从 {lo:02d} 开始](course/{lo:02d}.md) |"
        for name, lo, hi in STAGES
    )
    return f"""# LangGraph 智能体开发教程 · 在线阅读

以「经营数据分析与知识库智能助手」项目为主线的 LangChain / LangGraph 中文教程：
**{len(units)} 个单元**（22 基础单元 + 流式输出附录 + 契约与版本化验收），
学完的产物只有一个——你能独立讲清楚、改得动、测得了、演示得出的完整项目。

**[开始学习 → 单元 00](course/00.md){{ .md-button .md-button--primary }}**
**[课程总览与依赖图](course/index.md){{ .md-button }}**

## 这套教程怎么用

1. **按阶段推进**：每个单元 30–60 分钟（单元 23 约 90 分钟），有明确的可勾选验收清单；
2. **在线读概念、本地跑实验**：每个单元第 5 节给出完整代码与保存路径，配套 23 个已实测的
   实验脚本位于[仓库的 教程/实验/ 目录]({GITHUB}/tree/main/教程/实验)；
3. **诚实声明**：演示数据由固定随机种子生成，所有数字由程序计算，模型只影响措辞；
   当前验证状态见[验证记录](project/verification.md)。

## 内容地图

| 阶段 | 单元 | 入口 |
| --- | --- | --- |
{stage_rows}
| 收尾 · 附录与验收 | 22–23 | [附录：流式输出](course/22.md) · [契约与版本化验收](course/23.md) |

## 配套项目与文档

- [工程规格](project/spec.md)：接口、口径与验收标准的单一事实源
- [交付清单](project/deliverables.md)：每个文件的用途与状态
- [架构说明](project/architecture.md) · [已知限制](project/limitations.md) · [修复记录](project/audit-fixes.md)
- [离线速查表](reference/index.md)：LangChain / LangGraph 双份单页速查
- [仓库首页]({GITHUB})：源码、CI 与本地运行说明
"""


def render_reference() -> str:
    frame = (
        '<iframe src="{name}" style="width:100%;height:760px;border:1px solid '
        'var(--md-default-fg-color--lightest);border-radius:8px" title="{title}"></iframe>'
    )
    return (
        "# 离线速查表\n\n"
        "两份单页速查表随仓库分发（`reference/` 目录），可在此内嵌阅读，"
        f"或[在 GitHub 上打开]({GITHUB}/tree/main/reference)。\n\n"
        "## LangChain 速查表\n\n" + frame.format(name="langchain-cheatsheet.html", title="LangChain 速查表") + "\n\n"
        "## LangGraph 速查表\n\n" + frame.format(name="langgraph-cheatsheet.html", title="LangGraph 速查表") + "\n"
    )


def write_nav(units: list[tuple[str, str, str, str]]) -> None:
    lines = ["nav:", "  - 首页: index.md", "  - 教程:", "      - 课程总览: course/index.md"]
    for name, lo, hi in STAGES:
        lines.append(f"      - {name}:")
        for num, _src, dest, title in units:
            if lo <= int(num) <= hi:
                lines.append(f'          - "{num} · {title}": {dest}')
    extras = [(num, dest, title) for num, _src, dest, title in units if int(num) >= 22]
    if extras:
        lines.append("      - 收尾 · 附录与验收:")
        for num, dest, title in extras:
            lines.append(f'          - "{num} · {title}": {dest}')
    lines += [
        "  - 项目文档:",
        "      - 工程规格: project/spec.md",
        "      - 交付清单: project/deliverables.md",
        "      - 仓库 README: project/repo-readme.md",
        "      - 项目 README（ops-assistant）: project/ops-readme.md",
        "      - 架构说明: project/architecture.md",
        "      - 验证记录: project/verification.md",
        "      - 修复记录: project/audit-fixes.md",
        "      - 已知限制: project/limitations.md",
        "      - 演示脚本: project/demo-script.md",
        "      - 面试准备: project/interview.md",
        "      - 发布指南: project/publish-github.md",
        "      - 单文件草稿（仅参考）: project/draft.md",
        "  - 离线速查表: reference/index.md",
    ]
    (SITE_DIR / "nav.generated.yml").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="组装教程站点的 src/ 与导航")
    parser.add_argument("--check", action="store_true", help="只校验源文件映射完备性，不写盘")
    args = parser.parse_args(argv)

    units = collect_units()
    link_map = build_link_map(units)

    sources = ["教程/README.md", *(src for _, src, _, _ in units), *DOC_PAGES]
    missing = [src for src in sources if not (REPO / src).is_file()]
    missing += [f"reference/{p.name}" for p in [(REPO / "reference" / n) for n in ("langchain-cheatsheet.html", "langgraph-cheatsheet.html")] if not p.is_file()]
    if missing:
        print("缺失源文件：", missing, file=sys.stderr)
        return 2
    if args.check:
        print(f"映射完备：{len(units)} 个单元 + {len(DOC_PAGES)} 篇文档 + 2 份速查表")
        return 0

    if SRC.exists():
        shutil.rmtree(SRC)
    for sub in ("course", "project", "reference"):
        (SRC / sub).mkdir(parents=True)

    copies = [("教程/README.md", "course/index.md"), *((src, dest) for _, src, dest, _ in units), *DOC_PAGES.items()]
    total = 0
    for src_rel, dest_rel in copies:
        text = (REPO / src_rel).read_text(encoding="utf-8")
        new_text, count = rewrite_links(text, src_rel, dest_rel, link_map)
        total += count
        (SRC / dest_rel).write_text(new_text, encoding="utf-8")
        print(f"  {src_rel} → {dest_rel}（改写 {count} 链接）")

    for name in ("langchain-cheatsheet.html", "langgraph-cheatsheet.html"):
        shutil.copy2(REPO / "reference" / name, SRC / "reference" / name)
    (SRC / "index.md").write_text(render_landing(units), encoding="utf-8")
    (SRC / "reference" / "index.md").write_text(render_reference(), encoding="utf-8")
    write_nav(units)
    print(f"完成：{len(units)} 单元、{len(DOC_PAGES)} 文档，共改写 {total} 个链接 → {SRC}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
