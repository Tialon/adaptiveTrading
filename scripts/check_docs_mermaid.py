"""文档 Mermaid 图语法校验(V12.5)

给 `docs/*.md` 里的 ```mermaid 块做**真实解析**校验, 防止画坏的图悄悄进仓库。

为什么需要它: Mermaid 语法错**不会报错**, 只会把图渲染成空白或错乱 —— 在 GitHub 上
肉眼一看才发现。更隐蔽的是「语法合法但布局崩掉」: 例如在 `flowchart` 的 subgraph 里写
`direction LR`, 只要该 subgraph 有跨子图连线, 方向就会被忽略, 整条链被竖着堆成
1800px 高一列 —— 语法校验通过, 排版却比原来更难读。

本脚本用无头浏览器加载 mermaid 官方 CDN, 对每个块调 `mermaid.parse()`。
只做**语法**校验; 排版好坏仍需人眼看渲染结果(见 `--render`)。

依赖(不在 pyproject 里, 按需自装):
    pip install playwright && playwright install chromium

用法:
    python scripts/check_docs_mermaid.py docs/architecture.md
    python scripts/check_docs_mermaid.py docs/            # 递归校验整个目录
    python scripts/check_docs_mermaid.py docs/ --render out/   # 同时导出 PNG 供目视

退出码: 0 = 全部通过; 1 = 有图解析失败(或环境缺依赖)。
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

MERMAID_CDN = "https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.min.js"
BLOCK_RE = re.compile(r"```mermaid\r?\n(.*?)```", re.DOTALL)


def collect(target: Path) -> list[tuple[Path, int, str]]:
    """收集 (文件, 序号, 图内容)。target 可以是文件或目录。"""
    files = sorted(target.rglob("*.md")) if target.is_dir() else [target]
    out: list[tuple[Path, int, str]] = []
    for f in files:
        for i, m in enumerate(BLOCK_RE.findall(f.read_text(encoding="utf-8")), 1):
            out.append((f, i, m))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="校验文档里的 Mermaid 图")
    ap.add_argument("target", type=Path, help="markdown 文件或目录")
    ap.add_argument("--render", type=Path, default=None, help="同时导出 PNG 到该目录")
    args = ap.parse_args()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("缺少 playwright。请先: pip install playwright && playwright install chromium")
        return 1

    blocks = collect(args.target)
    if not blocks:
        print(f"{args.target}: 未找到 mermaid 图")
        return 0

    bad = 0
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.set_content("<!doctype html><html><body></body></html>")
        page.add_script_tag(url=MERMAID_CDN)
        page.wait_for_function("typeof mermaid !== 'undefined'", timeout=20_000)
        page.evaluate("mermaid.initialize({startOnLoad:false, theme:'neutral'})")

        for path, idx, code in blocks:
            ok, err = page.evaluate(
                """async (code) => {
                    try { await mermaid.parse(code); return [true, '']; }
                    catch (e) { return [false, String((e && e.message) || e)]; }
                }""",
                code,
            )
            label = f"{path} 图{idx}"
            if ok:
                print(f"  OK   {label}")
            else:
                bad += 1
                print(f"  FAIL {label}\n{err}\n")

            if ok and args.render:
                args.render.mkdir(parents=True, exist_ok=True)
                svg = page.evaluate(
                    """async (code) => (await mermaid.render(
                        'g' + Math.random().toString(36).slice(2), code)).svg""",
                    code,
                )
                shot = browser.new_page(viewport={"width": 1400, "height": 900})
                shot.set_content(f"<!doctype html><html><body style='margin:0'>{svg}</body></html>")
                out = args.render / f"{path.stem}-{idx}.png"
                shot.locator("svg").screenshot(path=str(out))
                print(f"       → {out}")
                shot.close()

        browser.close()

    print(f"\n{len(blocks) - bad}/{len(blocks)} 张图语法通过" + (f", {bad} 张失败" if bad else ""))
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
