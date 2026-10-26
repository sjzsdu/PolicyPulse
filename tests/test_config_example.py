"""配置模板（policypulse.yaml.example）必须始终是合法 YAML。

这是用户 `cp` 成 `policypulse.yaml` 的入口模板：解冲突手滑（行首粘个标点）
会让整个配置示例不可用，而功能测试一条都抓不到——直接解析并核对关键键防回归。
"""

from __future__ import annotations

from pathlib import Path

import yaml

EXAMPLE = Path(__file__).resolve().parents[1] / "policypulse.yaml.example"


def test_config_example_parses_and_keeps_expected_keys():
    data = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    assert isinstance(data, dict), "模板必须解析成映射，不能是空/标量"
    assert data["daily_window_days"] == 2, "默认两日窗口是当日增量口径的配置载体"
    assert data["weights"]["relevance"] == 0.50, "score-v2 里公司级相关性主导"
    assert data["relevance_gate"] is True, "公司级相关性闸门默认开"
    assert data["min_relevance"] == 0.5, "弱命中（0.45）默认不进正文"
    assert "lookback_days" not in data, "lookback_days 已删除，模板里不得复活"
    assert data["max_picks"] == 5, "选股上限默认 5 只（WAN-21）"
    assert data["global_max_picks"] == 5, "全局选股上限默认 5 只（WAN-21 v2）"
    assert "max_tickers_per_signal" not in data, "旧键已改名 max_picks，模板里不得复活"


def test_old_max_tickers_key_migrates_without_raising_the_cap(tmp_path):
    """老配置里的旧键不能把「先排序再取前 5」放大回 10 只。"""
    from policypulse.config import Settings

    cfg = tmp_path / "policypulse.yaml"
    cfg.write_text("max_tickers_per_signal: 10\n", encoding="utf-8")
    assert Settings.from_file(cfg).max_picks == 5

    cfg.write_text("max_tickers_per_signal: 3\n", encoding="utf-8")
    assert Settings.from_file(cfg).max_picks == 3

    cfg.write_text("max_picks: 4\nmax_tickers_per_signal: 10\n", encoding="utf-8")
    assert Settings.from_file(cfg).max_picks == 4, "新键优先"


def test_pick_limit_validation():
    """max_picks 必须 >= 1；global_max_picks 的 0 是合法「关闭全局截断」值。"""
    import pytest

    from policypulse.config import Settings

    with pytest.raises(ValueError):
        Settings(max_picks=0)
    with pytest.raises(ValueError):
        Settings(max_picks=-1)
    assert Settings(global_max_picks=0).global_max_picks == 0, "0 合法：关闭全局截断"
    with pytest.raises(ValueError):
        Settings(global_max_picks=-1)
