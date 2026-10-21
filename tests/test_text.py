from pathlib import Path

from policypulse.http import content_hash
from policypulse.text import (
    extract_main_text,
    extract_publish_time,
    extract_title,
    html_to_text,
    split_sentences,
)


def _fixture(name: str) -> str:
    return (Path(__file__).parent / "fixtures" / name).read_text(encoding="utf-8")


def test_extract_title_from_real_page():
    assert "义务教育" in extract_title(_fixture("gov_article.html")) or \
        len(extract_title(_fixture("gov_article.html"))) > 4


def test_main_text_is_longer_than_navigation():
    body = extract_main_text(_fixture("gov_article.html"))
    assert len(body) > 500
    assert "导航" not in body[:50]


def test_split_sentences_keeps_original_text():
    text = "会议决定加快培育低空经济新增长极。第二句话也要被切出来！第三句同样足够长。"
    parts = split_sentences(text)
    assert len(parts) == 3
    assert all(p in text for p in parts)
    assert split_sentences("短句。") == [], "过短的句子不作为政策信号来源"


def test_publish_time_parsing():
    html = "<div>发布时间：2026-10-09 10:00</div>"
    assert extract_publish_time(html) == "2026-10-09"
    assert extract_publish_time("<div>no date</div>") == ""


def test_publish_time_text_fallback_is_normalized():
    """文本兜底分支必须和 html 分支一样归一成零填充 YYYY-MM-DD。

    原来这里返回原始匹配串（`2026年10月19`），`_in_window` 是裸字符串比较，
    `年`/`/` 的码位大于 `-` → 窗口内稿件被误杀，还被计入 out_of_window_docs。
    """
    assert extract_publish_time("", "会议于2026年10月19日举行") == "2026-10-19"
    assert extract_publish_time("", "2026/10/19 发布") == "2026-10-19"
    assert extract_publish_time("", "2026.10.19 印发") == "2026-10-19"
    # 单位数字同样零填充
    assert extract_publish_time("", "2026年1月9日") == "2026-01-09"
    # 越界日期不返回原始串，按「无日期」处理（不进窗口外剔除计数）
    assert extract_publish_time("", "2026年13月45日") == ""
    # html 分支优先，行为不变
    assert extract_publish_time("<div>发布时间：2026-10-09 10:00</div>",
                                "会议于2026年10月19日举行") == "2026-10-09"


def test_content_hash_ignores_whitespace():
    assert content_hash("低空 经济\n") == content_hash("低空经济")
    assert content_hash("甲") != content_hash("乙")


def test_html_to_text_strips_scripts():
    html = "<html><head><script>var a=1;</script></head><body><p>正文内容在这里</p></body></html>"
    text = html_to_text(html)
    assert "var a=1" not in text
    assert "正文内容在这里" in text
