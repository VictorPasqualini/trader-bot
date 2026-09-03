# Roadmap

The trail from an empty repository to a strategy set worth trusting with real
money — what was built, what was measured, what it taught us, and what is left.

Each phase records the **evidence** it produced, not just the work. A phase is
only closed when a number backs it up.

## Definition of done

"Satisfactory" is not "the backtest looks good". The bar this project is aiming
at, in order:

1. A strategy set that beats buy-and-hold **out-of-sample** on at least three
   uncorrelated symbols, by a margin wide enough not to be noise (target: 20+
   percentage points of alpha over the validation window).
2. The same set surviving a **walk-forward** test across several market regimes,
   not a single train/test split.
3. A **forward test** on the testnet, running unattended for at least 90 days,
   whose realised P&L tracks the backtest within a reasonable band.
4. Portfolio drawdown held under 30%, since a 40% drawdown is the point where
   most people abandon a system regardless of its expected value.

Only after step 3 does the question of real money become reasonable to ask.

---

## Phase 0 — Rebuild ✅

The previous codebase was a set of loose scripts with API keys committed in
source. Rebuilt from scratch as ten modules with a clear boundary between
research, execution, and reporting.

**Outcome:** credentials moved to `.env` and gitignored.
**Open risk:** the old keys remain in git history and should be revoked.

## Phase 1 — Infrastructure ✅

| Component | Verification |
| --- | --- |
| Binance testnet connection | `canTrade: true`, SPOT permissions, funded account |
| Signed request path | Clock offset of −1.7s detected and corrected (error −1021) |
| Market data source | Split to the public production API — testnet history is shallow and partly synthetic |
| Order path | BUY and SELL round trip filled, lot-size rounding and notional filters honoured |
| Dashboard | 12 endpoints serving, no build step, canvas charting with no CDN dependency |

**Learned:** the testnet is fine for *execution* and useless for *research*. The
two data sources have to be separate, and the code says so explicitly.

## Phase 2 — First research run ✅

288 candidates on 4 symbols across `1h`/`4h`, 3000 candles. Five validated.

**The result was misleading, and finding out why was the useful part.** 3000
hourly candles cover about four months. The training slice happened to be a
falling market (buy-and-hold −21%) and the validation slice a rising one
(+34%). The strategies were not proving skill; they were proving that a trend
follower does well in an uptrend.

**Learned:** validation window *length* matters more than validation window
*existence*. A held-out slice that contains one regime tests nothing.

## Phase 3 — Hardening ✅

Three defects found and fixed, each caught by observing behaviour rather than
reading code.

| Defect | Symptom | Fix |
| --- | --- | --- |
| Paper mode charged no costs | Paper results were not comparable to backtests | Charge fee + slippage on both sides |
| `tick()` had no lock | Poll thread and manual tick both saw "no position" and bought the same symbol twice — observed as 4 open positions where 2 were expected | `_tick_lock` serialises ticks |
| No consistency gate | A strategy could pass on one lucky rally | `consistency_pct` over 8 sub-periods, required ≥50%, also scales the ranking score |

Also replaced the deprecated FastAPI `on_event` startup hook with a lifespan
handler, so an interrupted research run is marked as such rather than left
`running` forever.

## Phase 4 — Efficacy study ✅ (stop section superseded)

> **Correction.** The stop study below ran against a backtester that re-entered
> a stopped position on the same bar at that bar's open, which made any stop a
> guaranteed loss by construction. Its conclusion that stops worsen drawdown is
> wrong and its conclusion that they do not add return survives for a different
> reason. Phase 9 has the corrected measurement; the strategy comparison in this
> phase is unaffected, since those candidates carried no exits.

Second run: 540 candidates, 5 symbols, `1h`/`4h`/`1d`, 5000 candles. `1d` now
reaches back three years. 31 validated.

### Which strategies actually work

| Strategy | Validated | Rate | Median alpha | Median Sharpe | IS→OOS Sharpe lost |
| --- | --- | --- | --- | --- | --- |
| Bollinger Breakout | 8/45 | 17.8% | +15.3pp | 0.71 | 0.40 |
| ADX Filtered Trend | 6/45 | 13.3% | +4.8pp | 0.77 | 0.32 |
| Momentum (ROC) | 5/45 | 11.1% | −10.9pp | −0.15 | 0.95 |
| Donchian Breakout | 3/45 | 6.7% | +0.8pp | 0.66 | 0.46 |
| EMA Crossover | 3/45 | 6.7% | +1.5pp | 0.64 | 0.30 |
| Ensemble Vote | 3/45 | 6.7% | −11.0pp | 0.41 | 0.44 |
| MACD Trend | 1/45 | 2.2% | −23.7pp | 0.09 | 0.65 |
| Supertrend | 1/45 | 2.2% | −16.6pp | 0.13 | 1.01 |
| VWAP Reversion | 1/45 | 2.2% | −21.3pp | −0.35 | 0.32 |
| Bollinger Reversion | 0/45 | 0.0% | −16.0pp | −0.14 | 0.26 |
| RSI Reversion | 0/45 | 0.0% | −17.7pp | 0.07 | 0.72 |
| Stochastic Reversion | 0/45 | 0.0% | −28.4pp | −0.19 | 0.32 |

Only four strategies have a positive median alpha, and two of them —
Bollinger Breakout and ADX Trend — account for nearly half of all validations.

**Learned:** mean reversion is not merely weak in crypto, it is negative. Zero
validations across 135 reversion candidates, with median alpha around −20pp.
The sweep currently spends a third of its budget on families that have never
produced a survivor.

### Which timeframe works

| Timeframe | Rate | History covered | IS→OOS Sharpe lost |
| --- | --- | --- | --- |
| `1d` | 12.8% | ~3 years | 0.41 |
| `4h` | 3.3% | ~1 year | 1.57 |
| `1h` | 1.1% | ~7 months | −1.37 |

`4h` loses the most Sharpe from in-sample to out-of-sample — the classic
overfitting signature. `1h`'s *negative* gap is not a good sign either: it means
out-of-sample scored higher than in-sample, which is regime luck, not skill.

### Do stops help? No.

11 protective configurations against all 31 validated candidates, measured on
the held-out slice.

| Configuration | Median return | vs. baseline | Median Sharpe | Median drawdown | Median trades | Improved |
| --- | --- | --- | --- | --- | --- | --- |
| No protection | 41.3% | — | 0.71 | −39.2% | 18 | — |
| Stop 3% | 2.9% | −38.4pp | 0.24 | −53.8% | 46 | 0/31 |
| Stop 5% | 12.1% | −29.2pp | 0.43 | −49.3% | 33 | 0/31 |
| Stop 8% | 19.1% | −22.2pp | 0.61 | −46.2% | 26 | 0/31 |
| Stop 12% | 21.0% | −20.3pp | 0.67 | −41.0% | 22 | 0/31 |
| Stop 20% | 25.8% | −15.4pp | 0.71 | −40.2% | 18 | 0/31 |
| Trailing 5% | −81.2% | −122.5pp | −1.42 | −89.2% | 198 | 0/31 |
| Trailing 8% | −67.8% | −109.1pp | −0.80 | −81.3% | 92 | 0/31 |
| Trailing 12% | −41.3% | −82.6pp | −0.20 | −64.0% | 43 | 0/31 |
| Trailing 20% | 25.0% | −16.3pp | 0.49 | −50.0% | 23 | 0/31 |
| Stop 8% + target 20% | 60.2% | +19.0pp | 0.76 | −53.5% | 41 | 19/31 |

The pattern is monotonic: the tighter the stop, the worse the outcome, and
drawdown gets *worse* rather than better. The mechanism is visible in the trade
count — a stop pre-empts the strategy's own exit, banks the loss, and re-enters
into the same decline, paying fees each way. Trailing stops are catastrophic
because crypto's daily range is far wider than the trailing distance.

The stop-plus-target combination is the only configuration that beats the
baseline, but it does so by *increasing* drawdown, and 19 of 31 is barely
distinguishable from a coin flip.

**Learned:** for trend and breakout strategies, the exit signal *is* the stop.
Adding a price stop on top is not extra safety, it is a competing exit rule that
fires on noise. Risk control for this system has to happen at the portfolio
level, not the trade level.

## Phase 5 — Universe expansion ✅

20 liquid symbols with three or more years of history, `1d` and `4h`, 5000
candles — 1440 candidates, 122 validated (8.5%, against 5.7% on 5 symbols).

The run was designed to be judged on the validation *rate*, not the count. If
extra symbols were merely extra lottery tickets, the rate would have stayed
flat. It rose, which means the added symbols were genuinely more tractable —
not just more numerous.

### The result that overturned Phase 4

| Family | Rate on 5 majors | Rate on 20 symbols | Median alpha |
| --- | --- | --- | --- |
| Momentum | 11.1% | 12.5% | +8.6pp |
| Breakout | 12.2% | 9.6% | +25.1pp |
| Reversion | **0.6%** | **9.2%** | +22.1pp |
| Ensemble | 6.7% | 8.3% | +16.9pp |
| Trend | 6.1% | 6.2% | +19.4pp |

Phase 4 concluded that mean reversion does not work in crypto, on the strength
of 1 validation in 180 attempts. That conclusion was wrong — or rather, it was
right about BTC, ETH, BNB, SOL and XRP and wrong about everything else.
Reversion validated 15× more often on the wider universe, and now supplies three
of the six live allocations.

**Learned:** a negative result from a narrow universe is a statement about the
universe, not about the strategy. The five majors are the most efficiently
arbitraged, most institutionally traded assets in crypto; that they resist
reversion says little about a mid-cap.

### Why some symbols are tractable and others are not

Ranking symbols by buy-and-hold return over the validation window against their
validation rate gives a correlation of only −0.20 — but the scatter is not
random, it is **U-shaped**. Both extremes are barren, and the middle is fertile.

| Symbol | Buy-and-hold OOS | Validation rate |
| --- | --- | --- |
| DOTUSDT | −86.7% | 5.6% |
| ATOMUSDT | −83.5% | 5.6% |
| UNIUSDT | −32.0% | **36.1%** |
| DOGEUSDT | −26.9% | **33.3%** |
| ETHUSDT | +26.9% | **38.9%** |
| BNBUSDT | +187.6% | 5.6% |
| TRXUSDT | +300.8% | **0.0%** |

A one-directional collapse gives a long-only strategy nothing to catch; a
relentless rally cannot be beaten by anything that ever holds cash. Strategies
earn their keep on assets that swing.

**Learned:** symbol selection is a first-class research variable, not a
preference. Screening candidate symbols by realised choppiness before sweeping
them would raise the hit rate and cut sweep time — the strongest lever found so
far, and a new backlog item.

### Live allocation set

Selected under criteria stricter than the validation gate — ≥10 out-of-sample
trades, consistency ≥62.5%, ≥30pp alpha, positive Sharpe in both slices. 15 of
122 cleared it; 6 were chosen for family and symbol spread.

| Symbol | Timeframe | Strategy | Alpha | Sharpe | Drawdown | Trades |
| --- | --- | --- | --- | --- | --- | --- |
| XRPUSDT | 1d | Bollinger Breakout | +431pp | 1.61 | −25.9% | 21 |
| AAVEUSDT | 1d | VWAP Reversion | +157pp | 1.29 | −37.0% | 17 |
| DOGEUSDT | 1d | RSI Reversion | +133pp | 1.03 | −19.9% | 13 |
| ETHUSDT | 1d | Momentum | +122pp | 0.93 | −43.3% | 72 |
| XLMUSDT | 4h | Bollinger Breakout | +96pp | 1.51 | −21.9% | 19 |
| INJUSDT | 4h | Bollinger Reversion | +55pp | 0.92 | −22.9% | 21 |

Diversifying across strategy *families* was preferred to diversifying across
symbols, since crypto majors are heavily correlated while breakout, reversion
and momentum fail under different conditions.

---

## Phase 6 — Walk-forward validation ✅

Built `bot/walkforward.py`: fit on a rolling 365 days, trade the 90 days that
follow, step forward, repeat. Eight test windows per allocation, covering
September 2024 to August 2026. Reported as a distribution — how many windows
were profitable, how bad the worst one was — rather than a single total.

### The bug the first run exposed

The first pass returned something impossible: ETHUSDT Momentum made **zero
trades across all eight windows**, on a strategy that trades 55 times over the
same span in the live configuration.

The cause was in the harness, not the strategy. Each test window was handed to
the strategy as a standalone frame, so its indicators started from nothing. The
live Momentum parameters include `vol_filter: 100`, a rolling 100-bar volatility
quantile — undefined for the first 100 bars, on a window only 90 bars long. The
filter was never satisfied, so the strategy never traded.

Any strategy whose lookback approaches the window length was silently muted the
same way. `research.evaluate_slice()` fixes it: the signal is computed on the
whole frame and only the *trading* is restricted to the test slice, so the
fitting window doubles as indicator warm-up. There is no lookahead — the signal
at bar *t* still sees only bars up to *t* — and the slice starts flat, so no
position is inherited from the fitting period.

The same defect was present in the research sweep, where the test slice is 35%
of the frame. There it distorted rather than muted: with 5000 candles a
200-period indicator lost the first 200 bars of a 1750-bar test window, quietly
penalising every long-lookback parameter set. The fix applies to both.

**Learned:** a backtest that produces *no* trades is a louder signal than one
that produces bad ones. Bad results get analysed; empty results get explained
away. This one was nearly written off as "the filter is too strict".

### Refit versus fixed parameters

Two questions, and only one of them is about the live bot:

* **Refit** — re-optimise the parameters on every window. Answers "could this
  strategy have worked here?"
* **Fixed** — carry the deployed parameters through every window. Answers "does
  what is running right now survive these periods?"

| Symbol | Strategy | Fixed: windows profitable | Fixed: compounded | Refit: windows profitable | Refit: compounded |
| --- | --- | --- | --- | --- | --- |
| XRPUSDT 1d | Bollinger Breakout | 75.0% | +582.9% | 62.5% | +416.6% |
| AAVEUSDT 1d | VWAP Reversion | 87.5% | +177.4% | 37.5% | +12.6% |
| DOGEUSDT 1d | RSI Reversion | 62.5% | +83.5% | 37.5% | +3.8% |
| XLMUSDT 4h | Bollinger Breakout | 62.5% | +513.0% | 62.5% | +1804.1% |
| ETHUSDT 1d | Momentum | 62.5% | +49.0% | 50.0% | +12.7% |
| INJUSDT 4h | Bollinger Reversion | 50.0% | +52.0% | 37.5% | +2.6% |

Fixed parameters beat quarterly refitting on five of six allocations, and by
wide margins on three. Under refit, three allocations fail the verdict test
outright; under the parameters actually deployed, all six pass.

That is not the expected direction, and the mechanism is worth stating plainly:
365 daily bars is roughly 250 usable observations, against parameter grids of
50–500 combinations. Refitting on that is curve fitting with extra steps. The
parameters the full sweep found — chosen on a 1750-bar out-of-sample slice with
a consistency gate — carry more information than anything a single year can
supply.

**Learned:** more frequent re-optimisation is not more adaptive, it is noisier.
Refit cadence should be governed by evidence of decay, which is what the
forward-test tracking item exists to provide.

### Verdicts on the live set

All six allocations pass on their deployed parameters. Rank order, worst first:

| Symbol | Verdict | Worst window | Worst drawdown | Trades |
| --- | --- | --- | --- | --- |
| INJUSDT 4h | passes, marginal — 50% of windows profitable | −17.2% | −25.2% | 44 |
| ETHUSDT 1d | passes | −23.5% | −36.3% | 55 |
| AAVEUSDT 1d | passes | −22.7% | −37.0% | 18 |
| DOGEUSDT 1d | passes | −6.4% | −18.0% | 10 |
| XLMUSDT 4h | passes | −8.4% | −24.1% | 48 |
| XRPUSDT 1d | passes | −10.9% | −17.6% | 15 |

AAVEUSDT and ETHUSDT sit just inside the 30% portfolio drawdown target on their
own, before any correlation with the rest of the book. They are the two to watch.

Available as `python run.py walkforward SYMBOL INTERVAL STRATEGY` and on the
dashboard under **Validação**, which walks every live allocation forward on its
deployed parameters and shows the per-window table behind each verdict.

**Definition of done, item 2: met.**

## Phase 7 — Symbol screening ❌ (falsified)

Phase 5 named this the strongest available lever: validation rates ranged from
0% to 39% across symbols, an eightfold spread wider than the gap between
strategies. If a cheap price-shape measure predicted that spread, sweeps could
be pointed at the symbols worth sweeping.

Two versions were built. Both failed. The module survives as a descriptive
profiler and nothing in the pipeline consults it.

### Attempt 1 — rank by how cleanly a symbol trends

Choppiness (path length over net displacement), Hurst exponent, lag-1
autocorrelation, share of bars with ADX above 25.

Checked against run 3's twenty symbols rather than trusted, it had two problems.

**Raw choppiness is not comparable across sample sizes.** Path length grows with
the bar count, so on 720 daily candles every symbol looked choppy and the term
saturated at its floor for all but a handful — the screen had degenerated into a
near-constant. Replaced by `drift_ratio`: the displacement a random walk of the
same volatility would be expected to cover, divided by what the price actually
did. Below 1.0 the symbol trended more than chance explains. Scale-free, and
comparable across timeframes.

**The premise was still wrong.** Even after the fix the score correlated **0.03**
with validation rate, and its top half validated *less* often (7.4%) than its
bottom half (9.6%). The trend-versus-reversion call was right on 12 of 20
symbols, a coin flip.

### Attempt 2 — rank by what actually correlated

Every shape measure was regressed against three outcomes from run 3. Two carried
signal in the same direction across all of them:

| Measure | vs. validation rate | vs. median OOS alpha | vs. best OOS alpha |
| --- | --- | --- | --- |
| Realised volatility | ρ 0.53 | ρ 0.50 | ρ 0.60 |
| Lag-1 autocorrelation | ρ 0.33 | ρ 0.40 | ρ 0.62 |
| Hurst exponent | ρ 0.26 | ρ −0.15 | ρ 0.25 |
| ADX trending share | ρ 0.14 | ρ 0.08 | ρ −0.17 |
| Drift ratio | ρ 0.38 | ρ −0.36 | ρ −0.01 |
| *Attempt 1's composite* | *ρ 0.20* | *ρ 0.37* | *ρ 0.50* |

The story was persuasive. Volatility is the raw material — after 0.1% fees and
0.05% slippage on both sides a quiet symbol offers nothing to extract.
Autocorrelation, in either direction, is the structure these strategies trade.
Neither has anything to do with trending, which is why attempt 1 failed.

### The test that killed it

Both measures had been chosen *because* they correlated on run 3. At n=20 that
is a hypothesis. So: twenty symbols that appear nowhere in run 3, the screen's
ranking written to disk, and only then a full sweep — 1440 candidates on `1d`
and `4h`, 5000 candles. The prediction existed before the outcome did.

| Screen rank | Symbol | Validation rate |
| --- | --- | --- |
| 1 | FILUSDT | 5.6% |
| 2 | ICPUSDT | 0.0% |
| 3 | SEIUSDT | 1.4% |
| 4 | ALGOUSDT | **26.4%** |
| 5 | RUNEUSDT | 0.0% |
| … | | |
| 13 | THETAUSDT | 12.5% |
| 18 | CHZUSDT | 13.9% |
| 20 | MANAUSDT | 0.0% |

| Statistic | Result |
| --- | --- |
| Spearman (score, validation rate) | **−0.17** |
| Spearman (score, median OOS alpha) | **−0.33** |
| Top half validation rate | 4.44% |
| Bottom half validation rate | 4.58% |
| Family call correct | 12/20 |

Both correlations point the wrong way. The top half of the ranking validated
slightly *worse* than the bottom half. The screen has no predictive content.

### What went wrong, and why it was worth doing

Eight measures against three outcomes at n=20 is roughly two dozen chances for
something to look significant, and something did. The correlations in run 3 were
real; they were also selected, and selection at that sample size manufactures
exactly this kind of result. Attempt 2 was, in miniature, the same mistake the
whole project exists to avoid — fitting on the data used to judge the fit.

Registering the ranking before running the sweep is the only reason this is
known rather than believed. Had the screen been wired into the pipeline on
attempt 2's evidence, it would have quietly steered every future sweep on noise
and there would have been no way to notice.

**Learned:** an out-of-sample test with the prediction written down first is
cheap — one sweep — and it is the difference between a finding and a story. The
eightfold spread in validation rates across symbols is real and still
unexplained; whatever explains it is not price shape.

**Backlog item removed rather than deferred.** `bot/screening.py` keeps the
measurements and the record of the failure; `python run.py screen` prints them
with that warning attached.

## Phase 8 — Portfolio-level risk control ✅

Phase 4 closed the door on per-trade stops: not one of 31 validated candidates
improved under any pure stop, and tighter stops made both return *and* drawdown
worse. The mechanism was visible in the trade counts — a stop pre-empts the
strategy's own exit, banks the loss, and re-enters into the same decline paying
fees each way.

`bot/portfolio.py` puts the controls where they cannot compete with a strategy's
exit logic. All three are **off by default**: a control that silently blocks
trades is worse than no control if its owner did not choose it.

| Control | What it does | Why it is shaped that way |
| --- | --- | --- |
| Equity kill switch | Stops *opening* positions past a drawdown threshold; open positions keep their own exits | Closing everything at the bottom is the behaviour the stop study showed to be destructive. Hysteresis on resume (default: half the trigger) stops the switch chattering on every tick that crosses the line |
| Volatility-scaled sizing | Scales the quote amount by recent realised volatility, clamped to 0.4×–1.6× | A flat 200 USDT is a different risk in ETH than in AAVE. The clamp stops a quiet week concentrating the book and a violent day sizing a position out of existence |
| Correlation cap | Refuses an entry correlating above a threshold with an open position | Six positions in assets that move together is one position with six sets of fees |

Two defects found while testing it, both of the same kind — a number that looked
plausible and was measuring the wrong thing:

* **Volatility was not comparable across timeframes.** A 4h bar moves less than
  a daily one, so sizing on raw per-bar volatility handed every 4h allocation a
  larger position for no reason but its clock. Now scaled by √(bars per day).
* **The reference volatility was guessed at 4% and was wrong.** The measured
  median across twelve liquid pairs is 2.28% mean-absolute daily return, so
  almost every symbol pinned against the 1.6× ceiling and the control was a flat
  multiplier. Set to 2.3%, measured.

With sizing on, a 200 USDT base now spreads as: ETH 239, XLM 213, DOGE 211,
XRP 175, INJ 153, AAVE 147. The two allocations with the worst walk-forward
drawdowns — AAVE at −37% and ETH at −36% — move in opposite directions, which is
correct: AAVE's drawdown comes with high volatility, ETH's does not.

Measured correlations across the live book on daily returns: XRP/DOGE 0.90,
XRP/XLM 0.67. A cap at 0.85 would block a second entry in that pair.

Configured under **Ajustes → Risco da carteira**, or `POST /api/risk`.

---

## Phase 9 — ATR exits, and a backtester defect ✅

The backlog's top item was an ATR-scaled exit: a 5% stop is a routine day in one
symbol and a crash in another, so the level should be a multiple of recent true
range instead. `backtest.run` gained `atr_stop_mult`, `atr_trail_mult` and
`atr_period`, reading the ATR of the *previous* bar — the current bar's ATR is
computed from the very high and low the stop is about to be tested against.

The test: every validated candidate from every run, deduplicated to 194; one
fixed multiple applied to all of them, never fitted per candidate; measured only
on the out-of-sample slice, against the same strategy with no exit at all.

### The first run answered too cleanly

Not one candidate improved. Zero out of 194, on all eleven variants. A result
that clean is not a finding, it is a bug, and it was:

```python
if stop_price and low[i] <= stop_price:
    close_position(i, min(stop_price, price_open), "stop")

if qty == 0.0 and target[i] > 0.0 and i < n - 1:   # same bar, same open price
    qty = cash / (price_open * (1 + cost))
```

A stop fires while the entry signal is still long, so the very next statement
bought straight back in — at that bar's *open*, above the stop it had just sold
at, paying both spreads. Every stop exit was an immediate guaranteed loss. A
stop that fires could not help, arithmetically, and the study had measured that
tautology rather than anything about stops.

**This invalidated Phase 4.** Its conclusion — that no stop improved any of 31
candidates and that tighter stops made drawdown *worse* — was produced by the
same defect. The fix is a stand-aside flag: after a protective exit, stay flat
until the signal itself drops and turns long again. `live.py` had the identical
defect and the identical fix, persisted per symbol so it survives a restart.

### What the exits actually do

| Variant | median Δ return | mean Δ drawdown | median Δ Calmar | better/worse |
| --- | --- | --- | --- | --- |
| ATR stop 1.5× | −1.94pp | **+2.90pp** | −0.023 | 92 / 100 |
| ATR stop 2.0× | −0.60pp | +1.37pp | 0.000 | 90 / 94 |
| ATR stop 3.0× | 0.00pp | −0.02pp | 0.000 | 58 / 77 |
| 5% stop | −1.52pp | **+5.97pp** | −0.006 | 84 / 97 |
| 8% stop | 0.00pp | +3.38pp | 0.000 | 77 / 82 |
| 5% stop + 10% target | −21.74pp | **+15.55pp** | −0.267 | 46 / 147 |
| ATR trail 6.0× | −8.01pp | +1.53pp | −0.178 | 41 / 118 |
| ATR trail 2.0× | −47.01pp | +6.18pp | −0.802 | 9 / 183 |

Phase 4's headline was wrong in one direction and right in the other. **Stops do
reduce drawdown** — 3 to 6pp for a moderate stop, 15pp for a stop paired with a
target — which is what a stop is for and what the broken harness had hidden.
They still do not improve risk-adjusted return: median ΔCalmar is zero or
negative everywhere, and no variant wins on more candidates than it loses on.

Trailing stops are the clear loser. A trail tight enough to protect anything
exits a position the strategy is still right about, and the strategy has no way
back in until its own signal cycles.

ATR scaling did not beat percentages. The scale-free argument for it is sound
and it simply did not matter at this sample size.

### The direction is the opposite of the intuition

| Family | n | best variant | median Δ return | mean Δ drawdown | Calmar better/worse |
| --- | --- | --- | --- | --- | --- |
| Trend | 43 | 5% stop | +0.17pp | +6.50pp | 23 / 14 |
| Breakout | 45 | ATR stop 2.0× | +1.42pp | +0.47pp | 25 / 20 |
| Ensemble | 16 | 5% stop | +0.22pp | +4.19pp | 8 / 7 |
| Momentum | 17 | ATR stop 3.0× | 0.00pp | +0.32pp | 5 / 3 |
| Reversion | 73 | ATR stop 6.0× | 0.00pp | +0.35pp | 13 / 17 |

The expectation going in was that a stop would help mean reversion, which has no
natural stop — it exits when price returns to its mean, and in a sustained move
that may be never. The opposite is true, and emphatically: a 5% stop costs the
median reversion candidate **20.55pp** of return and wins on 24 of 73.

The reason is in the entry. Mean reversion buys *after* a decline, betting on a
bounce, so a stop a short distance below the entry sits directly in the path of
the continuation — it is a rule that sells the bottom, which is the one thing
that strategy must not do. Trend and breakout enter on strength, so their stop
only fires once the thesis is already broken.

The trend and breakout numbers lean positive but not far enough to act on: 23
wins against 14 losses is a coin flip that landed twice.

**Adopted:** ATR exits stay available in `backtest.run` and pass through the
research risk dictionary, so the question stays answerable. They are *not* added
to `RISK_GRID` and not offered per allocation. Every live allocation trades
without any protective exit, which the corrected evidence still supports — and
a third of the book is reversion strategies, where a stop is now measured to be
actively destructive.

**Scope of the correction.** 199 of the 223 stored validated results were
selected with no exit at all, so the defect could not have changed them. The
24 that carry a 5%-stop-with-10%-target were measured under the broken model and
their stored metrics are wrong. No live allocation is among them.

**Learned:** a result with no exceptions deserves more suspicion than a messy
one. "Zero out of 194" was read as a strong finding for about a minute before
the arithmetic of it — a stop can only ever sell below where it instantly
rebought — made it obvious that no data had been consulted at all.

---

## Phase 10 — Harvesting the book ✅

Runs 3 and 4 produced 187 validated candidates between them and six became
allocations. Run 4 in particular existed to test the screening hypothesis, so
nothing had ever looked at its output as a source of trades. That is the
cheapest gain available: candidates already swept, already validated on a
held-out slice, and never used.

Seventeen of them — the best-scoring unused candidate per symbol — were walked
forward on fixed parameters, the same standard the original six had to meet.
Nine were rejected before the correlation step:

| Rejected | Why |
| --- | --- |
| UNIUSDT `1d` VWAP reversion | 7 trades across 8 windows |
| AXSUSDT `1d` VWAP reversion | 4 trades, and half the windows lost |
| CRVUSDT `1d` Bollinger reversion | 17 trades, and beat the coin in half the windows |
| LTCUSDT, FILUSDT | median window 2.4–2.8%, which fees make close to nothing |
| TRXUSDT, BNBUSDT | coin flips on both window tests |
| BTCUSDT `1d` momentum | "weak": profitable, but holding BTC beat it more often than not |
| ALGOUSDT `4h` MACD trend | highest research score of the whole field (1.72) and it *fails* — loses money out of sample, median window −9.9% |

ALGOUSDT is the entry that justifies the whole walk-forward stage. A single
held-out split gave it +147.8pp of alpha and the top score in run 4. Rolled
across eight windows on the same parameters, it lost money in five of them. One
split flatters; that is what Phase 6 was built to catch, and here it caught the
best-looking candidate in the pool.

### Correlation, not just quality

The eight survivors were then taken in order of steadiness and checked against
the daily-return correlation of everything already accepted, capped at 0.80.

| Candidate | Windows won | Beat coin | Median window | Worst window | Max correlation | |
| --- | --- | --- | --- | --- | --- | --- |
| GRTUSDT `4h` RSI reversion | 87.5% | 87.5% | +11.91% | 0.0% | 0.82 DOGE | skipped |
| DOTUSDT `4h` Bollinger breakout | 75.0% | 87.5% | +8.85% | −25.1% | 0.83 DOGE | skipped |
| IMXUSDT `4h` VWAP reversion | 75.0% | 75.0% | +8.29% | −6.8% | 0.73 DOGE | added |
| ATOMUSDT `4h` Momentum | 75.0% | 75.0% | +5.42% | −36.5% | 0.64 DOGE | added |
| ARBUSDT `4h` Bollinger breakout | 62.5% | 75.0% | +11.88% | −11.3% | 0.72 ETH | added |
| CHZUSDT `4h` Ensemble Vote | 62.5% | 75.0% | +9.37% | −21.1% | 0.66 IMX | added |
| NEARUSDT `4h` Bollinger reversion | 75.0% | 62.5% | +5.33% | −3.7% | 0.66 IMX | added |
| ETCUSDT `4h` VWAP reversion | 62.5% | 62.5% | +9.24% | −6.6% | 0.83 DOGE | skipped |

Two of the three rejections were the best entries in the table on their own
merits. GRTUSDT won seven of eight windows and beat the coin in seven of eight,
and it correlates 0.82 with DOGEUSDT, which is already in the book — buying it
is mostly buying more DOGE with extra paperwork. Ranking by quality alone would
have picked exactly the candidates that add the least.

The book went from six allocations to eleven, `max_positions` from 6 to 11.
Fully deployed that is 5500 USDT of a 10 000 notional. Strategy mix afterwards:
three Bollinger breakout, two VWAP reversion, two Bollinger reversion, two
momentum, one RSI reversion, one ensemble.

**Learned:** the pipeline had been treating a research run as an experiment and
then discarding its output. Every sweep that validates anything is also a
shortlist, and the walk-forward is cheap enough — about a second per candidate —
that there is no reason not to run it over the whole backlog.

---

## Phase 11 — Making the trade log answer "why" ✅

The dashboard could say *what* the bot did and not *why*. The request was
explicit: per coin, the entry and exit signal, the strategy, the size of the
gain or loss, and the time it took. The trade log had the first two only as a
strategy name.

Every position now stores a frozen snapshot of the decision — the rule in plain
language, the indicator values that satisfied it, the candle it was read from,
and the market price at the time — written at order time so it cannot drift.
The log groups by coin, shows duration and result per trade, and expands into
the entry and exit cards. A second tab replays the same allocations over recent
history, because a daily strategy produces a handful of live trades a year and
that is not enough to see what it does.

### The bug the explanation surfaced

Writing the entry card against the live book produced a card that contradicted
itself: rule "price closes above the upper Bollinger band", price 1.3964, upper
band 1.6954. The rule had not fired on that candle.

Strategies hold a position between their entry and exit pulses. The live bot
enters whenever the signal *is* long, not when it *turns* long, so an allocation
added to the book mid-signal buys a move that started days earlier. XRPUSDT
broke out on 2026-08-19 at 1.106 and the bot bought it on 2026-08-30 at 1.3911 —
ten daily candles and 26% of the move later. The backtest that justified the
allocation never makes that trade; it buys the candle after the transition.
So the live book had been taking a position nothing had ever measured.

### What late entry costs

Each signal run in 900 candles of history, for all eleven allocations, entered
`k` candles after its start and held to the same exit. Only runs long enough to
be joined at every offset are counted — short runs drop out first and short runs
are disproportionately losers, so an unmatched sample would flatter late entry
by survivorship alone.

| Candles late | Mean trade | Median trade | Winners |
| --- | --- | --- | --- |
| 0 | +7.51% | +2.76% | 62.2% |
| 1 | +6.67% | +2.02% | 60.2% |
| 2 | +5.65% | +1.30% | 57.1% |
| 3 | +5.66% | +1.89% | 57.1% |
| 4 | +4.80% | +0.59% | 54.1% |
| 5 | +4.92% | +1.59% | 56.1% |

Roughly half a point of expected return per candle of delay, and six points of
win rate across five candles. Nine of the eleven allocations are worse at five
candles late than at zero. XRPUSDT was joined at ten.

The fix reuses the stand-aside flag from Phase 9: on the first tick for an
allocation, if the signal is already long, the bot sits out until the signal
drops and turns long again. The cost is the tail of whatever run is in progress
when an allocation is added, which the table prices at a few points; the gain is
that every live trade is one the backtest also makes.

**Learned:** an explanation is a test. The numbers had been correct and the
behaviour had been wrong for as long as the book existed, and nothing caught it
until the interface had to print the reason next to the evidence. This is the
second defect in two phases found by making the system state its case rather
than by reading its code.

---

## Phase 12 — Showing the money, and defining "ready" ✅

Three questions had no answer anywhere in the interface: what did the bot buy
and sell, how much has it made or lost, and how would anyone know when it is
safe to point it at a real account. The first two are reporting gaps. The third
is the actual goal of the project, and it had never been written down as
something that could be checked.

### The order ledger

Positions were visible; orders were not. The `orders` table had been written to
since the first version and read by nothing. That is a strange gap, because
"what did it buy and sell" is the most literal possible question about a trading
bot, and the position view cannot answer it: a position is a summary of two
orders, and the summary is what you look at *after* you already trust the
orders.

`/api/orders` returns the raw ledger, newest first, joined to the position each
order belongs to. Every row states the side, the coin, the strategy, the size,
the fill price, the cash movement, the realised result where there is one, and
the reason the bot acted. It is deliberately not grouped by coin — grouping
answers "how is this coin doing", which the panel below it already answers.
This one answers "what did the robot do, in order", which is what a bank
statement answers, and a statement that reorders itself is not a statement.

Linking a sale back to its position needed a column: `orders.position_id`. The
buy is written before the position row exists, so the link is set immediately
after the insert; the sale writes it directly. Without it a sale in the order
book cannot say what it made.

### Profit and loss, spelled out

A single "total result" number hides the two things that make it, and they are
not the same kind of money: one is banked and one can still evaporate. The
dashboard now reads top to bottom as arithmetic — starting capital, what closed
trades did to it, what open trades are currently doing to it, and what is left —
with the estimated fees paid so far stated underneath, since fees are already
inside every other number on the page and it is worth knowing how much the
exchange took. The strategy breakdown gained a per-coin view next to the
per-strategy one.

### A go-live checklist that can fail

The point of the whole exercise is a book good enough to trade real money. That
needs a definition, and the definition has to be able to say no.

`/api/readiness` compares what the walk-forward expects against what the live
run has actually produced, and gates the answer on five conditions:

| Gate | Threshold | Why that number |
| --- | --- | --- |
| Every allocation still passes walk-forward | 11 of 11 | A book is only as validated as its worst member |
| Closed trades observed live | 30 | Below about 30, a win rate carries a confidence interval near ±18 points, which cannot separate a good book from a bad one |
| Days running | 90 | One full walk-forward test window; less is not the same unit as the thing being compared against |
| Realised result not worse than the expected worst quarter | −8.52% of capital | A book can be profitable and still be broken; what matters is whether it behaves like the thing that was measured |
| Observed drawdown within the configured limit | 20% | Roughly 2.3× the expected worst quarter, so it fires when something is broken rather than during a normal bad run |

The expectation is scaled to the size the bot actually trades: each allocation's
median quarter is divided by three and multiplied by its share of capital
(500 of 10,000), which gives the book a combined expectation of **+1.69% per
month across about 21 trades**, with a worst measured quarter of **−8.52%**.
Against that, the live testnet run currently shows 1 closed trade over 4 days.
The honest reading is that there is nothing to compare yet, and the checklist
says so rather than producing a score that averages the gap away.

The drawdown kill switch was turned on as part of this (20% halt, 10% resume).
Volatility sizing and the correlation cap were deliberately left off: both
change which trades get taken, so enabling them would make the live run stop
being a test of the thing that was measured. The kill switch only acts in a tail
the expectation never contains, so it costs nothing in comparability.

## Phase 13 — Two weeks instead of ninety days ✅

The Phase 12 checklist asked for 30 closed trades and 90 days of running before
real money. Both are defensible and both were unreachable: the owner cannot
leave a machine on for three months and wanted a usable answer inside two weeks.
The obvious move is to trade a faster timeframe. The stored research says no.

| Timeframe | Trades per month per allocation | Median trade, after costs | Validated |
| --- | --- | --- | --- |
| 1d | 0.48 | +7.04% | 138 of 1620 |
| 4h | 2.50 | +0.30% | 68 of 1764 |
| 1h | 8.80 | **+0.01%** | **0 of 324** |

At 1h the round trip costs 0.30% and the median strategy nets one basis point.
The frequency is real and the edge is gone; not one of 324 candidates validated.
Buying sample size at that price is buying noise.

### The reframe

The 30-trade threshold exists to establish that an edge is real. That evidence
already exists, and it is much stronger than anything two weeks of live trading
could produce: walk-forward puts each allocation through eight quarters and
hundreds of trades. What a live run uniquely proves is something else — that the
engine fills where the model assumed, on the candle the model assumed, at the
cost the model assumed. That is an execution question, and execution defects are
systematic, not statistical: they appear in the first trade and every trade
after it.

So the comparison is pairwise. Each live trade is matched against the trade the
backtest would have made on the same candles: same decision bar, entry price
against the following open, realised return against the modelled return. Ten to
fifteen matched trades is enough. That fits in two weeks.

### The coverage problem, measured

Equity snapshots are written once per tick, which makes them an honest record of
when the process was alive. Read against the candle grid, they say the bot has
been present for **4 of 26 candle closes since it started — 15.4%**, with wall
clock uptime of 9.7%.

This matters more than a missed trade. A candle the bot slept through is
invisible afterwards: a strategy that never fired and a strategy that fired
while nobody was listening produce the same empty result. Coverage separates
them, per timeframe, because a missed daily close costs six times a missed 4h
close and one average would hide which is being lost.

### What coverage found

The first parity run flagged both live trades, and the second one was the
interesting one:

| Trade | Signal turned | Bot bought | Late by | Price paid vs model |
| --- | --- | --- | --- | --- |
| XRPUSDT 1d | 2026-08-19 | 2026-08-30 | 11 candles | +25.8% |
| ARBUSDT 4h | 2026-08-31 16:00 | 2026-09-03 01:06 | 13 candles | +27.8% |

XRP was the known pre-fix defect. ARB was bought *after* the transition-only fix
and was still 13 candles late, because downtime defeats that fix: the stand-aside
flag was recorded as flat before the machine went down, the signal turned long
while it was off, and the first tick after waking saw a clean flat-to-long
transition that was two days stale. The measured cost on XRP was −24.99
percentage points against its own model.

So downtime does not merely lose trades. It converts good entries into bad ones,
which is worse than missing them, and it does so silently. The engine now
refuses an entry more than one candle after the signal turned, records the miss
as an event, and waits for the next clean transition. One candle is the
tolerance because the model itself fills one bar after the decision.

### Gates, revised

`trades ≥ 30` and `days ≥ 90` were replaced by `matched parity trades ≥ 10` and
`candle coverage ≥ 90%`; the statistical thresholds are still reported next to
the live results, as context rather than as a blocker. The checklist now fails
for the right reason: not "we have not waited long enough" but "the machine was
not there, and the trades it did take do not match the model".

## Phase 14 — Judging the engine that exists ✅

Phase 13 shipped the stale-entry guard and, in the same breath, made the only
two live trades unjudgeable. Both were entered by the engine as it was before
the guard: XRPUSDT eleven candles late, ARBUSDT thirteen. Scoring them against
the model measures a bug that has already been fixed, and — worse — would let
the next fix show up as an improvement in a number that no live trade caused.

So parity now carries a baseline. `GUARD_LANDED` in `parity.py` is the moment
the guard shipped; entries older than it are listed with the verdict `antes da
guarda`, in grey, and left out of every total. The kv key `parity_baseline`
overrides it the next time the engine changes in a way that invalidates the
sample. The money those trades lost is untouched: it is in equity, in realised
P&L, in the drawdown gate. Only the verdict is withheld.

The open ARBUSDT position was closed manually at +0.81%, for the same reason —
it was not a trade the book asked for, it was an artefact of downtime, and
leaving it open meant its exit would also be scored against a model it never
followed.

INJUSDT left the book. At 50% profitable quarters and a +3.70% median it was
the weakest allocation by both measures, and there is no shortage of
replacements.

### The fast-timeframe question, measured

Phase 13 ruled out 1h on 324 stored candidates. That set covered five symbols,
which is thin, and 15m had never been swept at all. Both gaps are now closed:
40 symbols, 12 strategies, 15m and 1h, 3000 candles each.

The first thing the data says is arithmetic. A round trip costs 0.30%, and a
candle has to move further than that before a trade can pay for itself:

| Timeframe | Median candle move | Candles bigger than the round trip | History in 3000 candles |
| --- | --- | --- | --- |
| 15m | 0.122% | 19.8% | 31 days |
| 1h | 0.253% | 43.8% | 124 days |
| 4h | 0.624% | 72.1% | 499 days |
| 1d | 1.891% | 89.9% | 2574 days |

The second thing it says is that a sweep on 15m validates plenty — 59 of 1421
combinations — and that this means almost nothing, because 3000 candles of 15m
is 31 days and the out-of-sample slice inside it is nine. Walking the top six
candidates forward on 180 days of history, with windows scaled to the timeframe
(60 days train, 15 days test), one survived. At 1h, with 120/30 windows, two of
six survived.

| Timeframe | Candidates walked forward | Held up |
| --- | --- | --- |
| 15m | 6 | 1 (NEARUSDT, 5/8 windows, +2.46% median, worst −14.71%) |
| 1h | 6 | 2 (GRTUSDT +3.12%, TIAUSDT +1.88%) |

An edge exists down there. It is thinner, it survives less often, and it comes
with a decisive operational cost: 15m closes 96 candles a day against 6 at 4h,
and the stale-entry guard now converts every missed close into a skipped trade.
Measured coverage is 15.4%. A 15m book on this machine would skip almost
everything it signalled — the timeframe is wrong for this deployment, not merely
marginal for the market.

### More coins on the timeframes that already work

The same walk-forward that chose the book was run against the 20 highest-scoring
validated candidates outside it, on 1d and 4h. Seventeen held up, several of them
comfortably better than the allocation that was just dropped:

| Symbol | Interval | Strategy | Profitable quarters | Median | Worst | Trades |
| --- | --- | --- | --- | --- | --- | --- |
| ALGOUSDT | 4h | rsi_reversion | 88% | +17.91% | −4.10% | 53 |
| UNIUSDT | 1d | vwap_reversion | 62% | +15.75% | −5.99% | 7 |
| CRVUSDT | 1d | bollinger_reversion | 88% | +15.59% | −22.76% | 18 |
| GRTUSDT | 4h | rsi_reversion | 88% | +11.03% | 0.00% | 21 |
| ETCUSDT | 4h | vwap_reversion | 62% | +10.87% | −6.60% | 33 |
| DOTUSDT | 4h | bollinger_breakout | 75% | +8.85% | −25.12% | 44 |
| SEIUSDT | 1d | vwap_reversion | 75% | +7.33% | −21.68% | 14 |

This is the answer to "would more coins help": yes, and for a reason that has
nothing to do with diversification. The two-week deadline needs ten matched
parity trades, and the current ten allocations produce about 19 trades a month
between them. Each addition is independently validated, so it adds trades
without adding noise — which is exactly what dropping to 15m would not do.

---

## Next

Ordered by expected value, highest first.

### 1. Regime labelling

Every misleading result so far has been a regime artifact. Label each window
(bull, bear, chop) from the buy-and-hold return and volatility, then report
strategy performance *per regime*. This turns "this strategy works" into "this
strategy works in trending markets and loses in chop", which is both more true
and more actionable — it makes a regime-switched allocation possible.

### 2. Forward-test tracking, continued

Phase 12 compares realised totals against the walk-forward expectation. The
missing half is the shape: record the expected equity curve at allocation time
and plot realised against expected, so divergence is visible while it is
developing rather than after the totals have moved. Divergence is the earliest
available signal that an edge has decayed.

### 3. Short and market-neutral

Everything so far is spot-long-only, which means every strategy is structurally
long crypto beta. That is why beating buy-and-hold is so hard: the benchmark is
the same trade. Futures testnet would allow shorts and a genuinely
market-neutral comparison.

---

## Decision log

| Decision | Reason |
| --- | --- |
| Research on production data, trade on testnet | Testnet history is too shallow to research against |
| Rank on out-of-sample only | In-sample ranking selects for overfitting by construction |
| Require beating buy-and-hold | In a rising market, a strategy that trails it is worse than doing nothing |
| Signal at close, fill at next open | Any other convention lets the signal see its own fill price |
| One allocation per symbol | Two strategies on one asset fight over the same spot balance |
| Act on closed candles only | The forming candle changes, so signals derived from it are not reproducible |
| No stops on current allocations | Re-measured on 194 candidates after fixing the re-entry defect: stops cut drawdown but not one variant improves risk-adjusted return, and on reversion strategies a 5% stop costs 20pp |
| No re-entry after a protective exit until the signal cycles | Otherwise a stop sells at its level and rebuys above it on the same bar, which is a guaranteed loss and made the Phase 4 stop study meaningless |
| Signal computed on the full frame, trading restricted to the slice | A bare test slice leaves long-lookback indicators undefined; a 100-bar filter on a 90-bar window mutes the strategy entirely |
| Do not refit on a rolling window | Measured: fixed parameters beat quarterly refitting on 5 of 6 allocations; 250 daily bars cannot support a 500-point grid |
| No symbol screen in the pipeline | Two versions were built and both failed a pre-registered out-of-sample test; the module is descriptive only |
| Portfolio controls ship off by default | Each one blocks trades silently; that has to be the owner's choice, not a default |
| Kill switch blocks entries, never forces exits | Closing at the bottom is the behaviour the Phase 4 stop study measured as destructive |
| Enter only on a signal transition, never mid-signal | Joining a run already in progress costs about half a point of expected return per candle and is a trade no backtest ever measured |
| Every trade stores why it was made, at the moment it was made | Reconstructing a reason later gives the reason the code would give today, which is exactly the reason that cannot catch a bug in the code |
| Gate on execution parity, not on live sample size | The edge already has hundreds of walk-forward trades behind it; what only a live run can show is whether the engine executes the model, and that shows up in a handful of paired trades |
| Refuse an entry more than one candle after the signal turned | Downtime turns a clean transition into a stale one, and the bot cannot tell the difference on waking; ARBUSDT was bought 13 candles late at a 27.8% worse price with the transition fix already in place |
| Do not trade 1h to manufacture sample size | 0 of 324 candidates validated and the median trade nets +0.01% after costs — the frequency is real and the edge is not |
| Rank additions by steadiness, then cap correlation | The best candidate by return was 0.82 correlated with an existing allocation; buying it would have been buying the same exposure twice |
| Every research run gets harvested, not just read | Run 4 sat unused for a phase because it was built to answer a question, and it contained four allocations that hold up |
| Dropped BTC from live allocations | Its validated candidates beat buy-and-hold by ~6pp over 3 years — indistinguishable from noise |
| Readiness is a checklist, not a score | A score averages away the one missing condition, and the one missing condition is exactly what has to be known before risking real money |
| Kill switch on, volatility sizing and correlation cap off | The kill switch only acts in a tail the measured expectation never contains; the other two change which trades get taken, which would stop the live run from being a test of what was measured |
| Do not score live trades taken by an engine that no longer exists | A parity sample judges the engine as it stands; keeping the old trades in would let a later fix look like an improvement no live trade caused |
| Do not trade 15m on this deployment | The edge is thinner and rarer, and 96 candle closes a day against measured 15.4% coverage means the guard would skip almost every signal |
| Add coins before adding speed | Both raise trade count, but an independently validated allocation adds trades without adding noise |
