"""Entry point.

    python run.py              start the dashboard on http://127.0.0.1:8777
    python run.py check        verify Binance connectivity and API keys
    python run.py research     run a full strategy sweep from the terminal
    python run.py backtest BTCUSDT 1h supertrend
    python run.py walkforward XRPUSDT 1d bollinger_breakout
    python run.py screen BTCUSDT ETHUSDT SOLUSDT
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import webbrowser

HOST = "127.0.0.1"
PORT = 8777


def cmd_check() -> int:
    from bot.exchange import exchange

    status = exchange.ping()
    print(json.dumps(status, indent=2))
    if not status["market_data"]:
        print("\nMarket data unreachable. Check your internet connection.")
        return 1
    if not status["account"]:
        print("\nKeys rejected. Create testnet keys at https://testnet.binance.vision/"
              " and put them in .env")
        return 1
    print(f"\nOK. {'TESTNET' if status['testnet'] else 'LIVE'} account,"
          f" {status.get('quote_balance', 0):,.2f} USDT free.")
    return 0


def cmd_research(args: argparse.Namespace) -> int:
    from bot import research
    from bot.research import scan_pair

    symbols = args.symbols or ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    intervals = args.intervals or ["1h", "4h"]
    rows: list[dict] = []
    for symbol in symbols:
        for interval in intervals:
            print(f"scanning {symbol} {interval} ...", flush=True)
            rows.extend(scan_pair(symbol, interval, args.candles))
    rows.sort(key=lambda r: r["score"], reverse=True)

    print(f"\n{'symbol':10} {'tf':4} {'strategy':20} {'oos ret%':>9} {'b&h%':>8}"
          f" {'sharpe':>7} {'dd%':>7} {'trades':>7} {'score':>7}  ok")
    for row in rows[:25]:
        test = row["test"]
        print(f"{row['symbol']:10} {row['interval']:4} {row['strategy']:20}"
              f" {test['total_return_pct']:9.2f} {test['buy_hold_return_pct']:8.2f}"
              f" {test['sharpe']:7.2f} {test['max_drawdown_pct']:7.2f}"
              f" {test['trades']:7d} {row['score']:7.2f}  {'YES' if row['validated'] else ''}")
    validated = [r for r in rows if r["validated"]]
    print(f"\n{len(validated)} of {len(rows)} candidates survived out-of-sample validation.")
    return 0


def cmd_backtest(args: argparse.Namespace) -> int:
    from bot import backtest as bt
    from bot import research
    from bot import strategies as st

    frame = research.load_history(args.symbol, args.interval, args.candles)
    result = research.evaluate(frame, st.build(args.strategy), {})
    print(json.dumps(result.metrics, indent=2))
    print("score:", bt.robust_score(result.metrics))
    return 0


def cmd_walkforward(args: argparse.Namespace) -> int:
    from bot import walkforward as wf

    fixed = json.loads(args.params) if args.params else None
    report = wf.run(args.symbol, args.interval, args.strategy,
                    train_days=args.train_days, test_days=args.test_days,
                    fixed_params=fixed,
                    fixed_risk=json.loads(args.risk) if args.risk else None)
    if not report["windows"]:
        print(report["verdict"])
        return 1

    print(f"\n{report['symbol']} {report['interval']} {report['label']} "
          f"({'refit each window' if report['refit'] else 'fixed parameters'})")
    print(f"{report['bars']} bars, {report['start'][:10]} to {report['end'][:10]}, "
          f"fit {args.train_days}d / trade {args.test_days}d\n")
    print(f"{'test window':24} {'return%':>9} {'b&h%':>9} {'sharpe':>7} {'dd%':>8} {'trades':>7}")
    for window in report["windows"]:
        print(f"{window['test_start'][:10]} to {window['test_end'][:10]:12}"
              f" {window['return_pct']:9.2f} {window['buy_hold_pct']:9.2f}"
              f" {window['sharpe']:7.2f} {window['max_drawdown_pct']:8.2f}"
              f" {window['trades']:7d}")
    print(f"\n{report['profitable_windows']}/{report['window_count']} windows profitable, "
          f"{report['beat_buy_hold_pct']}% beat buy-and-hold")
    print(f"compounded {report['compounded_return_pct']:.2f}%, "
          f"median {report['median_return_pct']:.2f}%, "
          f"worst {report['worst_window_pct']:.2f}%")
    if report["refit"]:
        print(f"parameter stability {report['param_stability_pct']}%")
    print(f"\n-> {report['verdict']}")
    return 0


def cmd_screen(args: argparse.Namespace) -> int:
    from bot import screening

    rows = screening.screen(args.symbols, args.interval, args.candles)
    print(f"\n{'symbol':11} {'leans':15} {'drift':>6} {'hurst':>6}"
          f" {'ac1':>7} {'adx>25%':>8} {'vol%':>7} {'b&h%':>9}")
    for row in rows:
        if "error" in row:
            print(f"{row['symbol']:11} error: {row['error']}")
            continue
        drift = row["drift_ratio"]
        print(f"{row['symbol']:11} {row['favours']:15}"
              f" {drift if drift is not None else float('nan'):6.2f} {row['hurst']:6.3f}"
              f" {row['autocorr_1']:7.3f} {row['trending_share_pct']:8.1f}"
              f" {row['vol_annual_pct']:7.1f} {row['buy_hold_pct']:9.1f}")
    print("\nDescriptive only. Two versions of a screen built on these measures were"
          "\ntested against sweeps they had not seen, and neither predicted where"
          "\nstrategies validate - see bot/screening.py. Do not use this to choose"
          "\nwhat to sweep.")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    url = f"http://{args.host}:{args.port}"
    print(f"\n  Hodlster dashboard -> {url}\n")
    if not args.no_browser:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    uvicorn.run("bot.api:app", host=args.host, port=args.port,
                reload=args.reload, log_level="warning")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Hodlster")
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="start the web dashboard (default)")
    serve.add_argument("--host", default=HOST)
    serve.add_argument("--port", type=int, default=PORT)
    serve.add_argument("--reload", action="store_true")
    serve.add_argument("--no-browser", action="store_true")

    sub.add_parser("check", help="verify exchange connectivity")

    research_cmd = sub.add_parser("research", help="run a strategy sweep in the terminal")
    research_cmd.add_argument("--symbols", nargs="*")
    research_cmd.add_argument("--intervals", nargs="*")
    research_cmd.add_argument("--candles", type=int, default=3000)

    wf_cmd = sub.add_parser("walkforward",
                            help="roll a fit/trade window across the whole history")
    wf_cmd.add_argument("symbol")
    wf_cmd.add_argument("interval")
    wf_cmd.add_argument("strategy")
    wf_cmd.add_argument("--risk", help='fixed risk as JSON, e.g. {"stop_pct": 0.05}')
    wf_cmd.add_argument("--train-days", type=int, default=365)
    wf_cmd.add_argument("--test-days", type=int, default=90)
    wf_cmd.add_argument("--params", help='fixed parameters as JSON, e.g. {"period": 20};'
                                         " omit to refit every window")

    screen_cmd = sub.add_parser("screen", help="describe the price shape of symbols")
    screen_cmd.add_argument("symbols", nargs="+")
    screen_cmd.add_argument("--interval", default="1d")
    screen_cmd.add_argument("--candles", type=int, default=720)

    backtest_cmd = sub.add_parser("backtest", help="backtest one strategy")
    backtest_cmd.add_argument("symbol")
    backtest_cmd.add_argument("interval")
    backtest_cmd.add_argument("strategy")
    backtest_cmd.add_argument("--candles", type=int, default=2000)

    args = parser.parse_args()
    if args.command == "check":
        return cmd_check()
    if args.command == "research":
        return cmd_research(args)
    if args.command == "backtest":
        return cmd_backtest(args)
    if args.command == "walkforward":
        return cmd_walkforward(args)
    if args.command == "screen":
        return cmd_screen(args)
    if args.command is None:
        args = parser.parse_args(["serve"])
    return cmd_serve(args)


if __name__ == "__main__":
    sys.exit(main())
