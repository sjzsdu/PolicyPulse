from pathlib import Path

import pytest

from policypulse.config import Settings, load_theme_alias
from policypulse.extract import (
    classify_meeting_type,
    extract_signals,
    normalize,
    verify_quote,
)

BODY = (
    "国务院常务会议指出，要加快培育低空经济新增长极，支持通用航空和无人机制造企业发展。\n"
    "会议决定，设立专项资金支持人工智能算力中心建设，今年安排资金超过200亿元。\n"
    "同时强调，要严厉整治部分行业低价无序竞争，淘汰落后产能。\n"
    "本页面仅供浏览，导航栏内容不参与抽取。"
)


def test_quote_is_always_a_substring_of_the_body():
    signals = extract_signals("doc-test", "国务院常务会议部署低空经济", BODY,
                              load_theme_alias(), Settings())
    assert signals, "规则抽取应当至少产出一个信号"
    for sig in signals:
        assert verify_quote(sig.quote, BODY), f"quote 不是原文子串: {sig.quote}"
        assert len(sig.quote) <= 200


def test_hallucinated_quote_is_rejected():
    assert verify_quote("这句话根本不在原文里", BODY) is False
    assert verify_quote(BODY.split("\n")[0], BODY) is True


def test_normalize_ignores_whitespace():
    assert normalize("低 空\n经济") == normalize("低空经济")


def test_theme_and_direction():
    signals = extract_signals("doc-test", "国务院常务会议", BODY, load_theme_alias(), Settings())
    themes = {t for s in signals for t in s.theme_tags}
    assert themes & {"低空经济", "算力", "人工智能"}
    bullish = [s for s in signals if s.direction == "利好"]
    assert bullish, "存在明确的支持性表述"
    assert all(1 <= s.strength <= 5 for s in signals)


def test_strength_rises_with_funding_cues():
    weak = "会议要求继续做好相关工作，稳步推进各项改革任务落实，确保政策落地见效。"
    strong = ("会议决定设立专项资金支持人工智能算力中心建设，"
              "今年安排资金200亿元，落实技术改造投资相关税收优惠政策。")
    s_weak = extract_signals("d1", "会议", weak, load_theme_alias(), Settings())
    s_strong = extract_signals("d2", "会议", strong, load_theme_alias(), Settings())
    assert s_strong, "带专项资金与金额的部署句必须抽出信号"
    assert max(s.strength for s in s_strong) >= 4
    if s_weak:
        assert max(s.strength for s in s_strong) > max(s.strength for s in s_weak)


def test_low_value_sentences_are_dropped():
    filler = "本页面导航栏提供了网站地图、联系我们、站点地图等辅助信息入口。"
    assert extract_signals("d3", "网站首页", filler, load_theme_alias(), Settings()) == []


def test_meeting_type_classification():
    assert classify_meeting_type("李强主持召开国务院常务会议") == "国务院常务会议"
    assert classify_meeting_type("中共中央政治局会议召开") == "政治局会议"
    assert classify_meeting_type("国务院办公厅关于印发《某方案》的通知") == "政策文件"


def test_real_article_extraction(tmp_path):
    """用真实抓取的 gov.cn 政策全文做一遍抽取（离线 fixture，非 mock）。"""
    from policypulse.text import extract_main_text, extract_title

    html = (Path(__file__).parent / "fixtures" / "gov_article.html").read_text(encoding="utf-8")
    body = extract_main_text(html)
    assert len(body) > 500
    signals = extract_signals("doc-real", extract_title(html), body,
                              load_theme_alias(), Settings())
    for sig in signals:
        assert verify_quote(sig.quote, body)
