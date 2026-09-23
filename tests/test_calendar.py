from datetime import datetime
from types import SimpleNamespace

import pandas as pd

from cli_runtime import market


def test_calendar_uses_recent_sessions_without_rejecting_historic_weekend_regimes(monkeypatch):
    frame = pd.DataFrame({"trade_date": ["1991-01-05", "2025-12-31", "2026-09-22",
                                         "2026-09-23", "2026-12-31"]})
    provider = SimpleNamespace(tool_trade_date_hist_sina=lambda: frame)
    monkeypatch.setattr(market.importlib, "import_module", lambda name: provider)
    context, dates = market.calendar_now(datetime(2026, 9, 23, 17, 1, tzinfo=market.SHANGHAI))
    assert context["status"] == "READY"
    assert context["previous_session"] == "2026-09-22"
    assert "1991-01-05" not in dates
