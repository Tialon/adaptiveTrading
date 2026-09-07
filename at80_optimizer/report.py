"""
Optimizer Report(V9.0 M2.4) — Markdown 提案报告

把 ParamOptimizer.optimize() 的排名提案渲染为可读 markdown,
供人工审阅后决定是否 activate 某版本。
"""

from typing import Any


def render_report(proposals: list[dict[str, Any]], title: str = "策略优化提案") -> str:
    """渲染排名提案为 markdown 表格"""
    lines = [f"# {title}", ""]
    if not proposals:
        lines.append("无候选。")
        return "\n".join(lines)

    lines.append("| 排名 | 版本 | 得分 | 总收益 | 最大回撤 | 夏普 | 胜率 | 盈亏比 |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for rank, p in enumerate(proposals, 1):
        m = p.get("metrics", {})
        lines.append(
            f"| {rank} | `{p['version']}` | {p['score']:.4f} "
            f"| {m.get('total_return', 0.0):.4f} | {m.get('max_drawdown', 0.0):.4f} "
            f"| {m.get('sharpe', 0.0):.2f} | {m.get('win_rate', 0.0):.2f} "
            f"| {m.get('profit_factor', 0.0):.2f} |"
        )

    best = proposals[0]
    lines.append("")
    lines.append(f"最优提案: `{best['version']}` (得分 {best['score']:.4f})")
    lines.append("")
    lines.append(
        "> 优化结果仅供提案, 需人工 `StrategyVersionManager.activate(version)` 确认后才生效 "
        "(AI 只优化不交易)。"
    )
    return "\n".join(lines)


def save_report(md: str, path: str) -> str:
    """写 markdown 到文件, 返回路径"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(md)
    return path
