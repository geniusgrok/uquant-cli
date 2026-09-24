from datetime import datetime
from types import SimpleNamespace

import pandas as pd
import pytest

from uquant_cli import daily, market
from uquant_cli.store import identity, put


def test_calendar_uses_recent_sessions_without_rejecting_historic_weekend_regimes(monkeypatch):
    frame = pd.DataFrame({"trade_date": ["1991-01-05", "2025-12-31", "2026-09-22",
                                         "2026-09-23", "2026-12-31"]})
    provider = SimpleNamespace(tool_trade_date_hist_sina=lambda: frame)
    monkeypatch.setattr(market.importlib, "import_module", lambda name: provider)
    context, dates = market.calendar_now(datetime(2026, 9, 23, 17, 1, tzinfo=market.SHANGHAI))
    assert context["status"] == "READY"
    assert context["previous_session"] == "2026-09-22"
    assert "1991-01-05" not in dates


def test_offline_calendar_uses_only_verified_previous_receipt(tmp_path, monkeypatch):
    dates = ["2026-09-22", "2026-09-23", "2026-09-24", "2026-09-28"]
    path = "reports/2026-09-23/calendar.json"
    put(tmp_path, path, dates)
    put(tmp_path, "latest.json", {"result_path": "reports/2026-09-23/result.json",
        "files": {path: identity(tmp_path / path)}})
    monkeypatch.setattr(daily, "calendar_now", lambda now: (_ for _ in ()).throw(ConnectionError()))
    context, restored = daily.calendar_context(tmp_path, datetime(2026, 9, 24, 17, tzinfo=market.SHANGHAI))
    assert restored == dates and context["target_date"] == "2026-09-24"
    assert context["calendar_source"] == "已核验的前次交易日历"
    put(tmp_path, path, ["2026-09-22"])
    with pytest.raises(ValueError, match="manifest"):
        daily.calendar_context(tmp_path, datetime(2026, 9, 24, 17, tzinfo=market.SHANGHAI))
