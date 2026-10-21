"""统计窗口口径（第 1 点）与 tongstock 可执行文件定位（cron PATH 坑）。"""

from __future__ import annotations

from datetime import date

from policypulse.config import Settings
from policypulse.pipeline import window_for


def test_default_window_is_only_yesterday_and_today():
    """根因修复：不再默认回看 14 天，日报只覆盖目标日与前一天。"""
    start, end, days = window_for(date(2026, 10, 20), None, Settings())
    assert start == "2026-10-19" and end == "2026-10-20"
    assert days == 1


def test_since_widens_the_window_explicitly():
    start, end, days = window_for(date(2026, 10, 20), 14, Settings())
    assert start == "2026-10-06" and end == "2026-10-20"
    assert days == 14


def test_since_zero_covers_only_the_target_day():
    start, end, days = window_for(date(2026, 10, 20), 0, Settings())
    assert start == end == "2026-10-20" and days == 0


def test_settings_has_no_lookback_days_anymore():
    """旧字段删干净了：任何代码再读 lookback_days 都会立刻 AttributeError。"""
    fields = Settings.__dataclass_fields__
    assert "lookback_days" not in fields
    assert "top_n_per_sector" not in fields
    assert "daily_window_days" in fields
    assert "min_relevance" in fields


def test_resolve_bin_falls_back_to_user_local_bin(monkeypatch, tmp_path):
    from policypulse import tongstock

    monkeypatch.setattr(tongstock.shutil, "which", lambda name: "/usr/local/bin/" + name)
    assert tongstock.resolve_bin("tongstock") == "/usr/local/bin/tongstock"

    # which 找不到时必须兜底到 ~/.local/bin（cron 的 PATH 里通常没有它）
    monkeypatch.setattr(tongstock.shutil, "which", lambda name: None)
    monkeypatch.setattr(tongstock.Path, "home", lambda: tmp_path)
    assert tongstock.resolve_bin("tongstock") == "tongstock", "两处都没有时保留原名，交给调用方报错"

    bin_dir = tmp_path / ".local" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "tongstock").write_text("#!/bin/sh\n", encoding="utf-8")
    assert tongstock.resolve_bin("tongstock") == str(bin_dir / "tongstock")


def test_cli_run_exposes_since_instead_of_lookback():
    from policypulse.cli import build_parser

    args = build_parser().parse_args(["run", "--since", "14", "--date", "2026-10-20"])
    assert args.since_days == 14
    assert not hasattr(args, "lookback_days")
