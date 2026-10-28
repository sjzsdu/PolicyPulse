"""pipeline 端到端离线用例。

HTTP 层用冻结的**真实** gov.cn 页面，tongstock 层用一个最小数据客户端，
抓取/去重/抽取/映射/打分/落库/报告整条链路都是真实代码。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from policypulse.config import Settings, default_paths, load_theme_alias
from policypulse.http import Fetcher
from policypulse.pipeline import build_conclusions, doc_id_for, fetch_documents
from policypulse.report import render_markdown
from policypulse.store import Store
from policypulse.tongstock import CONCEPT_FILE, INDUSTRY_FILE, Ticker

ARTICLE = (Path(__file__).parent / "fixtures" / "gov_article.html").read_text(encoding="utf-8")
ARTICLE_URL = "https://www.gov.cn/zhengce/202610/content_7082820.htm"


class _FrozenFetcher:
    def __init__(self, pages: dict[str, str]) -> None:
        self.pages = pages
        self.warnings: list[str] = []
        self.requests: list[str] = []

    def get_text(self, url: str) -> str:
        self.requests.append(url)
        return self.pages[url]

    def get_json(self, url: str):
        return json.loads(self.pages[url])

    def configure_rate_limit(self, url: str, seconds: float) -> None:
        pass


class _MiniMarket:
    """最小可用数据集：只替换 TDX 读取，映射/过滤/打分是真实代码。"""

    calls = 0

    def sectors(self, block_file: str = CONCEPT_FILE):
        return {"碳中和": 4} if block_file == CONCEPT_FILE else {"环境保护": 4}

    def members(self, sector: str, block_file: str = CONCEPT_FILE):
        return ["600989", "300124", "000998", "002001"]

    def tickers(self):
        return {
            "600989": Ticker("600989", "宝丰能源", "sh", "沪市主板", 1705.0),
            "300124": Ticker("300124", "汇川技术", "sz", "创业板", 1220.0),
            "000998": Ticker("000998", "ST农科", "sz", "深市主板", 80.0),
            "002001": Ticker("002001", "小市值", "sz", "深市主板", 12.0),
        }


def _specs():
    from policypulse.config import SourceSpec

    return [SourceSpec(id="gov_zhengce", name="最新政策", org="国务院", tier="中央",
                       adapter="gov_json_feed", url="https://www.gov.cn/zhengce/zuixin/",
                       meeting_type_hint="政策文件", rate_limit_seconds=2.0,
                       extra={"feed_url": "https://www.gov.cn/zhengce/zuixin/ZUIXINZHENGCE.json"})]


def _feed():
    return json.dumps([{"TITLE": "中共中央 国务院关于发展新质生产力的意见",
                        "URL": ARTICLE_URL, "DOCRELPUBTIME": "2026-10-09"}],
                      ensure_ascii=False)


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("POLICYPULSE_HOME", str(tmp_path))
    paths = default_paths()
    (tmp_path / "snap").mkdir(exist_ok=True)
    return paths, tmp_path / "snap"


def _run_once(paths, snapshot_dir, store, dedupe=True):
    settings = Settings(cross_run_dedupe=dedupe)
    fetcher = _FrozenFetcher({
        "https://www.gov.cn/zhengce/zuixin/ZUIXINZHENGCE.json": _feed(),
        ARTICLE_URL: ARTICLE,
    })
    docs, failed, duplicates = fetch_documents(fetcher, _specs(), settings, "2026-10-06",
                                               "2026-10-19", snapshot_dir, "run-test", store)
    return docs, failed, duplicates


def test_end_to_end_offline(workspace):
    paths, snapshot_dir = workspace
    store = Store(paths.db)
    docs, failed, duplicates = _run_once(paths, snapshot_dir, store)

    assert not failed
    assert duplicates == 0
    assert len(docs) == 1
    doc = docs[0]
    assert doc.url == ARTICLE_URL
    assert Path(doc.snapshot_path).exists()
    assert doc.doc_id == doc_id_for(ARTICLE_URL)

    signals, conclusions = build_conclusions(docs, store, Settings(), _MiniMarket(),
                                             "run-test", load_theme_alias())
    assert signals, "真实政策全文应能抽出信号"
    assert all(s.quote_verified for s in signals), "quote 必须是原文子串"
    assert conclusions

    with_tickers = [c for c in conclusions if c["tickers"]]
    assert with_tickers, "新质生产力全文应能映射到碳中和板块"
    codes = [t["ticker_code"] for c in with_tickers for t in c["tickers"]]
    assert "600989" in codes
    assert "000998" not in codes, "ST 股必须被过滤"
    assert "002001" not in codes, "市值低于阈值的必须被过滤"

    md = render_markdown(type("R", (), {
        "run_id": "run-test", "window_start": "2026-10-06", "window_end": "2026-10-19",
        "docs": docs, "conclusions": conclusions, "failed_sources": [], "warnings": [],
        "duplicates_skipped": 0,
        "stats": {"docs": 1, "signals": len(signals), "with_tickers": len(with_tickers),
                  "ticker_rows": len(codes), "sources_total": 1, "sources_failed": 0,
                  "tongstock_calls": 0, "llm_enabled": False, "duplicates_skipped": 0,
                  "sources_no_update": [], "gate": {}, "min_strength_for_report": 3},
    })())
    assert ARTICLE_URL in md
    assert "600989" in md
    store.close()


def test_cross_run_dedupe_skips_already_seen_documents(workspace):
    """F：第二次 run 命中跨 run 去重——不重复抽取，但文档以复用态进本轮报告。"""
    paths, snapshot_dir = workspace
    store = Store(paths.db)
    first_docs, _, first_dupes = _run_once(paths, snapshot_dir, store)
    assert len(first_docs) == 1 and first_dupes == 0
    build_conclusions(first_docs, store, Settings(), _MiniMarket(), "run-1", load_theme_alias())

    second_docs, _, second_dupes = _run_once(paths, snapshot_dir, store)
    assert second_dupes == 1
    assert len(second_docs) == 1
    assert second_docs[0].reused and second_docs[0].doc_id == first_docs[0].doc_id

    # 复用文档不再重抽：结论从库内信号恢复，signals（新抽）为空
    signals, conclusions = build_conclusions(second_docs, store, Settings(), _MiniMarket(),
                                             "run-2", load_theme_alias())
    assert signals == []
    assert conclusions and all(c.get("reused") for c in conclusions)
    store.close()


def test_dedupe_can_be_disabled(workspace):
    paths, snapshot_dir = workspace
    store = Store(paths.db)
    docs, _, _ = _run_once(paths, snapshot_dir, store)
    build_conclusions(docs, store, Settings(), _MiniMarket(), "run-1", load_theme_alias())

    docs2, _, dupes = _run_once(paths, snapshot_dir, store, dedupe=False)
    assert len(docs2) == 1 and dupes == 0
    store.close()


def test_out_of_window_documents_are_not_fetched(workspace):
    """第 1 点：窗口内没有更新 = 不抓。

    旧实现会「退回抓入口页最近 3 条」，把 10 月的稿子塞进 1 月的日报；
    现在必须是空结果 + 明确记录「该源窗口内无更新」。
    """
    paths, snapshot_dir = workspace
    store = Store(paths.db)
    settings = Settings()
    fetcher = _FrozenFetcher({
        "https://www.gov.cn/zhengce/zuixin/ZUIXINZHENGCE.json": _feed(),
        ARTICLE_URL: ARTICLE,
    })
    diag: dict = {}
    docs, failed, dupes = fetch_documents(fetcher, _specs(), settings, "2026-01-01",
                                          "2026-01-02", snapshot_dir, "run-test", store, diag)
    assert docs == [], "窗口外的稿子一条都不能进产物"
    assert failed == [] and dupes == 0, "源本身是可用的，只是窗口内没有更新"
    assert diag["discovered"] == ["gov_zhengce"]
    assert diag["no_update"] == ["gov_zhengce"]
    store.close()


def _feed_no_list_date():
    """列表页不带日期：商务部 xwfb 一类栏目就是这样，靠详情页日期兜底。"""
    return json.dumps([{"TITLE": "中共中央 国务院关于发展新质生产力的意见",
                        "URL": ARTICLE_URL}], ensure_ascii=False)


def _fetch_with_feed(workspace, feed_json, window_start, window_end, article=ARTICLE):
    paths, snapshot_dir = workspace
    store = Store(paths.db)
    fetcher = _FrozenFetcher({
        "https://www.gov.cn/zhengce/zuixin/ZUIXINZHENGCE.json": feed_json,
        ARTICLE_URL: article,
    })
    diag: dict = {}
    docs, failed, dupes = fetch_documents(fetcher, _specs(), Settings(), window_start,
                                          window_end, snapshot_dir, "run-test", store, diag)
    store.close()
    return docs, failed, dupes, diag, fetcher


def test_detail_page_date_out_of_window_is_dropped(workspace):
    """列表页无日期时，详情页日期必须再判一次窗口。

    商务部 xwfb 列表页解析不出日期 → 旧实现绕过窗口过滤 → 10-14 的旧稿
    进了「10-19 ~ 10-20」的当日日报。现在要剔除并计入 out_of_window_docs。
    """
    docs, failed, dupes, diag, fetcher = _fetch_with_feed(workspace, _feed_no_list_date(),
                                                          "2026-10-19", "2026-10-20")
    assert docs == [], f"详情页日期 2026-10-09 落在窗口外，不得入库：{docs}"
    assert failed == [] and dupes == 0, "抓取本身成功，只是被窗口口径剔除"
    assert diag["out_of_window_docs"] == 1, "窗口外的稿子必须计数，不能静默丢弃"
    assert diag["no_update"] == ["gov_zhengce"], "全部候选都被剔除 = 该源窗口内无更新"
    # 剔除明细必须逐条进告警：source_id + url + 提取日期 + 判定窗口，数字才能回溯
    assert any("窗口外剔除" in w and ARTICLE_URL in w and "2026-10-09" in w
               and "[2026-10-19, 2026-10-20]" in w for w in fetcher.warnings), \
        f"窗口外剔除要有可回溯的逐条告警：{fetcher.warnings}"


def test_detail_page_date_inside_window_is_kept(workspace):
    """对照组：详情页日期在窗口内时照常入库，out_of_window_docs 归零。"""
    docs, failed, dupes, diag, _ = _fetch_with_feed(workspace, _feed_no_list_date(),
                                                    "2026-10-06", "2026-10-19")
    assert len(docs) == 1, "详情页日期 2026-10-09 在窗口内，应当入库"
    assert docs[0].published_at == "2026-10-09", "入库日期取详情页日期"
    assert diag["out_of_window_docs"] == 0
    assert diag["no_update"] == []
    assert failed == [] and dupes == 0


def _long_page_with_text_date(date_text: str) -> str:
    """前 200KB 里没有日期、正文里有的页面：日期只能走文本兜底分支。

    填充块用 <style>：extract_main_text 会剥掉它，正文仍然带日期；
    extract_publish_time 的 html 分支只看 raw html[:200000]，看不到日期。
    """
    pad = "#pad{color:red;padding:1px;margin:2px}\n" * 7000  # ~224KB，日期必然落在 200KB 之后
    sentence = ("会议于" + date_text + "举行，本次会议研究了低空经济产业协同与基础设施"
                "建设相关工作，并就下一步重点任务作出部署安排。")
    return (
        "<html><head><title>测试会议通知</title>"
        f"<style>{pad}</style></head><body>"
        f"<article><h1>测试会议通知</h1><p>{sentence * 6}</p></article>"
        "</body></html>"
    )


def test_text_fallback_date_in_window_is_not_dropped(workspace):
    """文本兜底分支的日期必须归一后再判窗：窗口内稿件不得被未归一日期误杀。"""
    docs, failed, dupes, diag, _ = _fetch_with_feed(
        workspace, _feed_no_list_date(), "2026-10-19", "2026-10-20",
        article=_long_page_with_text_date("2026年10月19日"))
    assert len(docs) == 1, "窗口内稿件不得被文本分支的未归一日期误杀"
    assert docs[0].published_at == "2026-10-19", "文本兜底分支也要归一成 YYYY-MM-DD"
    assert diag["out_of_window_docs"] == 0, "误杀不能混进真剔除的计数"
    assert diag["no_update"] == []
    assert failed == [] and dupes == 0


def test_text_fallback_date_out_of_window_is_still_dropped(workspace):
    """对照组：文本兜底分支取到的窗口外日期照常剔除并计数。"""
    docs, failed, dupes, diag, fetcher = _fetch_with_feed(
        workspace, _feed_no_list_date(), "2026-10-19", "2026-10-20",
        article=_long_page_with_text_date("2026年10月14日"))
    assert docs == [], "文本分支取到的 2026-10-14 在窗口外，不得入库"
    assert failed == [] and dupes == 0, "抓取本身成功，只是被窗口口径剔除"
    assert diag["out_of_window_docs"] == 1, "归一后的窗口外日期照常计入真剔除"
    assert diag["no_update"] == ["gov_zhengce"]
    assert any("窗口外剔除" in w and "2026-10-14" in w for w in fetcher.warnings), \
        "文本分支取到的窗口外日期同样要留下可回溯的告警明细"


# ---------- 商务部：列表页日期 + 候选按日期排序（WAN-26）----------

MOFCOM_LIST = (Path(__file__).parent / "fixtures" / "mofcom_xwfb_list.html").read_text(encoding="utf-8")
MOFCOM_URL = "http://www.mofcom.gov.cn/xwfb/index.html"


def _mofcom_spec():
    from policypulse.config import SourceSpec

    return SourceSpec(id="mofcom_xwfb", name="新闻发布", org="商务部", tier="部委",
                      adapter="html_list", url=MOFCOM_URL, column="新闻发布",
                      meeting_type_hint="会议", rate_limit_seconds=0.0,
                      extra={"link_pattern": r"/art/\d{4}/art_", "max_items": 60,
                             "min_title_length": 10, "reject_pattern": r"/zfxxgk/"})


def _mofcom_detail(title: str, published: str) -> str:
    """详情页：日期只在 meta PubDate 里（与商务部真实模板一致）。"""
    body = (
        f"{title}\n"
        "有记者问：近日有关部门发布了对华投资限制措施，请问商务部对此有何评论？\n"
        "答：中方注意到有关情况。商务部将会同有关部门密切关注，依法依规处理，"
        "坚决维护中国企业合法权益，同时继续推进高水平对外开放，稳住外贸外资基本盘，"
        "支持跨境电商、海外仓等新业态发展，推动外贸提质增效。\n"
        "下一步，商务部将抓好各项政策落实，加强部门协同和央地联动，"
        "持续优化营商环境，为外资企业提供更多便利，推动外贸稳规模、优结构，"
        "并把政策红利尽快传导到经营主体，切实增强企业获得感。\n"
    )
    return (f'<html><head><meta name="PubDate" content="{published} 10:00">'
            f"<title>{title}</title></head>"
            f'<body><div class="content">{body}</div></body></html>')


def _mofcom_pages(published_days) -> dict[str, str]:
    """列表页 + 指定日期那几条的详情页；窗口外稿件的详情页故意不给。"""
    pages = {MOFCOM_URL: MOFCOM_LIST}
    from policypulse.sources.html_list import HtmlListAdapter

    items = HtmlListAdapter().discover(_mofcom_spec(), _FrozenFetcher(pages))
    for item in items:
        if item.published_at in published_days:
            pages[item.url] = _mofcom_detail(item.title, item.published_at)
    return pages


def test_candidates_are_selected_by_date_not_by_page_order(tmp_path):
    """商务部首页顺序是栏目顺序：领导人活动在最前，产业栏目在后。

    按页面顺序取前 N 条，产业稿件（日常新闻发布/发言人谈话/司局负责人发布）
    会永远被截掉；这里断言候选先按日期过滤窗口、再按日期倒序。
    """
    fetcher = _FrozenFetcher(_mofcom_pages({"2026-10-24", "2026-10-23"}))
    docs, failed, _ = fetch_documents(fetcher, [_mofcom_spec()], Settings(max_docs_per_source=1),
                                     "2026-10-24", "2026-10-24", tmp_path, "run-mofcom", None)
    assert failed == []
    assert [d.published_at for d in docs] == ["2026-10-24"]
    # 页面第 1 条是转载的「李强主持召开国务院常务会议」（10-23，窗口外）；
    # 取到的必须是窗口内最新那条商务部自己的稿件
    assert "商务部新闻发言人" in docs[0].title


def test_all_in_window_mofcom_docs_are_kept(tmp_path):
    """窗口内有 6 条时全部入库，且不再有「详情页日期剔除」的浪费。"""
    diag: dict = {}
    fetcher = _FrozenFetcher(_mofcom_pages({"2026-10-24", "2026-10-23"}))
    docs, failed, _ = fetch_documents(fetcher, [_mofcom_spec()], Settings(max_docs_per_source=12),
                                     "2026-10-23", "2026-10-24", tmp_path, "run-mofcom2", None, diag)
    assert failed == []
    assert len(docs) == 6
    assert all(d.published_at in ("2026-10-23", "2026-10-24") for d in docs)
    assert diag["out_of_window_docs"] == 0
    # 只请求了窗口内的详情页：页面顺序前 3 条（10-23/10-22/10-22）之外没有再白抓旧稿
    assert all(u in fetcher.pages for u in fetcher.requests)
