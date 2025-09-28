from gc_bot.state import BotState, should_open_from_signal


def test_should_open_from_signal_true():
    state = {"position": "FLAT"}
    signal = {"is_gc": True, "already_signaled": False}
    assert should_open_from_signal(state, signal)


def test_should_open_from_signal_false_when_long():
    state = {"position": "LONG"}
    signal = {"is_gc": True, "already_signaled": False}
    assert not should_open_from_signal(state, signal)


def test_bot_state_defaults():
    st = BotState()
    assert st.position == "FLAT"
    assert st.pnl_cum == 0.0
