#!/usr/bin/env python3
"""把 docs/agent-architecture.md（源）渲染为 docs/agent-architecture.html（产物）。

用法（Conda 环境，需 markdown-it-py）：
    PY=/Users/francis/miniconda3/envs/ai-gaokao-counselor/bin/python3
    $PY scripts/render_arch.py

- MD 为唯一源，改 MD 后重跑本脚本即可同步 HTML；不要直接编辑 HTML。
- mermaid 图通过 CDN 在浏览器端渲染，查看 HTML 需联网（离线时图不显示、正文/表格仍可读）。
"""
import re
import html
import sys
from pathlib import Path
from markdown_it import MarkdownIt

SRC = Path("docs/agent-architecture.md")
OUT = Path("docs/agent-architecture.html")

src = SRC.read_text(encoding="utf-8")

md = MarkdownIt("commonmark")
try:
    md.enable(["table", "strikethrough"])
except Exception as e:  # noqa
    print("enable table failed:", e, file=sys.stderr)


def fence(self, tokens, idx, options, env):
    tok = tokens[idx]
    info = (tok.info or "").strip()
    esc = html.escape(tok.content, quote=False)
    if info.split()[0:1] == ["mermaid"]:
        return f'<pre class="mermaid">\n{esc}</pre>\n'
    lang = info.split()[0] if info else ""
    cls = f' class="language-{lang}"' if lang else ""
    return f"<pre><code{cls}>{esc}</code></pre>\n"


md.add_render_rule("fence", fence)

# parse -> inject heading ids -> build TOC
tokens = md.parse(src)
toc = []
seen = {}
for i, t in enumerate(tokens):
    if t.type == "heading_open" and t.tag in ("h1", "h2"):
        text = tokens[i + 1].content if i + 1 < len(tokens) else ""
        plain = re.sub(r"[`*]", "", text)
        slug = re.sub(r"[^\w\u4e00-\u9fff-]+", "-", plain).strip("-").lower()
        if slug in seen:
            seen[slug] += 1
            slug = f"{slug}-{seen[slug]}"
        else:
            seen[slug] = 0
        t.attrSet("id", slug)
        if t.tag == "h2":
            toc.append((slug, plain))

body = md.renderer.render(tokens, md.options, {})

toc_html = "\n".join(
    f'<a href="#{s}">{html.escape(t, quote=False)}</a>' for s, t in toc
)

page = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>架构总览 · 高考志愿智能决策 Agent 系统</title>
<script src="https://cdn.jsdelivr.net/npm/mermaid@10.9.1/dist/mermaid.min.js"></script>
<script>window.addEventListener('load',function(){{if(window.mermaid){{mermaid.initialize({{startOnLoad:true,theme:'default',securityLevel:'loose'}});}}}});</script>
<style>
:root{{--fg:#1f2328;--muted:#656d76;--bd:#d0d7de;--bg:#fff;--side:#f6f8fa;--ac:#0969da;}}
*{{box-sizing:border-box;}}
body{{margin:0;color:var(--fg);background:var(--bg);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",Helvetica,Arial,sans-serif;line-height:1.65;}}
#side{{position:fixed;top:0;left:0;width:280px;height:100vh;overflow:auto;background:var(--side);border-right:1px solid var(--bd);padding:20px 16px;}}
#side h2{{font-size:14px;color:var(--muted);margin:0 0 12px;text-transform:none;}}
#side a{{display:block;padding:6px 10px;color:var(--fg);text-decoration:none;border-radius:6px;font-size:14px;margin-bottom:2px;}}
#side a:hover{{background:#eaeef2;color:var(--ac);}}
#main{{margin-left:280px;padding:32px 48px;max-width:960px;}}
h1{{font-size:28px;border-bottom:2px solid var(--bd);padding-bottom:12px;}}
h2{{font-size:21px;margin-top:36px;border-bottom:1px solid var(--bd);padding-bottom:6px;}}
blockquote{{margin:16px 0;padding:12px 16px;border-left:4px solid #d4a72c;background:#fff8e5;border-radius:0 6px 6px 0;color:#4d3800;}}
blockquote p{{margin:6px 0;}}
table{{border-collapse:collapse;width:100%;margin:16px 0;font-size:14px;}}
th,td{{border:1px solid var(--bd);padding:8px 12px;text-align:left;vertical-align:top;}}
th{{background:var(--side);font-weight:600;}}
tr:nth-child(even) td{{background:#fbfcfd;}}
code{{background:#eff1f3;padding:2px 6px;border-radius:4px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:85%;}}
pre{{background:var(--side);border:1px solid var(--bd);border-radius:8px;padding:14px;overflow:auto;}}
pre code{{background:none;padding:0;}}
pre.mermaid{{background:#fff;border:1px solid var(--bd);text-align:center;}}
a{{color:var(--ac);}}
hr{{border:0;border-top:1px solid var(--bd);margin:28px 0;}}
@media(max-width:900px){{#side{{display:none;}}#main{{margin-left:0;padding:20px;}}}}
</style>
</head>
<body>
<nav id="side">
<h2>架构总览（派生自 PRD v1.3）</h2>
{toc_html}
</nav>
<main id="main">
{body}
</main>
</body>
</html>
"""

OUT.write_text(page, encoding="utf-8")
print(f"wrote {OUT} ({len(page.splitlines())} lines), TOC entries={len(toc)}")
