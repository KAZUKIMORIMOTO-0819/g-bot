# GC_backtest.ipynb 仕様書

## 1. 概要
- `notebooks/GC_backtest.ipynb` は SMA30/60 ゴールデンクロス戦略のバックテストを実施するためのノートブック。
- 既存のローソク足ログ (`data/candles/logs/xrpjpy_1h_*.csv`) を結合し、`gc_bot` パッケージのバックテスト機能 (`run_backtest`) を呼び出して結果を可視化する。

## 2. 前提
- `gc_bot` パッケージに同梱されたバックテストユーティリティが import 可能であること。
- ローカルに 1 時間足 CSV ログが保存されていること（デフォルト: `../data/candles/logs`）。
- Python 環境には `pandas`, `matplotlib`, `gc_bot` の依存ライブラリが導入済み。

## 3. セル構成
1. **イントロダクション (Markdown)**: ノートブックの目的を説明。
2. **依存モジュール import**: dataclasses / pathlib / pandas / matplotlib / `gc_bot` の `BacktestConfig`, `BacktestResult`, `BacktestTrade`, `OrderParams`, `SignalParams`, `run_backtest` を読み込む。
3. **データロード用ヘルパー定義**: `load_cached_ohlcv()` 関数を定義。
4. **バックテスト実行関数定義**: `run_gc_backtest()` で `BacktestConfig` を組み立てつつ `run_backtest` を呼び出す。
5. **データ読込セル**: 例として直近 200 件の CSV を結合し DataFrame (`df`) を作成。
6. **パラメータ設定セル**: `SignalParams(short_window=30, long_window=60)`・`OrderParams(mode="paper", notional_jpy=5000.0, slippage_bps=5.0, taker_fee_bps=15.0)` に加え、`initial_capital` (総資金) と `notional_fraction` (投入割合) を設定。
7. **SMA付与セル**: `add_sma_columns()` で `df_with_ma` を生成し、バックテストおよび描画用の移動平均列を用意。
8. **バックテスト実行セル**: `run_gc_backtest(df_with_ma, ..., initial_capital, notional_fraction)` を呼び出し `result.summary` を取得。
9. **主要指標表示セル**: サマリからトータルリターン・最大ドローダウン(円/%)・シャープレシオをフォーマットして表示。
10. **トレード明細セル**: `result.trades` を DataFrame 化し内容を確認。
11. **ローソク足+移動平均描画セル**: 直近ローソク足にエントリー/イグジットと SMA30/60 を重ねて表示 (`plot_candles_with_trades`)。
12. **エクイティカーブ描画セル**: `%matplotlib inline` のもと `result.equity_curve` を折れ線グラフ表示。
13. **まとめ (Markdown)**: 手順の再確認とパラメータ調整の促し。

## 4. 関数仕様
### 4.1 `load_cached_ohlcv(log_dir: Path = Path("../data/candles/logs"), limit_files: Optional[int] = None) -> pd.DataFrame`
- 処理: 指定ディレクトリ内の `xrpjpy_1h_*.csv` を読み込み、`close_time_jst` をインデックスとする OHLCV DataFrame を作成。
- 主な挙動:
  - `limit_files` が指定されれば末尾から指定件数だけ読み込む。
  - 重複インデックスは `keep="last"` で後勝ち。
  - `open/high/low/close/volume` 列のみ抽出し、時系列順にソートして返す。
- エラー: ファイルが存在しない場合は `FileNotFoundError`。

### 4.2 `run_gc_backtest(df_ohlcv, signal_params=None, order_params=None, **backtest_kwargs) -> BacktestResult`
- 処理: 渡された DataFrame とパラメータから `BacktestConfig` を生成し `run_backtest()` を実行。`initial_capital` と `notional_fraction` を指定すると、各トレードの想定投資額が総資金×割合で動的に決定される。
- 既定値: `SignalParams()` / `OrderParams()` を fallback とし、`BacktestConfig` の他パラメータ (`force_close_last`, `prefer_take_profit_when_overlap`, `initial_capital`, `notional_fraction`) は `**backtest_kwargs` で上書き可能。
- 戻り値: `BacktestResult`（`trades`, `equity_curve`, `summary` を保持）。summary には `capital_initial` / `capital_final` / `notional_fraction` なども含まれる。

### 4.3 `plot_candles_with_trades(df_plot, trades, title='...', max_trades=None)`
- 処理: 指定した OHLCV DataFrame をローソク足で描画し、トレードのエントリー(▲)・エグジット(▼)を重ねる。
- 追加表示: DataFrame に `sma_short` / `sma_long` が存在する場合はゴールデンクロス判定に用いた移動平均を同時に描画。
- 注意: `df_plot` は `DatetimeIndex` 必須。`max_trades` で描画するトレード数を制限可能。

## 5. 実行手順
1. ノートブックを開き、上から順にセルを実行。
2. データ読み込みセルで `limit_files` やパスを必要に応じて変更。
3. パラメータ設定セルで SMA 窓幅、想定証拠金、スリッページ/手数料を調整。
4. バックテスト結果 (`result.summary`) を確認し、必要に応じてトレード詳細 (`trades_df`) やエクイティカーブを分析。

## 6. 出力物
- `result.summary`: トレード数、勝敗、合計PnL、トータルリターン、最大ドローダウン(円/%)、シャープレシオ、初期/最終資金、使用割合などの要約。
- `trades_df`: 各トレードのエントリー/クローズ時刻、利確/損切理由、サイズ、PnL。
- `result.equity_curve`: 実現損益の累積推移（プロット済み）。
- ローソク足＋移動平均チャート: 直近 240 本のローソク足と SMA30/60、エントリー/イグジットを重ねた可視化。

## 7. 拡張ポイント
- `load_cached_ohlcv()` の `log_dir` を差し替えることで他銘柄のCSVにも対応可能。
- `run_gc_backtest()` の `backtest_kwargs` で `force_close_last=False` 等の検証条件変更が可能。
- 描画セルを応用して、`trades_df` から各種統計や可視化を追加すると分析が充実する。
