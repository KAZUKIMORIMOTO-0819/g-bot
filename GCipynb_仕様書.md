
# GCボット仕様書

## 1. 概要
- notebooks/GC.ipynb に実装されたゴールデンクロス（GC）をトリガーとする自動売買ボットの仕様を整理する。
- 対象銘柄は XRP/JPY、時間足は 1 時間足。Paper / Real モード双方に対応。
- 1 時間ごとのサイクルでデータ取得→シグナル判定→エントリーまたはクローズ→通知→メトリクス更新を行う。

## 2. 前提環境
- 主要ライブラリ: ccxt, pandas, pytz, requests, pyarrow(任意)。
- Python 3.10 以上を想定。
- データ保存ディレクトリ（相対パス）はノートブック実行ディレクトリからの参照。
- Slack Webhook は `SLACK_WEBHOOK_URL` 環境変数で設定する（ノートブック中にハードコード例あり、実運用では削除推奨）。

### 2.1 ディレクトリ
- ローソク足／ログ: `./data/candles`, `./data/candles/logs`
- 取引履歴: `./data/trades`
- 状態ファイル: `./data/state`
- メトリクス: `./data/metrics`
- JSONL ログ: `./data/logs/jsonl`

## 3. コンフィグレーション

### 3.1 データ取得 `CCXTConfig`
- 主なプロパティ: `exchange_id`, `symbol`, `timeframe`, `period_sec`, `limit`, `max_retries`, `retry_backoff_sec`, `timeout_ms`, `trades_page_limit`, `api_key`, `secret`。
- `exchange_id` 既定値は `binance` だが、実装は bitFlyer 想定。必要に応じて上書きする。

### 3.2 シグナル `SignalParams`
- `short_window=30`, `long_window=60`, `epsilon=1e-12`。
- `add_sma_columns` および `detect_golden_cross_latest` で利用。

### 3.3 発注 `OrderParams`
- `mode`: "paper" または "real"。
- `notional_jpy`: 1 回のエントリーに使用する想定 JPY。
- `slippage_bps`, `taker_fee_bps`: スリッページ・手数料の bps 表現。
- `api_key`, `secret`: Real モードで ccxt に渡す。

### 3.4 状態 `BotState`
- `position`（"FLAT"|"LONG"）や `entry_price`, `size`, `tp`, `sl`, `pnl_cum`, `streak_loss`, `last_gc_bar_ts`, `entry_ts_jst`, `last_updated_jst`, `last_daily_summary_date` を保持。
- JSON へのシリアライズを想定。

### 3.5 Slack 通知 `SlackConfig`
- `webhook_url`（省略時 `SLACK_WEBHOOK_URL` 参照）、`username`, `icon_emoji`, `timeout_sec`, `max_retries`, `backoff_factor`。

### 3.6 ランナー `RunnerConfig`
- `mode`, `symbol`, `state_path`, `notional_jpy`, `slippage_bps`, `taker_fee_bps`, `api_key`, `secret`。
- 環境変数 `BFX_API_KEY`/`BITFLYER_API_KEY` などと `_env_or` でマージ。

## 4. データフロー

1. `_check_dependencies` で必須シンボルの存在確認。
2. `StateStore` を介して `state.json` を排他制御付きで開く。
3. `fetch_ohlcv_latest_ccxt` が最新 1 時間足データ（未確定足除外）を取得し、CSV/Parquet 保存。
    - `fetchOHLCV` 非対応の場合 `_fetch_ohlcv_via_trades` が `fetchTrades` から OHLCV を構築。
4. `add_sma_columns` で SMA30/60 付与。
5. `detect_golden_cross_latest` が直近バーでの GC 判定を行い、重複抑止フラグと各種値を返す。
6. GC 発生時 `notify_gc` が Slack 通知。
7. `should_open_from_signal` が新規エントリー可否を判断。
    - 可の場合 `place_market_buy` で成行買いし、`set_entry_from_order` により状態更新。
    - 同時に `notify_entry` で通知し `trades.csv` にログ。
8. エントリーせず保有中の場合、`close_if_reached_and_update` が TP/SL 到達判定→成行売り→PnL 計算→`trades.csv` 追記→`notify_close`。
9. `update_state_after_signal` が GC 多重発報防止のため `last_gc_bar_ts` を更新。
10. `StateStore.save` で状態を原子的に保存。
11. `write_jsonl` を通じて各ステージ・イベントを JSONL ログに落とす。

## 5. 主要コンポーネント詳細

### 5.1 StateStore
- `acquire_lock` が `.lock` ファイルで簡易ロック。
- `save` は一時ファイル→置換で原子保存、バックアップを維持。
- `__enter__`/`__exit__` で with ブロック対応。

### 5.2 データ取得
- `_init_exchange`：ccxt クライアント初期化。
- `_fetch_ohlcv_direct`：`fetchOHLCV` で確定足＋バッファ。
- `_fetch_ohlcv_via_trades`：`fetchTrades` のページング集計、1 時間バケット化。
- `fetch_ohlcv_latest_ccxt`：cutoff 計算、時刻カラム整形、保存、メタ情報付与。
- `load_latest_cached_ccxt`：Parquet 優先でキャッシュ読み込み。

### 5.3 シグナル
- `add_sma_columns`：ローリング平均で SMA 列追加。
- `detect_golden_cross_latest`：前バー/現バーの SMA 比較で GC 判定、重複判定、必要パラメータ返却。
- `update_state_after_signal`：GC 発報時に `last_gc_bar_ts` を記録。

### 5.4 発注／決済
- `_now_jst`, `compute_tp_sl`：時刻・TP/SL 計算。
- `_fit_amount_to_market`：取引所制約に合わせ数量調整。
- `decide_order_size_jpy_to_amount`：JPY 想定額→数量。
- `_ensure_tradelog`, `_append_trade_row`, `_append_trade_row_close`：`trades.csv` 管理。
- `place_market_buy`：Paper（擬似約定）/Real（ccxt 成行）共通ロジック。
- `place_market_sell`：決済時の Paper/Real 成行処理。
- `is_exit_reached`：現在価格が TP/SL 到達か判定。
- `realize_pnl_and_update_state`：実現損益を計算し、state を FLAT へ。
- `find_last_buy_fee_from_trades`：直近買いの手数料取得。
- `append_trade_outcome_row`：summary 行追記。
- `close_if_reached_and_update`：決済一括処理（通知含む）。

### 5.5 通知
- `_http_post_json`：requests による POST。
- `send_slack_message`：リトライ・指数バックオフ。
- `fmt_signal_gc` / `fmt_entry` / `fmt_close` / `fmt_error` / `fmt_daily_summary`：Slack Block Kit メッセージ生成。
- `notify_gc` / `notify_entry` / `notify_close` / `notify_error` / `notify_daily_summary`：上記フォーマッタと送信を組み合わせ。

### 5.6 ロギング
- `setup_structured_logger`：コンソール・ファイルハンドラ。
- `_jsonl_path_for_today` / `write_jsonl`：日付別 JSONL 出力。
- `log_api_call`：API 呼び出しログ＋ JSONL。
- `log_exception`：例外ログ＋トレース JSONL。
- `append_trade_log`：任意イベントをトレードログと JSONL に記録。

### 5.7 メトリクス
- `_ensure_metrics_log`：`metrics.csv` の存在保証（同名関数重複定義あり、挙動は同じ）。
- `_append_metrics_row`：メトリクス行追記。
- `_equity_curve_from_trades`：summary 行から日内累積損益カーブ構築。
- `_max_drawdown_from_equity`：最大ドローダウン算出。
- `write_daily_metrics`：当日分トレード統計、累積PnL、MaxDD を集計し `metrics.csv` 更新、JSONL に出力。

### 5.8 統合実行 `run_hourly_cycle`
- CCXT からのデータ取得～Slack 通知まで全処理を束ね、処理経過を JSONL に記録。
- エラー時 `notify_error` で Slack へ通知。
- 戻り値は `{"stage": "...", "signal": {...}, "order": {...}, "close": {...}, "state_meta": {...}}` 形式のサマリ辞書。

## 6. エラーハンドリングとリトライ
- データ取得および Slack 送信はリトライと指数バックオフを実装。
- 例外発生時 `log_exception` が構造化ログを残し、Slack へのエラーメッセージ送信を試みる。
- ファイル操作は原子的保存＋バックアップ、ロック解除時はベストエフォートで失敗を無視。

## 7. セキュリティ考慮事項
- ノートブック内に実際の Slack Webhook、API キー/シークレットがハードコードされている例が存在。公開前に削除・無効化が必須。
- 実運用では環境変数または秘密管理システムを用いること。

## 8. テストと運用上の注意
- Paper モードでの動作確認後に Real モードへ移行する。
- ノートブック環境（対話実行）を前提としているため、自動実行環境では Python スクリプト化やスケジューラ整備が望ましい。
- SMA 計算には十分な履歴本数が必要（>= long_window）。データ不足時は `ValueError`。
- `fetchTrades` フォールバック時は出来高が少ないとバー欠落が生じるため、後段での再インデックスが必要なケースあり。
- 日次サマリ・メトリクスの集計は `trades.csv` の summary 行に依存。記録フォーマット変更時は要調整。
