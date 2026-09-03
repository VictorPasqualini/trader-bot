# Hodlster

A multi-strategy crypto trading bot with a local web dashboard.

The name is a hamster wearing a HODL sign, and the mascot is the honest
description of the engine: it sits still for weeks, stuffs a position into its
cheek when the price is wrong, and empties the pouch when the price is right.
Seventeen allocations on 4h and 1d candles do almost nothing almost all of the
time, and the interface is built around making that stillness legible rather
than alarming.

## Objective

Most trading bots ship a strategy and assume it works. This one is built around
the opposite premise: **almost nothing works, and the hard part is telling the
difference.** The project exists to find out — empirically, on real market
history — which strategies hold up on data they were never fitted to, and to
trade only those, against a real exchange API with fake money.

The dashboard answers "is this actually making money, and why?" — equity,
realised and unrealised P&L, win rate, profit factor, drawdown, Sharpe, and a
per-strategy breakdown, updated live.

## What it does

1. **Downloads** years of real candle history from Binance's public API.
2. **Sweeps** 13 strategies across hundreds of parameter combinations each, on
   every symbol and timeframe you select.
3. **Validates** every survivor on a held-out slice of history the optimiser
   never touched, and rejects anything that does not stay profitable there.
4. **Ranks** what is left by risk-adjusted return, penalising small samples,
   deep drawdowns, and edges that came from one lucky stretch.
5. **Walks the survivors forward** through a decade of rolling quarters, on the
   exact parameters they would trade with, and reports how many of those
   quarters they actually won.
6. **Trades** the strategies you approve on the **Binance Spot Testnet** — real
   API, real order book, fake money — one position per symbol.
7. **Reports** everything in a local dashboard with no build step: every entry
   and exit, per coin, with the indicator values that triggered it, the strategy
   that decided it, the result in cash and percent, and how long it took.

![stack](https://img.shields.io/badge/python-3.11%2B-blue) ![license](https://img.shields.io/badge/license-MIT-green)

## Quick start

```bash
python -m venv .venv
.venv/Scripts/activate          # Windows
# source .venv/bin/activate     # macOS / Linux

pip install -r requirements.txt
cp .env.example .env            # then paste your testnet keys
python run.py
```

The dashboard opens at <http://127.0.0.1:8777>.

Get free testnet keys at <https://testnet.binance.vision/> (log in with GitHub,
click *Generate HMAC_SHA256 Key*). The account is pre-funded with fake USDT.

Verify the connection at any time:

```bash
python run.py check
```

## How it works

```
market history (Binance public API, up to 5000 candles)
        |
        v
  research sweep  ->  optimise parameters on the first 65% of history
        |               score on the held-out 35% (out-of-sample)
        v
   leaderboard    ->  only candidates that stay profitable out-of-sample,
        |               beat buy-and-hold, and earn money across sub-periods
        |               are marked "aprovada"
        v
  walk-forward    ->  the same parameters re-run over eight rolling quarters,
        |               judged on how many they won, not on the total
        v
  live engine     ->  runs the chosen strategies on the testnet,
                      one position per symbol, acting on closed candles only
```

Research reads history from Binance's **public production** API, because the
testnet's own history is shallow and partly synthetic. Orders, balances and
account state come from the **testnet**. The two are deliberately separate.

### Why the split matters

Any strategy can be tuned until it looks brilliant on the data used to tune it.
That number is worthless. The research engine therefore never ranks on the data
it optimised against: parameters are fitted on the older slice, and the ranking
you see comes from the newer slice the optimiser never touched.

A candidate is marked **aprovada** only when the held-out slice:

* is profitable,
* has a Sharpe above 0.3,
* contains at least 3 trades,
* beats buy-and-hold over the same window, and
* turns a profit in at least half of eight equal sub-periods.

That last gate — consistency — is what separates a repeatable edge from one
lucky rally, and it also scales the ranking score.

Costs are charged on both sides of every simulated trade — 0.1% fee plus a
0.05% slippage assumption — and paper trading charges the same, so simulated
and live numbers stay comparable.

### Execution model

The backtester is deliberately pessimistic:

* the position decided at bar `t`'s close is filled at bar `t + 1`'s open, so
  no signal can ever see the price it trades at;
* stops and targets are checked against each bar's own high and low, and when a
  bar touches both, the stop is assumed to hit first;
* after a protective exit the strategy stands aside until its own signal drops
  and turns long again, so a stop can never re-enter the position it just closed;
* the live engine drops the forming candle and acts only on closed ones, which
  is what makes live behaviour match the backtest;
* the live engine also enters only on a signal *transition*. Strategies hold a
  position between their entry and exit pulses, so an allocation added to the
  book while its signal is already long would buy a move that started days
  earlier — a trade no backtest ever makes. Measured over 900 candles of the
  seventeen live allocations, joining a run one candle late costs about 0.8 points
  of mean trade return, and five candles late costs 2.6 points and six points of
  win rate. So a new allocation waits for its signal to drop and turn long
  again, at the cost of the run already in progress.

### Walk-forward validation

One train/test split tests one regime transition, and a strategy can clear it
because the held-out slice happened to suit it. Walk-forward asks the question
repeatedly: fit on 365 days, trade the 90 that follow, step forward, repeat.
What comes out is a distribution rather than a number — how many of those
quarters were profitable, how bad the worst one was, and whether the fitted
parameters kept changing.

Two modes, answering different questions:

* **fixed** (the default for live allocations) carries the deployed parameters
  through every window. "Does what is running right now survive these periods?"
* **refit** re-optimises on each window. "Could this strategy have worked here?"

Measured on this project's six live allocations, fixed beat refit on five of
six, and by wide margins on three — 365 daily bars cannot support a 500-point
parameter grid, so refitting that often is curve fitting with extra steps.

Indicators are computed on the full frame and only the *trading* is restricted
to the test window, so the fitting period doubles as warm-up. Without that, a
100-bar volatility filter on a 90-bar window is undefined for the entire window
and the strategy silently makes no trades at all.

```bash
python run.py walkforward XRPUSDT 1d bollinger_breakout
python run.py walkforward XRPUSDT 1d bollinger_breakout --params '{"period": 20, "mult": 2.0}'
```

The dashboard's **Validação** tab runs this across every live allocation and
shows the per-quarter table behind each verdict.

### Symbol profiling

Validation rates vary from 0% to 39% across symbols — a spread far wider than
the gap between strategies. `python run.py screen SYMBOL [SYMBOL ...]` measures
the shape of a price series: drift ratio, Hurst exponent, lag-1 autocorrelation,
the share of bars in a directional regime, realised volatility.

**These numbers do not predict where strategies validate, and nothing in the
pipeline consults them.** Two screens were built on them. The first, ranking on
how cleanly a symbol trended, correlated 0.03 with the validation rate. The
second ranked on the two measures that *did* correlate on a 20-symbol sweep — and
because those measures had been chosen for correlating, it was tested properly:
run on twenty fresh symbols with its ranking written to disk before the sweep
started, it scored Spearman −0.17 against validation rate, and its top half
validated slightly worse than its bottom half.

The module is kept as a descriptive profiler, with that record in its docstring.
Every strategy family is still swept on every symbol.

### Execution parity and coverage

Two reports exist to keep the live run comparable to the backtest that justified
it, both in the dashboard and both readable through the API.

`bot/parity.py` matches every live trade against the trade the backtest would
have made on the same candles, and reports the difference in decision bar, fill
price and realised return. This is the primary go-live evidence: it is pairwise,
so a timing or pricing defect is visible immediately instead of being averaged
into a win rate that would take dozens of trades to estimate.

Parity scores the engine as it stands now, not as it used to be. Trades entered
before `GUARD_LANDED` — the moment the stale-entry guard shipped — are listed
for the record and excluded from every total, because a sample that still
contains a fixed defect makes the current engine look worse than it is, and
makes the next fix look like an improvement that no live trade caused. Set the
kv key `parity_baseline` to move the line the next time the engine changes. The
money those trades made or lost stays in equity, realised P&L and the drawdown
gate; only the verdict is withheld.

`bot/coverage.py` derives, from the per-tick equity snapshots, which candle
closes the process was actually alive for. This exists because downtime is not
neutral. The engine reads the signal on the last closed candle, so a bot that
wakes up two days into a move sees a flat-to-long transition that is two days
stale and buys the top of it — measured at 25.8% and 27.8% worse than the
modelled fill on the first two live trades. `MAX_ENTRY_LAG_BARS` in
`bot/live.py` now refuses any entry more than one candle after the signal turned
and logs the miss instead, so a gap costs a skipped trade rather than a bad one.

This is also why the bot trades 4h and 1d rather than something faster. A round
trip costs 0.30%; the median 15m candle moves 0.122% and the median 4h candle
0.624%, so on the fast timeframe most candles cannot pay for the trade that
crosses them. A full 15m sweep of 40 symbols does validate 59 combinations, but
3000 candles of 15m is 31 days of history and only one of the top six survived a
walk-forward on 180 days. On top of that, 15m closes 96 candles a day against 6
at 4h, and with the entry guard in place every close the machine sleeps through
is a skipped trade. Faster is worse here for two independent reasons.

### Portfolio risk controls

Per-trade stops buy drawdown reduction with return and are destructive on mean
reversion (measured below). These act on the book instead, where they cannot
pre-empt a strategy's own exit. All three are **off by default** and configured
under **Ajustes → Risco da carteira**.

| Control | Effect |
| --- | --- |
| Equity kill switch | Stops opening new positions past a drawdown from peak equity. Open positions keep their own exits — closing everything at the bottom is the behaviour the stop study measured as destructive. Resumes only after recovering past a separate, smaller threshold, so the switch does not chatter |
| Volatility-scaled sizing | Scales each order by recent realised volatility against a 2.3% daily reference, clamped to 0.4×–1.6×, so a flat quote amount means the same risk in a quiet symbol as in a violent one |
| Correlation cap | Refuses an entry correlating above the threshold with an already-open position |

## Strategies

| Family | Strategy | Idea |
| --- | --- | --- |
| Trend | EMA Crossover | Fast EMA over slow EMA, optional long-term trend filter |
| Trend | MACD Trend | Long while the MACD histogram is positive |
| Trend | Supertrend | ATR-banded trend follower |
| Trend | ADX Filtered Trend | EMA trend entries, only in directional markets |
| Breakout | Donchian Breakout | Turtle-style N-bar high entries |
| Breakout | Bollinger Breakout | Rides volatility expansion |
| Reversion | Bollinger Mean Reversion | Fades stretched moves back to the mean |
| Reversion | RSI Mean Reversion | Buys oversold, exits on recovery |
| Reversion | Stochastic Reversion | %K crossing up out of oversold |
| Reversion | Rolling VWAP Reversion | Buys z-score dips below rolling VWAP |
| Momentum | Momentum (ROC) | Time-series momentum with a volatility filter |
| Ensemble | Ensemble Vote | Holds when several sleeves agree |
| Benchmark | Buy & Hold | The bar every strategy has to clear |

Each ships a parameter grid the research engine sweeps, then a small risk grid
(fixed stop, stop + target, trailing stop) is fitted to the finalists.

## What the research found

Measured across two runs: 540 candidates on 5 large-cap symbols, then 1440
candidates on 20 symbols. "Validation rate" is the share that survived
out-of-sample; "median alpha" is the median out-of-sample return minus
buy-and-hold over the same window. See [ROADMAP.md](ROADMAP.md) for the trail.

| Family | Rate, 5 majors | Rate, 20 symbols | Median alpha, 20 symbols |
| --- | --- | --- | --- |
| Momentum | 11.1% | 12.5% | +8.6pp |
| Breakout | 12.2% | 9.6% | **+25.1pp** |
| Reversion | **0.6%** | **9.2%** | +22.1pp |
| Ensemble | 6.7% | 8.3% | +16.9pp |
| Trend | 6.1% | 6.2% | +19.4pp |

Four results are worth internalising before trusting any leaderboard.

**The tradeable universe changes which strategies work — more than the
strategies themselves do.** On BTC, ETH, BNB, SOL and XRP, mean reversion
validated once in 180 attempts and looked definitively dead. Widening to 20
symbols took it from 0.6% to 9.2%, with a median alpha above 20 points. Nothing
about the strategies changed. The first conclusion was not wrong about the
data — it was wrong about how far the data generalised.

**Strategies beat buy-and-hold on choppy markets, not on crashes or rallies.**
Ranking symbols by their buy-and-hold return over the validation window, the
best validation rates sit in the middle of the range — UNI (−32% buy-and-hold,
36% validated), ETH (+27%, 39%), DOGE (−27%, 33%) — while both extremes are
barren: DOT (−87%, 5.6%) and ATOM (−84%, 5.6%) at one end, TRX (+301%, 0%) and
BNB (+188%, 5.6%) at the other. The mechanism is intuitive once seen: a
one-directional crash offers a long-only strategy nothing to catch, and a
relentless rally cannot be beaten by anything that ever sits in cash. Edges live
where there are swings to trade.

**Daily candles beat intraday.** `1d` validated at 11.4% against `4h` at 5.6%
across 20 symbols, and at 12.8% against 3.3% and 1.1% for `1h` in the earlier
run. The candle cap is on count, not on time, so 5000 daily candles reach back
three years while 5000 hourly candles cover seven months — and a validation
window that short is usually one market regime rather than several. The `4h`
sweep also showed the worst overfitting gap, losing 1.57 Sharpe from in-sample
to out-of-sample.

**Protective exits cut drawdown and do not add return.** This was measured
twice. The first study, against 31 candidates, ran on a backtester that bought
straight back in on the bar a stop fired, at that bar's open — above the level it
had just sold at. Every stop exit was a guaranteed loss, so no stop could
possibly have helped, and the study measured its own defect. That is fixed: after
a protective exit the strategy stays flat until its own signal drops and turns
long again.

The corrected test covers all 194 distinct validated candidates from every
research run, with each exit multiple fixed across all of them — never fitted per
candidate — and measured only on the out-of-sample slice.

| Configuration | Median Δ return | Mean Δ drawdown | Median Δ Calmar | Better / worse |
| --- | --- | --- | --- | --- |
| ATR stop 1.5× | −1.94pp | **+2.90pp** | −0.023 | 92 / 100 |
| ATR stop 2.0× | −0.60pp | +1.37pp | 0.000 | 90 / 94 |
| 5% stop | −1.52pp | **+5.97pp** | −0.006 | 84 / 97 |
| 8% stop | 0.00pp | +3.38pp | 0.000 | 77 / 82 |
| 5% stop + 10% target | −21.74pp | **+15.55pp** | −0.267 | 46 / 147 |
| ATR trail 6.0× | −8.01pp | +1.53pp | −0.178 | 41 / 118 |
| ATR trail 2.0× | −47.01pp | +6.18pp | −0.802 | 9 / 183 |

Stops do what stops are supposed to do: a moderate one takes 3–6pp off the worst
drawdown, and one paired with a target takes off 15pp. What they do not do is
improve risk-adjusted return — median ΔCalmar is zero or negative for every
variant, and none wins on more candidates than it loses on. Trailing stops are
strictly destructive: a trail tight enough to protect anything exits a position
the strategy is still right about, and it cannot re-enter until its signal
cycles.

ATR scaling did not beat plain percentages. The argument for it — that 5% means
something different on every symbol — is sound, and at this sample size it simply
did not show up.

**The family breakdown inverts the obvious expectation.** Mean reversion is the
strategy with no natural stop: it exits when price returns to its mean, which in
a sustained move may be never. It is also the family a stop damages most — a 5%
stop costs the median reversion candidate **20.6pp** and wins on 24 of 73. The
reason is in the entry: reversion buys *after* a decline, so a stop below the
entry sits directly in the path of the continuation. It is a rule that sells the
bottom. Trend and breakout enter on strength, so their stop only fires when the
thesis is already broken — and there the numbers lean slightly positive (5% stop
on trend: +6.5pp drawdown, better on 23 of 43), though not far enough from a coin
flip to act on.

So the conclusion is not "risk management does not matter". It is that a
per-trade price stop trades return for drawdown at roughly a fair price, and is
actively wrong for mean reversion. `bot/backtest.py` supports ATR and percentage
exits and the research risk grid can fit them, but no live allocation uses one.
Risk is managed at the book level instead — see **Portfolio risk controls**
above.

## Current live allocation

Seventeen strategies across five families. The first six were chosen from the
20-major sweep under criteria stricter than the validation gate — at least 10
out-of-sample trades, consistency at or above 62.5%, at least 30 points of alpha,
and a positive Sharpe in both slices. Four more were harvested afterwards from
the validated candidates that no allocation had ever used, and the last seven
came from a walk-forward of the 20 highest-scoring validated candidates still
outside the book, of which 17 held up.

Every one is walked forward across eight rolling quarters *on the exact
parameters it trades*, which is a much less flattering test than the single
held-out split that first surfaced it. All seventeen pass.

| Symbol | Timeframe | Strategy | Quarters profitable | Beat buy-and-hold | Median quarter | Worst quarter | Worst drawdown | Trades |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| XRPUSDT | 1d | Bollinger Breakout | 75.0% | 50.0% | +7.5% | −10.9% | −18.8% | 14 |
| AAVEUSDT | 1d | Rolling VWAP Reversion | 87.5% | 62.5% | +19.6% | −22.7% | −37.0% | 17 |
| DOGEUSDT | 1d | RSI Mean Reversion | 75.0% | 62.5% | +7.8% | −12.4% | −18.0% | 12 |
| XLMUSDT | 4h | Bollinger Breakout | 62.5% | 75.0% | +13.2% | −10.5% | −24.1% | 48 |
| ETHUSDT | 1d | Momentum (ROC) | 62.5% | 62.5% | +8.5% | −26.6% | −36.3% | 56 |
| IMXUSDT | 4h | Rolling VWAP Reversion | 62.5% | 75.0% | +7.0% | −6.8% | −16.4% | 33 |
| ATOMUSDT | 4h | Momentum (ROC) | 75.0% | 75.0% | +5.9% | −29.8% | −31.5% | 144 |
| ARBUSDT | 4h | Bollinger Breakout | 62.5% | 75.0% | +12.8% | −11.3% | −20.8% | 46 |
| CHZUSDT | 4h | Ensemble Vote | 62.5% | 75.0% | +10.1% | −18.5% | −21.1% | 69 |
| NEARUSDT | 4h | Bollinger Mean Reversion | 87.5% | 62.5% | +5.3% | −3.7% | −18.2% | 25 |
| ALGOUSDT | 4h | RSI Mean Reversion | 87.5% | 50.0% | +17.9% | −4.1% | −23.2% | 53 |
| ETCUSDT | 4h | Rolling VWAP Reversion | 62.5% | 75.0% | +10.9% | −6.6% | −21.4% | 33 |
| GRTUSDT | 4h | RSI Mean Reversion | 87.5% | 87.5% | +11.0% | 0.0% | −20.2% | 21 |
| DOTUSDT | 4h | Bollinger Breakout | 75.0% | 87.5% | +8.8% | −25.1% | −27.9% | 44 |
| CRVUSDT | 1d | Bollinger Mean Reversion | 87.5% | 50.0% | +15.6% | −22.8% | −37.4% | 18 |
| UNIUSDT | 1d | Rolling VWAP Reversion | 62.5% | 62.5% | +15.8% | −6.0% | −16.5% | 7 |
| SEIUSDT | 1d | Rolling VWAP Reversion | 75.0% | 75.0% | +7.3% | −21.7% | −34.2% | 14 |

The selection favours *strategy* diversification over *symbol* diversification,
because crypto assets are heavily correlated with each other while breakout,
reversion and momentum fail in different conditions.

Additions used to be capped at 0.80 correlation of daily returns against
everything already in the book, which is why GRTUSDT and DOTUSDT were absent for
several phases despite winning seven and six of eight quarters. That cap was
replaced, because it measured the wrong thing. Correlated *prices* only produce
correlated *losses* if the strategies are in the market at the same time, and
these are mostly reversion rules with different lookbacks and thresholds, so
they rarely are. Measured over 501 days of the current book: the highest
position overlap between any two allocations is a Jaccard index of 0.45, the
average number of simultaneous positions is 3.3, the maximum ever observed is 9
of 17, and there are 23 days with no position at all. The criterion is now
overlap rather than price correlation.

AAVEUSDT, CRVUSDT, ETHUSDT and SEIUSDT reach drawdowns of 34–37% on their own,
before any correlation with the rest of the book. They are the four worth
watching. GRTUSDT is the opposite case and worth reading carefully: a worst
quarter of exactly 0.0% does not mean it cannot lose, it means that in the worst
of its eight quarters the strategy never took a trade. An untested quarter is
not a survived one.

Each allocation trades a fixed quote amount rather than the whole account, so
live returns scale to roughly a tenth of the single-strategy backtest figures.
That is the intended trade-off: less concentration for less variance. Fully
deployed, seventeen positions at 500 USDT would be 8500 of the 10 000 notional —
but the overlap measurement above says the realistic peak is closer to 4500, and
the typical commitment closer to 1650.

## Using the dashboard

The sidebar has five views, and they are ordered by how often you need them.

| Menu | What it is for |
| --- | --- |
| **Painel** | The state of the money right now: equity curve, P&L broken into realised and open, win rate, profit factor, drawdown, Sharpe, and what every allocation is currently watching. The page to open first and to leave open. |
| **Laboratório** | Where allocations come from. Runs the parameter search over history, ranks candidates on out-of-sample results only, and lets you promote the survivors into the live book. Nothing here trades; it produces candidates. |
| **Operações** | The audit trail. Every buy and sell in the order they happened, and the same trades grouped by coin with the signal that opened and closed each one. Answers "what did it do, and why". |
| **Validação** | Whether the book deserves real money. A checklist that can say no, plus the walk-forward table behind it — each allocation re-tested quarter by quarter on the parameters it is actually deployed with. |
| **Ajustes** | Execution mode (`paper` or `testnet`), size per order, portfolio risk limits, and the list of active allocations. The only view that changes what the bot does. |

1. **Laboratório** — pick pairs and timeframes, hit *Rodar pesquisa*. Prefer
   `1d` and `4h` with 5000 candles, for the reason above. A few minutes later
   the ranking fills in. Click any row for its equity curve and the
   in-sample/out-of-sample comparison.
2. Tick the strategies you want and press *Operar selecionadas*. One allocation
   per symbol is enforced — two strategies on the same asset would fight over
   the same spot balance.
3. **Ajustes** — choose `testnet` (real orders, fake money) or `paper`
   (simulated fills, nothing sent), set the size per trade, save.
4. Press **Ligar robô**. Every cycle it reads the last *closed* candle, applies
   the strategy, and buys or sells accordingly.
5. **Painel** shows equity, P&L, win rate, profit factor, drawdown and Sharpe.
   The *Lucro e perda* panel spells out the arithmetic in order — starting
   capital, what closed trades did to it, what open trades are currently doing
   to it, what is left — because a single total hides the difference between
   money that is banked and money that can still evaporate. The estimated fees
   paid so far are shown underneath; they are already deducted from every other
   number on the page. *Sinais agora* lists every allocation with the one
   comparison it is waiting on — the value measured on the last closed candle,
   the level it has to cross, and the distance between them — sorted so the
   closest to firing is at the top. Seventeen allocations on 4h and 1d candles
   are silent most of the time, and this panel is what distinguishes a bot that
   is waiting from a bot that is stuck. For a coin already held the row flips to
   the exit rule, because that is the decision actually pending.
6. **Operações** opens with *Compras e vendas*, the raw order ledger: every buy
   and every sell in the order they happened, with the cash movement, the
   realised result where there is one, and the reason. It is not grouped,
   because a statement that reorders itself is not a statement. Below it,
   *Operações por moeda* groups every entry and exit by coin. Each row expands into the
   signal that opened the position and the one that closed it — the rule in
   words, the indicator values at that candle, the price paid, and the time
   between them. The card names the candle the rule fired on, which is not
   always the candle the order was sent on: orders are filled on the open after
   the decision, so the two are normally one candle apart. The *Sinal de compra*
   column carries the decisive pair on one line — the measured value against the
   level it crossed — which is the same number *Sinais agora* tracks before the
   trade exists. Simulated rows carry it too, so the column is readable before
   the bot has made a single live trade. The *Simulado* toggle
   replays the same allocations over recent history, so the view is readable
   before the bot has closed its first trade; those rows are a simulation, not
   money made.
7. **Validação** opens with *Pronto para conta real?*, a checklist that compares
   what the walk-forward expects against what the live run has actually
   produced, and can say no. Below it, the walk-forward itself walks each live
   allocation across eight rolling quarters on its deployed parameters and shows
   the quarter-by-quarter table behind the verdict. This is the number to trust
   — a single backtest total is not.

## Terminal usage

```bash
python run.py check                              # connectivity + keys
python run.py research --symbols BTCUSDT ETHUSDT --intervals 1d 4h
python run.py backtest BTCUSDT 1d bollinger_breakout
python run.py walkforward XRPUSDT 1d bollinger_breakout
python run.py screen BTCUSDT ETHUSDT XRPUSDT SOLUSDT DOGEUSDT
python run.py serve --port 9000 --no-browser
```

## Layout

```
bot/
  config.py       settings from .env
  exchange.py     Binance REST client (market data + signed order calls)
  indicators.py   vectorised technical indicators
  strategies.py   strategy library + parameter grids
  backtest.py     event-driven backtester and metrics
  research.py     sweep, out-of-sample validation, ranking
  walkforward.py  rolling fit/trade windows over the whole history
  screening.py    descriptive price-shape profiling (predicts nothing — see docstring)
  portfolio.py    book-level risk: kill switch, volatility sizing, correlation cap
  live.py         live trading engine
  parity.py       every live trade against the trade the backtest would have made
  coverage.py     which candle closes the bot was actually awake for
  report.py       dashboard aggregations
  storage.py      SQLite persistence
  api.py          FastAPI app
web/              dashboard (no build step, plain HTML/CSS/JS)
data/trader.db    created on first run
ROADMAP.md        what has been tried, what was learned, what is next
```

## Interpreting results honestly

A leaderboard is a list of survivors, and survivors of a large search are partly
survivors of luck. Testing 13 strategies across hundreds of parameter sets on
dozens of symbols means some candidates clear every gate by chance alone. The
out-of-sample split, the buy-and-hold comparison and the consistency gate each
lower that rate, but none of them drive it to zero.

Treat a validated result as *evidence worth testing forward*, not as a finding.
The testnet exists precisely so that forward testing costs nothing.

## Going live with real money

`BINANCE_TESTNET=false` in `.env` points every order at the real exchange. Do
not flip it because a backtest looked good. Fees, slippage, liquidity and regime
changes all bite harder in production than in simulation, and spot trading can
lose money.

The **Validação** view answers the question directly, and is designed to be able
to say no. A backtest describes data the strategy was chosen against; only a
forward run describes data nobody has seen. Six conditions have to hold, and
they fall into two tiers that clear on different clocks.

The **execution tier** — parity and coverage — asks whether the engine does what
the model says. That is a systematic property: a timing or pricing defect
appears in the first two or three paired trades, because each live trade is
compared against its own backtest twin rather than pooled into an average. It is
expected to clear within weeks, and until it does nothing else on the list means
anything, because a book that is profitable while filling somewhere the backtest
never modelled is profitable by accident.

The **evidence tier** — sample, tracking, drawdown — asks whether the edge is
still there, which no amount of careful execution can answer and only time can.

| Gate | Threshold | Why that number |
| --- | --- | --- |
| Every allocation still passes walk-forward | all of them | A book is only as validated as its worst member |
| Live trades matching their backtest twin | 10 | The edge is established by walk-forward, on hundreds of trades. What a live run uniquely proves is that the engine executes the model — same decision candle, fill against the following open, cost inside the assumption — and an execution defect is systematic, so it shows up in the first few paired trades rather than needing a statistical sample |
| Candle closes the bot was awake for | 90% | A candle slept through is invisible afterwards: a strategy that never fired and a strategy that fired while nobody was listening leave the same empty record |
| Closed trades, and days of running | 100 **and** 270 | 270 days is three complete 90-day walk-forward windows, which is the smallest number of realised quarters that can be placed inside the distribution of measured ones — a single quarter is one draw and is consistent with almost any hypothesis. 100 closed trades puts the win rate inside roughly ±10 points; at 30 the interval is ±18 and separates nothing. Both bounds have to clear, because 100 trades inside one volatile month samples one regime, and nine quiet months with forty trades is time without evidence |
| Realised result not worse than the expected worst quarter | −11.97% of capital | A book can be profitable and still be broken; what matters is whether it behaves like the thing that was measured |
| Observed drawdown within the configured limit | 20% | Roughly 1.7× the expected worst quarter, so it fires when something is broken rather than during a normal bad run |

The expectation is scaled to the size actually traded — each allocation's median
quarter divided by three, times its share of capital — which for the current
book of 17 allocations at 500 of 10,000 comes to **+3.08% per month across about
27 trades**, against a worst measured quarter of **−11.97%**. Those are the
numbers a real account should be expected to reproduce before it is funded.

At that trade rate the 100-trade bound arrives in under four months, so the
270-day bound is the one that actually binds — which is the intended shape. The
trade count exists to stop a book being judged on too few results; the calendar
exists to stop it being judged on too few market conditions, and the second is
the harder problem.

The API keys in `.env` are testnet-only. Never commit real keys — `.env` is
already in `.gitignore`.

## License

MIT
