# GCボット処理仕様書

## 1. システム概要
- 本仕様は `gc_bot` パッケージおよび付随スクリプト群の処理内容をまとめたもの。
- ゴールデンクロス（SMA30/60）をトリガーとし、XRP/JPY の1時間足で売買判断を行う。
- ノートブック `notebooks/GC.ipynb` から抽出したロジックを Python モジュール化し、定期実行やテストを容易にする構成。

## 2. ディレクトリ構成
```
gc_bot/                ... コアモジュール
  __init__.py          ... 公開APIまとめ
  cli.py               ... CLIエントリポイント
  config.py            ... 各種設定dataclassと環境変数ロード
  data.py              ... データ取得・シグナル生成
  logging_utils.py     ... 構造化ログ/JSONL出力
  metrics.py           ... 日次メトリクス集計
  notifications.py     ... Slack通知
  orders.py            ... 注文実行・取引ログ
  runner.py            ... 1時間サイクル統合処理
  state.py             ... Bot状態管理
  timeutils.py         ... タイムゾーンヘルパー
scripts/               ... CLIスクリプト
  run_once.py          ... 1回実行
  run_scheduler.py     ... schedule を用いた常駐実行
  backfill_data.py     ... OHLCVバックフィル
tests/                 ... pytest 用スモークテスト
```

## 3. 設定と依存
- `requirements.txt`: ccxt, pandas, numpy, notifications,スケジューラ等に加え pytest を記載。
- `pyproject.toml`: editable install 用定義。`pip install -e .` で `gc_bot` を import 可能。
- `config.py`
  - `load_env_settings()` で `.env` (python-dotenv) をロード（オプション）。
  - `CCXTConfig`, `SignalParams`, `OrderParams`, `SlackConfig`, `RunnerConfig` を dataclass 化。
  - `SlackConfig.resolved_url()` は `SLACK_WEBHOOK_URL` を参照。
- 環境変数:
  - `SLACK_WEBHOOK_URL`, `BITFLYER_API_KEY`, `BITFLYER_API_SECRET` 等。
- データ/ログ保存先は `GC_CANDLES_LOG_DIR`, `GC_CANDLES_DATA_DIR`, `GC_TRADES_DIR`, `GC_METRICS_DIR`, `GC_APP_LOG_DIR`, `GC_JSONL_DIR` で上書き可能。デフォルトは `./data/...`。

## 4. コアモジュール詳細
### 4.1 timeutils.py
- `TZ_UTC`, `TZ_JST` を定義。pytz が無い場合は `timezone(+9)` でフォールバック。
- `now_jst()` / `floor_to_full_hour_utc()` を提供し、全体で統一的に利用。

### 4.2 state.py
- `BotState`: 単一建玉ボットの状態をシリアライズ可能な形で保持。
- `StateStore`:
  - 排他制御: `.lock` ファイルを作成。初期化時に親ディレクトリを `os.makedirs`。
  - 保存: 一時ファイル→`os.replace` で原子的書き込み。バックアップ (`.bak`) も更新。
- ヘルパー関数:
  - `ensure_single_position`, `set_entry_from_order`, `clear_to_flat`, `bump_streak`, `touch_daily_summary_marker`
  - dict互換の `update_state_on_entry`, 判定 `should_open_from_signal`

### 4.3 data.py
- ディレクトリ作成: モジュール import 時に `LOG_DIR`・`DATA_DIR` を作成。
- `_init_exchange`: ccxt クライアント初期化（APIキー/secret対応）。
- `_fetch_ohlcv_direct` / `_fetch_ohlcv_via_trades`: 取引所により `fetchOHLCV` もしくはトレード集計にフォールバック。
- `fetch_ohlcv_latest_ccxt`:
  - 未確定足を除外するため `floor_to_full_hour_utc` で cutoff を計算。
  - リトライ制御 (`max_retries`, `retry_backoff_sec`)。
  - 取得後は DataFrame を整形し、CSV・Parquet（pyarrowある場合）保存、`df.attrs["meta"]` に情報付与。
- `load_latest_cached_ccxt`: Parquet があれば優先して読み込み、無い場合はログCSVの最新を使用。
- `add_sma_columns`: `SignalParams`で指定された短期/長期窓でSMA列を追加。データ不足時は例外。
- `detect_golden_cross_latest`: 最新バーと一つ前のバーでGC判定し、各種数値を辞書で返却。
- `update_state_after_signal`: GC発生時に `last_gc_bar_ts` を更新。

### 4.4 orders.py
- 出力先 `TRADES_DIR` を import時に作成。
- `decide_order_size_jpy_to_amount`: Notional→数量。
- `_fit_amount_to_market`: 取引所最小数量・刻みに合わせ数量調整。
- `_ensure_tradelog`, `_append_trade_row`, `_append_trade_row_close`: `trades.csv` の追記処理。
- `compute_tp_sl`: デフォルトで TP+2%, SL-3%。
- `_init_ccxt_for_real`: Bitflyer クライアントを初期化。
- `place_market_buy` / `place_market_sell`:
  - `mode="paper"` はスリッページ/手数料を内部計算。
  - `mode="real"` は ccxt で発注し、filled/averageなどの情報を利用。
  - 取引ログに追記し、共通フォーマットの dict を返す (動的ノーションは `notional_jpy` として保存)。
- `is_exit_reached`: 現在価格が TP/SL に達したか判定。
- `find_last_buy_fee_from_trades`: 決済時に買い手数料を参照。
- `append_trade_outcome_row`: `mode=summary` の補足行を trades.csv に追記。
- `realize_pnl_and_update_state`: 実現損益（手数料込み）を計算し、`BotState` を FLAT に戻す。
- `close_if_reached_and_update`: TP/SL判定→成行売り→実現損益計算→state保存→summary 行追記→JSONL までを一括実行し、`{reason, close_result, pnl_jpy, state}` を返す（未到達時は None）。

### 4.5 notifications.py
- `send_slack_message`: Webhook無しの場合は no-op の True。指数バックオフリトライ。
- フォーマッタ: `fmt_signal_gc`, `fmt_entry`, `fmt_close`, `fmt_error`, `fmt_daily_summary`。
- `notify_gc`, `notify_entry`, `notify_close`, `notify_error`, `notify_daily_summary`: SlackConfig を受け取り送信。`notify_daily_summary` は `state.json` と `trades.csv` を読み日次レポートを作成。
- `notify_runner_status`: ランナーやバックフィル開始/完了/失敗をステータス通知。`scripts/run_once.py` や `scripts/run_scheduler.py` が利用。

### 4.6 logging_utils.py
- ログ出力先をディレクトリ単位で設定し、存在しなければ作成。
- `setup_structured_logger`: コンソール & `app.log` への INFO ログ。
- `write_jsonl`: 日付別 JSONL (`logs/jsonl/YYYYMMDD.jsonl`) を追記。呼び出しごとに `ts_jst` を自動付与し、`stage`/`signal`/`entry`/`close`/`trade_log`/`daily_metrics` などのイベントを記録。
- `log_api_call`: API呼び出し結果をログ + JSONL に書き込み。
- `log_exception`: 例外内容とスタックトレースを構造化。
- `append_trade_log`: 補助的に trades.csv と JSONL に任意イベントを記録。

### 4.7 metrics.py
- `build_daily_summary`: 当日 `trades.csv` の `mode=summary` 行を優先して勝敗数・PnL を集計し、summary が無い場合は売り約定から推定。
- `_equity_curve_from_trades` / `_max_drawdown_from_equity`: summary 行から累積損益カーブと最大ドローダウンを算出。
- `write_daily_metrics`: 1日1行になるよう既存行を差し替えて `metrics.csv` を冪等更新し、`daily_metrics` イベントを JSONL にも書き出す。

### 4.8 runner.py
- `_env_or`: RunnerConfig の APIキー/secret を環境変数とマージ。
- `_effective_notional`: RunnerConfig と BotState から投入額(JPY)を決定。`notional_fraction` 指定時は総資金×割合、未指定時は固定 `notional_jpy`。
- `run_hourly_cycle` 処理フロー:
  1. ロガー初期化・SlackConfig 生成。
  2. `StateStore` を `with` 管理で開く（ロック取得、stateロード）。
  3. `fetch_ohlcv_latest_ccxt` でデータ取得。成功/失敗に応じて `log_api_call`・`notify_error` 実行。
  4. 取得データが `SignalParams.long_window` 未満の場合: WARNING を出し、JSONL に `insufficient_data` を書き、Slackにもエラー通知、サマリを返して終了。
  5. 十分なデータがある場合: `add_sma_columns`→`detect_golden_cross_latest` でシグナル生成、イベントを JSONL に記録。
  6. GC成立かつ重複でない場合、Slackに通知。
  7. `should_open_from_signal` が True なら `_effective_notional` で算出した投入額を用いて `place_market_buy`→`set_entry_from_order`→state保存→Slack通知。
  8. それ以外で Long 建玉があれば `close_if_reached_and_update` を実行し、決済が行われた場合は Slack通知と JSONL 追記。
  9. `update_state_after_signal` の結果から `last_gc_bar_ts` を更新し、state 保存。
  10. `summary` に `stage`/`signal`/`order`/`close`/`state_meta` などを格納しつつ `write_jsonl` で `done` ステージを記録して返却。
- 例外発生時: `notify_error` を試行し、再度 raise。

## 5. 実行スクリプト
### 5.1 scripts/run_once.py
- コマンドライン引数から `RunnerConfig` を生成し `run_hourly_cycle` を1回実行、結果を JSON 出力。
- `load_env_settings()` で `.env` ロード。
- 実行開始/完了/失敗を `notify_runner_status` で Slack に通知し、失敗時は例外内容を添付。

### 5.2 scripts/run_scheduler.py
- `schedule` ライブラリで毎時 `:05` に `run_hourly_cycle` を実行。
- メインループは5秒間隔で `schedule.run_pending()` を呼び続ける。
- 各サイクルの開始・成功・失敗を `notify_runner_status` で Slack に通知。

### 5.3 scripts/backfill_data.py
- 指定した `CCXTConfig` で `fetch_ohlcv_latest_ccxt` を呼び出し、CSV/Parquet キャッシュを作成。
- 結果のメタ情報（保存パス等）を標準出力。
- バックフィルの開始/完了/失敗を `notify_runner_status` で Slack に送信。

## 6. ログ・ファイル入出力
- `./data/candles/logs/xrpjpy_1h_*.csv`: 各取得タイミングの生データ。
- `./data/candles/xrpjpy_1h_latest.parquet`: 最新 Parquet キャッシュ。
- `./data/trades/trades.csv`: エントリー・クローズ・summary 行を追記。
- `./data/state/state.json`: BotState。本処理で存在しない場合は自動作成。
- `./data/metrics/metrics.csv`: 日次メトリクス。`write_daily_metrics` が冪等更新。
- `./data/logs/app.log`: INFO ログ。
- `./data/logs/jsonl/YYYYMMDD.jsonl`: 構造化ログ（stage, signal, entry, close, daily_metrics など）。

## 7. エラーハンドリング
- ccxt 呼び出し / Slack通知でリトライや例外捕捉を実装。
- `run_hourly_cycle` 内で例外が発生した場合は `notify_error`（Webhook未設定時は no-op）後に再度例外を送出。
- SMA計算に必要な本数が不足する場合は、WARN/JSONL/Slack通知で終了。これにより実行スクリプトが落ちずに次サイクルを待てる。

## 8. テスト
- `tests/test_smoke.py`: SMA計算とGC判定のスモーク。pytest で実行。
- `tests/test_state.py`: `should_open_from_signal` と `BotState` デフォルト値の確認。
- 追加テストを実施する際は `pytest` を利用。

## 9. 運用手順
1. `pip install -r requirements.txt` で依存関係を導入。
2. `pip install -e .` で `gc_bot` パッケージを editable install。
3. `.env` などで `SLACK_WEBHOOK_URL`, `BITFLYER_API_KEY`, `BITFLYER_API_SECRET` を設定。
4. 初回実行前に `scripts/backfill_data.py --limit 200` を実行してデータを蓄積するとスムーズ。
5. 定期実行は `scripts/run_scheduler.py` を pm2, systemd, cron 等から起動。
6. 日次集計が必要なときは適宜 `gc_bot.metrics.write_daily_metrics` をタスク化。

## 10. 補足
- Slack Webhook が未設定の場合は通知をスキップしつつ処理を継続。
- `SignalParams` のウィンドウ長、TP/SL 比率等は dataclass を直接上書きすることで簡単にカスタマイズ可能。
- 取引所から返るデータ不足や API 制限に備え、`run_hourly_cycle` は冪等に設計されているため、失敗時には次サイクルで再実行する運用を推奨。
