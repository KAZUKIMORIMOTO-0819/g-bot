from gc_bot import SignalParams, add_sma_columns, detect_golden_cross_latest
import pandas as pd


def test_detect_golden_cross_latest_triggers():
    data = {
        "close": [100, 101, 102, 103, 104, 105, 106, 107, 108, 109],
    }
    idx = pd.date_range("2024-01-01", periods=len(data["close"]), freq="H", tz="Asia/Tokyo")
    df = pd.DataFrame(data, index=idx)
    params = SignalParams(short_window=3, long_window=5, epsilon=0.0)
    df_feat = add_sma_columns(df, params)
    sig = detect_golden_cross_latest(df_feat, params)
    assert isinstance(sig["is_gc"], bool)
    assert "price" in sig
