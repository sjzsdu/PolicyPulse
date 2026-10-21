"""tools/capture_pbc_inbound.py 的离线用例（不起浏览器、不发网络请求）。

这个工具的合规性靠两件事保证，两条都在这里钉住：
1. 只取命令行显式给出的 URL——脚本里不得出现"自动发现/翻页"的逻辑；
2. 不伪装身份——脚本里不得出现伪造 UA 或 Baiduspider 之类的字符串。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_tool():
    spec = importlib.util.spec_from_file_location(
        "capture_pbc_inbound", REPO_ROOT / "tools" / "capture_pbc_inbound.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


tool = _load_tool()

PBC_URL = ("https://www.pbc.gov.cn/goutongjiaoliu/113456/113469/"
           "2026101916082228353/index.html")


def test_slugify_strips_path_and_space_chars():
    assert tool.slugify("关于 金融/支持:建设（北京）") == "关于金融支持建设（北京）"
    assert tool.slugify("   ") == "未命名"
    assert len(tool.slugify("长" * 100)) == 40


def test_filename_for_uses_publish_date_prefix():
    assert tool.filename_for("2026-10-19", "公开市场业务交易公告") == "2026-10-19-公开市场业务交易公告.html"
    # 取不到日期就不带前缀，留给 local_inbound 再从正文找
    assert tool.filename_for("", "公开市场业务交易公告") == "公开市场业务交易公告.html"


def test_build_script_only_uses_given_urls_and_hands_off_on_challenge():
    script = tool.build_script([PBC_URL], "/tmp/work", "/tmp/work/manifest.json",
                               "space-1", 4000)
    assert PBC_URL in script
    assert "/tmp/work/manifest.json" in script
    assert "4000" in script
    # 遇到验证码要交还控制权，不破解
    assert "task.handOff" in script
    # 正常结束要收掉 Agent 页面
    assert "task.finish" in script


def test_build_script_does_not_spoof_identity():
    script = tool.build_script([PBC_URL], "/tmp/work", "/tmp/work/m.json", "s", 4000)
    lowered = script.lower()
    # 不伪造 UA、不用代理池、不开 stealth：这些词不该出现在浏览器脚本里
    for banned in ("baiduspider", "useragent", "user-agent", "proxy", "stealth"):
        assert banned not in lowered


def test_build_script_skips_discovery_beyond_given_urls():
    script = tool.build_script([PBC_URL], "/tmp/work", "/tmp/work/m.json", "s", 4000)
    # 不做链接发现：脚本里只允许出现命令行给的那一个 URL
    assert script.count("pbc.gov.cn") == 1


def test_published_at_reads_page_date_and_never_invents_one():
    html = '<html><body><span>发布时间：2026-10-19 16:08</span></body></html>'
    assert tool._published_at(html) == "2026-10-19"
    assert tool._published_at("<html><body>没有日期</body></html>") == ""


def test_dry_run_prints_script_without_touching_browser(capsys):
    assert tool.main(["--url", PBC_URL, "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "ego-browser" not in out  # 脚本内容里没有调用 CLI 自身
    assert PBC_URL in out
    assert "await task.finish" in out
