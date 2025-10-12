# GCボット仕様書

## 1. システム概要
- 暗号資産 XRP/JPY の 1 時間足に対して SMA30/60 のゴールデンクロスを検出し、単一ポジションで売買を行う自動売買ボット。
- `gc_bot` パッケージを中心に、データ取得・シグナル判定・発注・状態管理・通知・メトリクス集計をモジュール化。
- Paper（内部約定）と Real（bitFlyer 成行注文）の 2 モードに対応し、Slack 通知と CSV/JSONL ログで運用状況を可視化する。

## 2. 実装構成
### 2.1 ディレクトリ
- `gc_bot/`: ボット本体モジュール。後述の各コンポーネントを実装。
- `scripts/`: 周辺ユーティリティ（単発実行、スケジューラ、データバックフィル、実発注テスト）。
- `notebooks/`: 解析・バックテスト用ノートブック。
- `data/`: 実行時に生成されるローソク足・状態・取引履歴・ログ類の保存先（既定値）。
- `tests/`: pytest による基本的なスモークテスト。

### 2.2 主要モジュールと責務
| ファイル | 主な責務 |
| --- | --- |
| `gc_bot/config.py` | ccxt・シグナル・注文・Slack・ランナー設定の dataclass と `.env` ロード。 |
| `gc_bot/data.py` | ccxt からの OHLCV 取得、キャッシュ保存、SMA 付与、GC 判定、シグナル後の状態更新。 |
| `gc_bot/state.py` | `BotState` の定義、`StateStore` による `state.json` のロック付き読み書き、ポジション管理ヘルパー。 |
| `gc_bot/orders.py` | Paper/Real 共通の成行注文処理、TP/SL 判定、損益計算、`trades.csv` への記録。 |
| `gc_bot/runner.py` | 1 時間サイクルのオーケストレーション (`run_hourly_cycle`)。各ステージで JSONL/Slack 通知を発行。 |
| `gc_bot/notifications.py` | Slack Webhook 送信と Block Kit 形式のメッセージ生成。 |
| `gc_bot/logging_utils.py` | 構造化ロギング、JSONL 書き出し、任意取引ログの補助。 |
| `gc_bot/metrics.py` | `trades.csv` から日次サマリ・最大ドローダウンを算出し `metrics.csv` と JSONL へ出力。 |
| `gc_bot/backtest.py` | SMA 戦略のバックテスト（トレード生成、エクイティカーブ、サマリ指標）。 |
| `gc_bot/cli.py` | `gc-bot-run` エントリーポイント（単発サイクルを CLI から実行）。 |

`gc_bot/__init__.py` は上記のエントリーポイントを公開し、スクリプト／ノートブックから一貫した API を利用可能にしている。

## 3. 実行フロー（`run_hourly_cycle`）
1. ロガー `setup_structured_logger` と Slack 設定を初期化。
2. `StateStore` をコンテキストマネージャで開き、`state.json` をロックした上で読み込み。
3. `CCXTConfig` を生成し、`fetch_ohlcv_latest_ccxt` が最新の確定 1H 足を取得。
   - `exchange.has['fetchOHLCV']` が False の場合は `_fetch_ohlcv_via_trades` が `fetchTrades` を集計して OHLCV を構築。
   - 取得データは `data/candles/logs/xrpjpy_1h_*.csv`（タイムスタンプ付き）と `data/candles/xrpjpy_1h_latest.parquet` に保存。
   - API 呼び出しはリトライ／レイテンシ記録付きで JSONL (`type=api_call`) にも書き出す。
4. 過去足が `SignalParams.long_window` 未満の場合は不足メッセージを Slack/ログに出力してサイクル終了。
5. `add_sma_columns` で SMA30/60 を付与し、`detect_golden_cross_latest` が GC 判定と価格情報を返す。
   - `RunnerConfig.use_rsi_filter=True` の場合は `strategies.add_gc_rsi_features` で RSI を算出し、`evaluate_gc_rsi_signal` が `passes_rsi_filter` を確認。
   - 前回通知したバー (`state.last_gc_bar_ts`) と同一なら `already_signaled=True`。
6. GC 検出時 `notify_gc` で Slack に通知。
7. `should_open_from_signal` が True かつ `state.position == "FLAT"` の場合は新規エントリー。RSI フィルタ有効時は `passes_rsi_filter` が False のケースを除外。
   - `_effective_notional` が `RunnerConfig.notional_fraction` 指定時は残資金（初期資金＋累積 PnL）に基づき動的に算出。
   - `place_market_buy` が Paper/Real を切り替え、スリッページ／手数料を反映して発注。
   - 約定結果を `StateStore.save`（原子的 replace + `.bak` バックアップ）に反映し、`notify_entry` と JSONL (`type=entry`) を送出。
8. GC が無い／建玉保有中の場合は `close_if_reached_and_update` が TP（+2%）/SL（-3%）到達を判定。
   - 到達時に `place_market_sell` を実行し、`realize_pnl_and_update_state` が実現損益と累積 PnL 更新を行う。
   - `append_trade_outcome_row` が `mode=summary` の行を `trades.csv` に追記し、`notify_close` を送出。
9. シグナル処理後 `update_state_after_signal` が `last_gc_bar_ts` を更新し、`StateStore.save` が最新状態を永続化。
10. サマリ (`stage`, `signal`, `order`, `close`, `state_meta`) を辞書で返却し、JSONL (`type=stage`) へも出力。
11. 例外発生時は `log_exception` がスタックトレースを JSONL に保存し、`notify_error` が Slack 通知を試みた上で再送出。

## 4. データ取得と指標計算
- `CCXTConfig` 既定値は `exchange_id="binance"` だが、スクリプト側で `bitflyer` を指定して利用する想定。
- `fetch_ohlcv_latest_ccxt` は `limit+5` 本を取得し確定足に限定、`df.attrs['meta']` に取得メタ情報（シンボル・手段・保存パスなど）を付与。
- `add_sma_columns` は長期窓が計算可能になるまで `NaN` を返すため、`detect_golden_cross_latest` 前に行数チェックを実施。
- `detect_golden_cross_latest` は直近バーと前バーの SMA を比較し、閾値 `epsilon` で微小な差分を許容。
- `fetch_ohlcv_range_ccxt` は開始・終了時刻を指定して複数リクエストにまたがる OHLCV を取得するヘルパー。bitFlyer など `fetchOHLCV` を提供する取引所では自動でページングし、fallback 時は取引履歴を集計する。

## 5. 注文・損益・状態管理
- `OrderParams` でモード、想定ノーション、スリッページ／手数料（bps）、API キーを指定。
- Paper モード：指定スリッページ分だけ価格を補正し、手数料を計算して内部約定。
- Real モード：bitFlyer ccxt クライアントを初期化し、`amount_to_precision`・`limits.step` を考慮した数量調整 `_fit_amount_to_market` を実施。
- 取引履歴 `data/trades/trades.csv` は `pd.concat` で追記し、エントリー・クローズの両方に詳細を残す。summary 行には PnL やエントリー／クローズ価格が JSON で含まれる。
- `StateStore` は `.lock` ファイルで排他制御し、保存時に `.tmp` → `os.replace` を行い `.bak` でバックアップを維持。
- `BotState` は `position`（"FLAT"/"LONG"）、建玉情報、累積 PnL、連敗数、日次サマリ送信有無などを保持。

## 6. 通知・ロギング・メトリクス
- Slack 通知は `SlackConfig` の明示 URL か `SLACK_WEBHOOK_URL` を使用し、指数バックオフ（`backoff_factor=1.6`）付きで最大 3 回リトライ。
- 通知種別：GC 検出、エントリー、クローズ（PnL/累計）、エラー、任意ステータス、日次サマリ。
- JSONL ログ (`data/logs/jsonl/YYYYMMDD.jsonl`) にはステージ遷移・シグナル・注文・決済・例外・日次メトリクスが記録される。
- `metrics.write_daily_metrics` は `trades.csv` から当日分の勝敗・PnL・最大ドローダウンを算出し、`data/metrics/metrics.csv` を上書き更新。

## 7. コンフィグレーションと環境変数
- `.env` を `load_env_settings()` が読み込み（python-dotenv がインストールされている場合）。
- 主な環境変数:
  - `SLACK_WEBHOOK_URL`: Slack Webhook。
  - `BFX_API_KEY` / `BFX_API_SECRET`（互換: `BITFLYER_API_KEY` / `BITFLYER_API_SECRET`）: Real モード用 API 認証。
  - `GC_CANDLES_LOG_DIR`, `GC_CANDLES_DATA_DIR`, `GC_TRADES_DIR`, `GC_METRICS_DIR`, `GC_APP_LOG_DIR`, `GC_JSONL_DIR`: データ保存先の上書き。
- CLI/Scheduler では `--notional-fraction` をセットすると初期資金＋累積 PnL に対する割合でノーションを算出する。
- `--use-rsi-filter` と `--rsi-period` / `--rsi-min` / `--rsi-max` を指定すると、本番ランナーでも GC+RSI 戦略を利用できる。

## 8. 補助スクリプト
- `scripts/run_once.py`: 単発サイクル実行。Slack で開始/成功/失敗を通知し、必要に応じて RSI フィルタ設定を付与できる。
- `scripts/run_scheduler.py`: `schedule` ライブラリで毎時 :05 開始の実行。初回は即時実行。RSI フィルタ設定にも対応。
- `scripts/backfill_data.py`: 指定 ccxt 設定で最新 OHLCV キャッシュを取得し保存。
- `scripts/backfill_history.py`: 指定期間（既定は直近365日）の OHLCV を一括取得し、Parquet/CSV に保存する。
- `scripts/test_real_trade.py`: API キー疎通確認のための小額買い→売りシーケンス。`--dry-run` で送信抑止可能。
- `project.scripts` エントリーポイント `gc-bot-run` は `gc_bot.cli:main` を指し、`pip install -e .` 後にコマンドで単発実行できる。

## 9. バックテスト
- `gc_bot/backtest.py` の `run_backtest` が過去 OHLC データ上で GC 戦略をシミュレート。
  - Paper モードと同等のスリッページ・手数料を考慮し、TP/SL・強制クローズを判定。
  - `BacktestResult` にトレード一覧、エクイティカーブ、勝率・最大ドローダウン・リターン・シャープレシオなどのサマリを保持。
- ノートブック `notebooks/GC_backtest.ipynb` から `BacktestConfig` を調整しつつ `run_backtest` を活用可能。

## 10. テスト
- `tests/test_smoke.py`: SMA 付与と GC 判定の基本挙動を検証。
- `tests/test_state.py`: `should_open_from_signal` の判定と `BotState` デフォルト値を確認。
- `pytest` 実行で最小限のリグレッションチェックが可能。追加テストは `tests/` 以下に拡張する。

## 11. 運用・監視上の注意
- Slack Webhook や API キーが未設定の場合、通知・実発注はスキップされるため環境変数の設定を必ず確認。
- `data/` 配下の CSV/JSONL は実行ごとに追記されるため、ローテーションまたは外部ストレージへの退避を推奨。
- Real モード前に Paper モードで十分な期間のドライランを行い、`trades.csv`・`metrics.csv` から期待どおりの損益推移を確認する。
- 例外発生時は JSONL の `type=exception` レコードおよび Slack のエラー通知で詳細を確認し、必要に応じて再試行またはバックアップから状態復元を行う。
