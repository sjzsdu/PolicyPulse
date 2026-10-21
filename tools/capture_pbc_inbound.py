#!/usr/bin/env python3
"""半自动旁路导入：把央行原文用「浏览器会话」取成本地 inbound 文件（WAN-10）。

## 这条路为什么合规

`http://www.pbc.gov.cn/robots.txt` 是 `User-agent: * / Disallow: /`，程序不得主动抓取。
本工具**不向 pbc.gov.cn 发 HTTP 请求**：它驱动的是 ego-browser（真实 Chromium、真实
profile、你本人可见、可随时接管）的**一次人工会话**，等价于你自己开浏览器另存网页。
管线本身仍然只读 `inbound/<source_id>/` 的本地文件（`local_inbound` 适配器），
`policypulse run` 对 pbc.gov.cn 依旧零请求。

## 用法

```bash
# 先用单篇文章 URL 试一条（URL 由你从央行栏目页肉眼选定后粘过来）
python3 tools/capture_pbc_inbound.py \\
  --url 'https://www.pbc.gov.cn/goutongjiaoliu/113456/113469/2026101916082228353/index.html'

# 确认落盘无误，再跑管线
policypulse run --sources pbc_inbound
policypulse verify --run <run_id>
```

落盘结果（文件名里的日期 = 页面发布时间，不是抓取时间）：

    inbound/pbc_inbound/2026-10-19-<标题>.html
    inbound/pbc_inbound/2026-10-19-<标题>.url    # 一行：page.url() 生成的原文 URL

## 硬约束（写在代码里，别改成爬虫）

1. 只取命令行显式给出的 URL：不自动发现链接、不翻列表页、不跟分页。
2. 真实 profile、默认 UA：不伪造 UA、不挂代理、不开 stealth。
3. 串行，页间隔由 `--delay-ms` 控制（默认 4s，人类节奏）。
4. 遇到验证码 / 反爬挑战 / 登录墙 → `handOff()` 把浏览器交还给你，不破解、不绕过。
5. 每次执行前后打印 URL 清单，落盘文件可点名核对。
6. 不要把它放进定时任务：无人在场就不该跑，那已经是爬虫了。
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = REPO_ROOT / "inbound" / "pbc_inbound"

# 文件名里不能出现的字符 + 空白
_BAD_CHARS_RE = re.compile(r'[\s/\\:*?"<>|\u0000-\u001f]+')
MANIFEST_MARKER = "__POLICYPULSE_CAPTURE_MANIFEST__"


def slugify(title: str, max_len: int = 40) -> str:
    """标题 → 文件名片段：去空白与路径分隔符，避免超长。"""
    slug = _BAD_CHARS_RE.sub("", title or "").strip(" .-_")
    return slug[:max_len] or "未命名"


def filename_for(published_at: str, title: str) -> str:
    """`2026-10-19-标题.html`；日期取不到就不带前缀（适配器会再试正文）。"""
    slug = slugify(title)
    return f"{published_at}-{slug}.html" if published_at else f"{slug}.html"


def build_script(urls: list[str], work_dir: str, manifest_path: str,
                 space_name: str, delay_ms: int) -> str:
    """生成喂给 `ego-browser nodejs` 的脚本。

    ego-browser 的脚本拿不到 shell 环境变量、也不接受参数，所以输入一律内联进脚本。
    纯函数，便于离线测试。
    """
    payload = json.dumps({"urls": urls, "workDir": work_dir,
                          "manifestPath": manifest_path, "delayMs": delay_ms},
                         ensure_ascii=False)
    return f"""const fs = await import("node:fs/promises");
const cfg = {payload};

console.log("URL 清单（执行前），共 " + cfg.urls.length + " 条：");
for (const u of cfg.urls) console.log("  - " + u);

const task = await taskSpace({json.dumps(space_name)});
const page = task.page("p1");
const entries = [];
let stopped = null;

for (let i = 0; i < cfg.urls.length; i++) {{
  if (i > 0) await page.waitForTimeout(cfg.delayMs);      // 串行 + 人类节奏
  await page.goto(cfg.urls[i], {{ waitUntil: "load", timeout: 60000 }});
  await page.waitForTimeout(2500);                        // 让正文渲染完

  const url = await page.url();                           // 用 page.url()，不手抄
  const title = await page.title();
  const html = await page.evaluate(() => document.documentElement.outerHTML);

  // 反爬/验证码/登录墙：交还控制权，不破解
  if (/验证码|人机验证|访问受限|安全验证|请登录/.test(title) && html.length < 20000) {{
    stopped = {{ index: i, url, title }};
    console.log("疑似验证码/访问限制，已把浏览器交还给你：" + url);
    break;
  }}

  const htmlPath = cfg.workDir + "/" + i + ".html";
  const urlPath = cfg.workDir + "/" + i + ".url";
  await fs.writeFile(htmlPath, "<!-- source: " + url + " -->\\n" + html, "utf8");
  await fs.writeFile(urlPath, url + "\\n", "utf8");

  entries.push({{ url, title, htmlPath, urlPath, bytes: html.length }});
  console.log("已取：" + title + " → " + url);
}}

console.log("URL 清单（执行后）：");
for (const e of entries) console.log("  - " + e.url);

await fs.writeFile(cfg.manifestPath,
  JSON.stringify({{ entries, stopped }}, null, 2), "utf8");
console.log("{MANIFEST_MARKER}" + cfg.manifestPath);

if (stopped) {{
  await task.handOff();
}} else {{
  await task.finish({{ keep: [] }});
}}
"""


def _published_at(html: str) -> str:
    """页面发布时间（YYYY-MM-DD）；取不到返回空串，不拿抓取时间冒充。"""
    try:
        from policypulse.text import extract_publish_time

        raw = extract_publish_time(html, html)
    except Exception:  # noqa: BLE001 - 未安装 policypulse 时退化成空
        return ""
    m = re.search(r"(20\d{2})-(\d{2})-(\d{2})", raw or "")
    return m.group(0) if m else ""


def _ego_binary() -> str:
    found = shutil.which("ego-browser")
    if not found:
        sys.exit("找不到 ego-browser：先按 ~/.agents/skills/ego-browser/references/install.md 安装")
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="用 ego-browser 会话把央行原文取成本地 inbound 文件")
    parser.add_argument("--url", action="append", required=True,
                        help="原文 URL，可重复；只取你显式给出的这些，不自动发现链接")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help=f"入库目录（默认 {DEFAULT_OUT}）")
    parser.add_argument("--delay-ms", type=int, default=4000, help="篇间隔，人类节奏（默认 4000ms）")
    parser.add_argument("--dry-run", action="store_true", help="只打印将要执行的浏览器脚本，不开浏览器")
    args = parser.parse_args(argv)

    out_dir = Path(args.out).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    space_name = f"policypulse-pbc-inbound-{time.strftime('%Y%m%d-%H%M%S')}"

    work_dir = Path(tempfile.mkdtemp(prefix="policypulse-capture-"))
    manifest_path = work_dir / "manifest.json"
    script = build_script(args.url, str(work_dir), str(manifest_path), space_name, args.delay_ms)

    if args.dry_run:
        print(script)
        shutil.rmtree(work_dir, ignore_errors=True)
        return 0

    print(f"[capture] 浏览器会话 {space_name}，{len(args.url)} 条 URL，篇间隔 {args.delay_ms}ms")
    proc = subprocess.run([_ego_binary(), "nodejs"], input=script, text=True,
                          capture_output=True, timeout=600)
    sys.stdout.write(proc.stdout)
    if proc.stderr.strip():
        sys.stderr.write(proc.stderr)
    if proc.returncode != 0 or not manifest_path.is_file():
        print("[capture] 浏览器会话没有产出清单，未写入任何文件", file=sys.stderr)
        shutil.rmtree(work_dir, ignore_errors=True)
        return 1

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    written, skipped = [], []
    for entry in manifest.get("entries", []):
        html_path = Path(entry["htmlPath"])
        html = html_path.read_text(encoding="utf-8", errors="replace")
        # 正文里的第一个注释是 source: URL，去掉后再算文件名，避免把注释当正文
        body_html = re.sub(r"^<!-- source: .*? -->\n", "", html, count=1)
        name = filename_for(_published_at(body_html), entry["title"])
        target = out_dir / name
        if target.exists():
            skipped.append(name)
            continue
        shutil.copyfile(html_path, target)
        target.with_suffix(".url").write_text(entry["url"] + "\n", encoding="utf-8")
        written.append((name, entry["url"], entry.get("bytes", 0)))

    shutil.rmtree(work_dir, ignore_errors=True)

    print("\n[capture] 落盘：")
    for name, url, size in written:
        print(f"  + {out_dir / name}  ({size} 字节)\n    {url}")
    for name in skipped:
        print(f"  = 已存在，跳过：{name}")
    if manifest.get("stopped"):
        print(f"\n[capture] 第 {manifest['stopped']['index']} 条疑似验证码/访问限制，"
              f"浏览器已交还给你：{manifest['stopped']['url']}\n"
              f"[capture] 其余 URL 未抓取；按提示手动处理后重跑。")
    if written:
        print(f"\n[capture] 下一步：policypulse run --sources pbc_inbound")
    return 0 if written else 2


if __name__ == "__main__":
    raise SystemExit(main())
