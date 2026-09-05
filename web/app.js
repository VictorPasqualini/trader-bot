/* Pouch dashboard ---------------------------------------------------- */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const state = {
  view: 'dashboard',
  status: null,
  overview: null,
  equity: [],
  leaderboard: [],
  selected: new Set(),
  researchTimer: null,
  detailCurve: null,
  tradesMode: 'live',
  breakdown: null,
  breakdownGroup: 'by_strategy',
  book: 'live',
  lab: null,
  exit: null,
};

/* The two books, side by side and measured identically. The live one is a
   forward test of strategies that already survived a walk-forward; the other is
   an experiment that is allowed to be wrong. Keeping them on one screen with
   one set of metrics is the whole point - a comparison where each side reports
   its own favourite number is not a comparison. */
const BOOKS = {
  live: {
    label: 'Livro validado',
    hint: 'Estratégias que passaram na caminhada para a frente, operando adiante '
        + 'sem reajuste. $100 por posição, teto de 11 posições. '
        + 'Abaixo, o estudo de saída roda sobre estas mesmas operações.',
    overview: '/overview',
    equity: '/equity',
    events: 'bot',
  },
  ml: {
    label: 'Laboratório ML',
    hint: 'Modelo de ranking treinado do zero, com liberdade para errar. Escolhe '
        + 'as 3 melhores moedas do dia entre as 18 e rebalanceia por semana. '
        + '$100 por posição.',
    overview: '/lab/overview',
    equity: '/lab/equity',
    events: 'lab',
  },
};

const VIEW_META = {
  dashboard: ['Painel', 'Resultado consolidado das estratégias em operação'],
  lab: ['Pesquisa', 'Otimiza no histórico antigo e valida no que ficou de fora'],
  trades: ['Operações', 'Cada entrada e saída, moeda a moeda, com o sinal que a disparou'],
  validation: ['Validação', 'A mesma configuração testada trimestre a trimestre, sem reajuste'],
  settings: ['Ajustes', 'Modo de execução, risco por operação e estratégias ativas'],
};

/* ------------------------------------------------------------------- utils */

async function api(path, options = {}) {
  const response = await fetch(`/api${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  const text = await response.text();
  const data = text ? JSON.parse(text) : null;
  if (!response.ok) throw new Error(data?.detail || response.statusText);
  return data;
}

const nf = (value, digits = 2) =>
  (value ?? 0).toLocaleString('pt-BR', { minimumFractionDigits: digits, maximumFractionDigits: digits });

const money = (value, digits = 2) => `$${nf(value, digits)}`;
const signed = (value, digits = 2) => `${value >= 0 ? '+' : ''}${nf(value, digits)}`;
const pct = (value, digits = 2) => `${signed(value, digits)}%`;
const cls = (value) => (value > 0 ? 'pos' : value < 0 ? 'neg' : '');
const plural = (count, one, many) => `${count} ${count === 1 ? one : many}`;

function dt(iso, withTime = true) {
  if (!iso) return '—';
  const date = new Date(iso);
  const day = date.toLocaleDateString('pt-BR', { day: '2-digit', month: '2-digit' });
  if (!withTime) return day;
  return `${day} ${date.toLocaleTimeString('pt-BR', { hour: '2-digit', minute: '2-digit' })}`;
}

function toast(message, kind = '') {
  const el = $('#toast');
  el.textContent = message;
  el.className = `toast ${kind}`;
  el.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { el.hidden = true; }, 3800);
}

function setText(id, value, className) {
  const el = $(id);
  if (!el) return;
  el.textContent = value;
  if (className !== undefined) el.className = el.className.replace(/\b(pos|neg)\b/g, '').trim() + ' ' + className;
}

/* ------------------------------------------------------------------ charts */

function drawChart(canvas, series, { fill = true, tipTarget = null, format = money } = {}) {
  const dpr = window.devicePixelRatio || 1;
  const width = canvas.clientWidth || canvas.parentElement.clientWidth;
  const height = Number(canvas.getAttribute('height'));
  canvas.width = width * dpr;
  canvas.height = height * dpr;
  canvas.style.height = `${height}px`;

  const ctx = canvas.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, width, height);

  const points = series.flatMap((s) => s.points);
  if (points.length < 2) return null;
  // A band's far edge is drawn but is not a point, so it has to be folded into
  // the extent by hand or the shaded area gets clipped at the axis.
  const edges = series.flatMap((s) => (s.bandTo || []).map((y) => ({ y })));

  const pad = { top: 14, right: 56, bottom: 22, left: 10 };
  const plotW = width - pad.left - pad.right;
  const plotH = height - pad.top - pad.bottom;

  let min = Math.min(...points.concat(edges).map((p) => p.y));
  let max = Math.max(...points.concat(edges).map((p) => p.y));
  const span = max - min || Math.abs(max) * 0.02 || 1;
  min -= span * 0.12;
  max += span * 0.12;

  const n = Math.max(...series.map((s) => s.points.length));
  const xAt = (i, len) => pad.left + (len <= 1 ? plotW : (i / (len - 1)) * plotW);
  const yAt = (v) => pad.top + plotH - ((v - min) / (max - min)) * plotH;

  // grid + right-hand axis labels
  ctx.font = '11px system-ui, sans-serif';
  ctx.textBaseline = 'middle';
  for (let i = 0; i <= 4; i += 1) {
    const y = pad.top + (plotH / 4) * i;
    ctx.strokeStyle = 'rgba(255,255,255,0.045)';
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(pad.left, y + 0.5);
    ctx.lineTo(pad.left + plotW, y + 0.5);
    ctx.stroke();
    ctx.fillStyle = '#5d6a80';
    ctx.textAlign = 'left';
    const value = max - ((max - min) / 4) * i;
    ctx.fillText(nf(value, Math.abs(value) > 1000 ? 0 : 2), pad.left + plotW + 8, y);
  }

  // Bands go down first: they are context, and the lines that carry the answer
  // have to sit on top of them.
  series.filter((s) => s.bandTo).forEach((s) => {
    const len = s.points.length;
    if (len < 2) return;
    ctx.beginPath();
    s.points.forEach((p, i) => {
      const x = xAt(i, len);
      const y = yAt(p.y);
      if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    });
    for (let i = len - 1; i >= 0; i -= 1) ctx.lineTo(xAt(i, len), yAt(s.bandTo[i]));
    ctx.closePath();
    ctx.fillStyle = s.bandColor || 'rgba(91,124,250,0.13)';
    ctx.fill();
  });

  series.forEach((s) => {
    const len = s.points.length;
    if (len < 2 || !s.color) return;
    ctx.lineWidth = s.width || 2;
    ctx.strokeStyle = s.color;
    if (s.dash) ctx.setLineDash(s.dash); else ctx.setLineDash([]);
    ctx.beginPath();
    s.points.forEach((p, i) => {
      const x = xAt(i, len);
      const y = yAt(p.y);
      if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    });
    ctx.stroke();

    if (fill && s.fill !== false) {
      const gradient = ctx.createLinearGradient(0, pad.top, 0, pad.top + plotH);
      gradient.addColorStop(0, s.fillColor || 'rgba(91,124,250,0.28)');
      gradient.addColorStop(1, 'rgba(91,124,250,0)');
      ctx.lineTo(xAt(len - 1, len), pad.top + plotH);
      ctx.lineTo(xAt(0, len), pad.top + plotH);
      ctx.closePath();
      ctx.fillStyle = gradient;
      ctx.fill();
    }
    ctx.setLineDash([]);
  });

  // x-axis: first and last timestamp
  const first = series[0].points[0];
  const last = series[0].points[series[0].points.length - 1];
  ctx.fillStyle = '#5d6a80';
  ctx.textAlign = 'left';
  ctx.fillText(dt(first.t, false), pad.left, height - 9);
  ctx.textAlign = 'right';
  ctx.fillText(dt(last.t, false), pad.left + plotW, height - 9);

  if (tipTarget) attachTip(canvas, tipTarget, series[0], { xAt, yAt, pad, plotH }, format);
  return { xAt, yAt };
}

function attachTip(canvas, tip, series, geo, format = money) {
  canvas.onmousemove = (event) => {
    const rect = canvas.getBoundingClientRect();
    const x = event.clientX - rect.left;
    const len = series.points.length;
    const ratio = (x - geo.pad.left) / (rect.width - geo.pad.left - 56);
    const index = Math.max(0, Math.min(len - 1, Math.round(ratio * (len - 1))));
    const point = series.points[index];
    tip.hidden = false;
    tip.style.left = `${geo.xAt(index, len)}px`;
    tip.style.top = `${geo.yAt(point.y)}px`;
    tip.innerHTML = `<b>${format(point.y)}</b><span>${dt(point.t)}</span>`;
  };
  canvas.onmouseleave = () => { tip.hidden = true; };
}

/* --------------------------------------------------------------- dashboard */

async function loadStatus() {
  const status = await api('/status');
  state.status = status;

  const { exchange, bot } = status;
  $('#dot-market').className = `dot ${exchange.market_data ? 'on' : 'off'}`;
  $('#dot-account').className = `dot ${exchange.account ? 'on' : 'off'}`;
  $('#dot-bot').className = `dot ${bot.running ? 'on' : 'idle'}`;
  $('#brand-mode').textContent = bot.mode === 'paper' ? 'papel' : (exchange.testnet ? 'testnet' : 'REAL');
  $('#free-balance').textContent = exchange.account ? money(exchange.quote_balance) : '—';

  const toggle = $('#btn-toggle-bot');
  toggle.textContent = bot.running ? 'Parar robô' : 'Ligar robô';
  toggle.className = `btn ${bot.running ? 'btn-danger' : 'btn-primary'}`;

  if (!exchange.account && exchange.account_error) {
    $('#free-balance').textContent = 'sem chave';
  }
  if (status.research?.status === 'running') watchResearch();
}

async function loadDashboard() {
  const book = BOOKS[state.book];
  $$('[data-book-only]').forEach((panel) => {
    panel.hidden = !panel.dataset.bookOnly.split(' ').includes(state.book);
  });
  $('#book-hint').textContent = book.hint;
  $$('#book-toggle .seg-btn').forEach((button) =>
    button.classList.toggle('is-on', button.dataset.book === state.book));

  /* Filtered by book. Three books writing into one feed makes the feed
     useless: what a reader wants from it is what the book in front of them
     just did, and interleaving three makes that impossible to see. */
  const [overview, equity, events] = await Promise.all([
    api(book.overview), api(book.equity), api(`/events?limit=30&source=${book.events}`),
  ]);
  state.overview = overview;
  state.equity = equity;

  $('#book-hint').textContent =
    `${book.hint} Capital de ${money(overview.start_capital, 0)}.`;

  setText('#kpi-equity', money(overview.total_value));
  setText('#kpi-equity-delta',
    `${pct(overview.total_return_pct)} sobre ${money(overview.start_capital, 0)}`,
    cls(overview.total_return_pct));
  setText('#kpi-pnl', `${overview.total_pnl >= 0 ? '+' : '−'}${money(Math.abs(overview.total_pnl))}`,
    cls(overview.total_pnl));
  setText('#kpi-pnl-split',
    `realizado ${money(overview.realised_pnl)} · aberto ${money(overview.unrealised_pnl)}`);
  setText('#kpi-winrate', `${nf(overview.win_rate_pct, 1)}%`);
  setText('#kpi-winrate-sub', `${overview.wins}G / ${overview.losses}P em ${overview.closed_trades}`);
  setText('#kpi-pf', overview.profit_factor >= 999 ? '∞' : nf(overview.profit_factor, 2));
  setText('#kpi-dd', `${nf(overview.max_drawdown_pct, 2)}%`);
  renderLastKpi(overview);

  renderPnl(overview);
  renderEquity(equity, overview);
  renderPositions(overview.positions);
  renderEvents(events);

  if (state.book === 'live') {
    const breakdown = await api('/breakdown');
    state.breakdown = breakdown;
    renderBreakdown(breakdown[state.breakdownGroup || 'by_strategy']);
    await loadSignals();
    /* The study lives in this tab because it is this book: the same trades,
       exited four ways. Putting it in a tab of its own asked the reader to
       hold the validated book's numbers in their head while looking at it. */
    await loadExitBook(overview);
  } else {
    await loadLabBook(overview);
  }
}

/* ------------------------------------------------------------- exit study */

/* One colour per arm, fixed here so the tile, the chart line and the legend all
   agree. The control is white on purpose: every other line is read against it. */
const EXIT_COLORS = {
  rule: '#e8edf7', t2: '#19d69b', t5: '#f2c14e', t10: '#5b7cfa',
};

async function loadExitBook(liveOverview) {
  const [overview, open, closed, events, curves] = await Promise.all([
    api('/mirror/overview'),
    api('/mirror/positions'),
    api('/mirror/trades?limit=200'),
    api('/events?limit=30&source=mirror'),
    api('/mirror/equity?limit=500'),
  ]);
  state.exit = overview;

  $('#btn-exit-jump').onclick = () =>
    $('#exit-study').scrollIntoView({ behavior: 'smooth', block: 'start' });

  $('#btn-exit-toggle').textContent = overview.running ? 'Parar' : 'Ligar';
  $('#btn-exit-toggle').classList.toggle('btn-danger', !!overview.running);
  $('#btn-exit-toggle').classList.toggle('btn-primary', !overview.running);

  /* Said in words, at the top, before any number. Four arms and a handful of
     trades cannot separate exits that differ by a couple of points a trade, and
     a panel that shows a ranking without saying so invites reading a winner out
     of noise. */
  setText('#exit-note',
    `${overview.note} ${overview.closed_trades} de ~${overview.trades_needed} `
    + `operações fechadas. Começou em ${dt(overview.started_at)}`
    + `${overview.last_tick ? ` · último ciclo ${dt(overview.last_tick)}` : ''}.`,
    overview.conclusive ? '' : 'muted');

  renderExitVerdict(overview);
  renderExitKpis(overview);
  renderExitArms(overview, liveOverview);
  renderExitEquity(curves, overview);
  renderExitPaired(overview);
  renderExitLedger(open, closed);
  renderEvents(events, '#exit-events-list');
}

/* The answer, in words, above the fold. The paired difference is the whole
   result of the study - each pair is the same trade with two exits, so the mean
   difference is the effect and nothing else - and it was previously readable
   only from a table near the bottom of the page.

   The sample size travels with it. A verdict card with no count is an invitation
   to read a winner out of four arms and a handful of trades. */
function renderExitVerdict(overview) {
  const rows = Object.entries(overview.paired);
  $('#exit-verdict').innerHTML = rows.map(([arm, data]) => {
    const target = `alvo +${arm.slice(1)}%`;
    if (!data.trades) {
      return `<div class="vcard" style="--arm:${EXIT_COLORS[arm] || '#8b94b2'}">
          <span class="vcard-label">regra vs ${target}</span>
          <strong class="vcard-value muted">sem pares</strong>
          <span class="vcard-sub">nenhuma operação fechou nos dois</span>
        </div>`;
    }
    const pp = data.rule_minus_target_pp;
    const ahead = pp > 0 ? 'regra à frente' : pp < 0 ? `${target} à frente` : 'empate';
    return `<div class="vcard" style="--arm:${EXIT_COLORS[arm] || '#8b94b2'}">
        <span class="vcard-label">regra vs ${target}</span>
        <strong class="vcard-value ${cls(pp)}">${signed(pp, 2)} pp</strong>
        <span class="vcard-sub">${ahead} · ${data.trades} pares ·
          ${data.rule_ahead}–${data.target_ahead}${data.identical ? `–${data.identical} iguais` : ''}</span>
      </div>`;
  }).join('');
  setText('#exit-verdict-note',
    `Média por operação, mesma entrada dos dois lados. `
    + `${overview.note} ${overview.closed_trades} de ~${overview.trades_needed} fechadas.`,
    overview.conclusive ? '' : 'muted');
}

/* The study has its own money and shows it, one tile per arm. Folding four arms
   into a single headline figure would hide the only thing being measured, and
   leaving them out entirely made the tab look like it had one book in it. */
function renderExitKpis(overview) {
  $('#exit-kpis').innerHTML = overview.arms.map((arm) => `
    <div class="kpi kpi-arm" style="--arm:${EXIT_COLORS[arm.arm] || '#8b94b2'}">
      <span class="kpi-label">${arm.label}</span>
      <strong class="kpi-value">${money(arm.total_value)}</strong>
      <span class="kpi-delta ${cls(arm.total_pnl)}">${signed(arm.total_pnl)} · ${pct(arm.return_pct)}</span>
      <span class="kpi-sub">${arm.closed_trades} fechada${arm.closed_trades === 1 ? '' : 's'}
        · ${arm.open_positions} aberta${arm.open_positions === 1 ? '' : 's'}
        · ${money(arm.invested)} aplicado${arm.arm === 'rule' ? '' : ` · ${arm.hit_target} no alvo`}</span>
    </div>`).join('');
}

function renderExitEquity(curves, overview) {
  const canvas = $('#exit-equity-chart');
  const empty = $('#exit-equity-empty');
  const names = overview.arms.map((arm) => arm.arm);
  const longest = Math.max(0, ...names.map((name) => (curves[name] || []).length));
  if (longest < 2) {
    canvas.style.display = 'none';
    empty.hidden = false;
    $('#exit-legend').innerHTML = '';
    setText('#exit-equity-range', '—');
    return;
  }
  canvas.style.display = 'block';
  empty.hidden = true;

  const series = names.map((name) => ({
    points: (curves[name] || []).map((row) => ({ t: row.ts, y: row.total_value })),
    color: EXIT_COLORS[name] || '#8b94b2',
    width: name === 'rule' ? 2.5 : 1.8,
  }));
  const spine = curves[names[0]] || [];
  series.push({
    points: spine.map((row) => ({ t: row.ts, y: overview.capital })),
    color: 'rgba(255,255,255,0.18)', width: 1, dash: [4, 4],
  });
  /* No fill: four shaded areas stacked on one canvas hide each other, and the
     answer here is where the lines separate, not the area under any of them. */
  drawChart(canvas, series, { fill: false, tipTarget: $('#exit-equity-tip') });

  $('#exit-legend').innerHTML = overview.arms.map((arm) => `
    <span class="legend-item"><i class="legend-swatch"
      style="border-top-color:${EXIT_COLORS[arm.arm] || '#8b94b2'}"></i>${arm.label}</span>`).join('')
    + '<span class="legend-item"><i class="legend-swatch"'
    + ' style="border-top-color:rgba(255,255,255,0.4);border-top-style:dashed"></i>capital de partida</span>';

  setText('#exit-equity-range', `${dt(spine[0].ts)} — ${dt(spine[spine.length - 1].ts)}`);
}

function renderExitArms(overview, liveOverview) {
  /* The control arm is the validated book, restricted to the trades mirrored
     since the study started. Saying so is the whole comparison: the tiles at
     the top of this tab cover a longer span, so the two sets of numbers are
     the same money over different periods and only the table below is a
     like-for-like read. */
  setText('#exit-scope', liveOverview
    ? `Livro validado no total: ${money(liveOverview.total_pnl)} em `
      + `${liveOverview.closed_trades} operações fechadas, sobre `
      + `${money(liveOverview.start_capital, 0)}. A linha "regra decide" abaixo é `
      + 'este mesmo livro, limitado ao que o estudo espelhou, com o mesmo '
      + `$${nf(overview.quote_per_trade, 0)} por posição sobre `
      + `${money(overview.capital, 0)}.`
    : '—');
  $('#exit-arms-table tbody').innerHTML = overview.arms.map((arm) => {
    const inherited = arm.adopted_trades
      ? ` <span class="muted">(${arm.adopted_trades} herdada${arm.adopted_trades > 1 ? 's' : ''})</span>`
      : '';
    return `<tr${arm.arm === 'rule' ? ' class="row-strong"' : ''}>
      <td>${arm.label}</td>
      <td class="num ${cls(arm.total_pnl)}">${money(arm.total_pnl)}</td>
      <td class="num ${cls(arm.return_pct)}">${pct(arm.return_pct)}</td>
      <td class="num ${cls(arm.vs_rule_pct)}">${arm.arm === 'rule' ? '—' : pct(arm.vs_rule_pct)}</td>
      <td class="num">${arm.closed_trades}${inherited}</td>
      <td class="num">${arm.closed_trades ? `${nf(arm.win_rate_pct, 0)}%` : '—'}</td>
      <td class="num ${cls(arm.avg_trade_pct)}">${arm.closed_trades ? pct(arm.avg_trade_pct) : '—'}</td>
      <td class="num">${arm.arm === 'rule' ? '—' : arm.hit_target}</td>
      <td class="num">${arm.open_positions}</td>
    </tr>`;
  }).join('');
}

function renderExitPaired(overview) {
  const rows = Object.entries(overview.paired).filter(([, data]) => data.trades > 0);
  $('#exit-paired-empty').hidden = rows.length > 0;
  $('#exit-paired-table tbody').innerHTML = rows.map(([arm, data]) => `<tr>
      <td>+${arm.slice(1)}%</td>
      <td class="num">${data.trades}</td>
      <td class="num ${cls(data.rule_minus_target_pp)}">${signed(data.rule_minus_target_pp, 2)} pp</td>
      <td class="num">${data.rule_ahead}</td>
      <td class="num">${data.target_ahead}</td>
      <td class="num">${data.identical}</td>
    </tr>`).join('');
  setText('#exit-paired-note',
    rows.length ? `${rows[0][1].trades} pares por alvo, no máximo` : 'sem pares ainda');
}

function renderExitLedger(open, closed) {
  const label = (row) => (row.target_pct == null ? 'regra decide' : `+${nf(row.target_pct, 0)}%`);
  const rows = [...open, ...closed];
  $('#exit-ledger-empty').hidden = rows.length > 0;
  setText('#exit-ledger-note', `${open.length} abertas · ${closed.length} fechadas`);
  $('#exit-ledger-table tbody').innerHTML = rows.map((row) => `<tr>
      <td>${label(row)}</td>
      <td class="mono">${row.symbol}</td>
      <td>${row.status === 'open' ? 'aberta' : 'fechada'}${row.adopted ? ' <span class="muted">herdada</span>' : ''}</td>
      <td class="num mono">${num(row.entry_price)}</td>
      <td class="num mono">${row.target_price == null ? '—' : num(row.target_price)}</td>
      <td class="num mono">${row.exit_price == null ? '—' : num(row.exit_price)}</td>
      <td class="num ${cls(row.pnl)}">${row.pnl == null ? '—' : `${money(row.pnl)} (${pct(row.return_pct)})`}</td>
      <td class="muted">${row.reason || '—'}</td>
    </tr>`).join('');
}

/* The sixth tile carries a different fact in each book. The live one has enough
   equity snapshots for a Sharpe ratio; the experiment does not, and would only
   be reporting the noise in a week of paper trading. What it has instead is the
   information coefficient from its walk-forward, which is the number that says
   whether the ranking works at all. */
function renderLastKpi(overview) {
  if (state.book === 'live') {
    setText('#kpi-last-label', 'Sharpe');
    setText('#kpi-sharpe', nf(overview.sharpe, 2));
    setText('#kpi-last-sub', 'retorno por unidade de risco');
    return;
  }
  const ic = overview.model?.information_coefficient;
  setText('#kpi-last-label', 'Coef. de informação');
  setText('#kpi-sharpe', ic?.mean == null ? '—' : nf(ic.mean, 3),
    ic?.mean > 0 ? 'pos' : '');
  setText('#kpi-last-sub', ic?.mean == null
    ? 'sem modelo treinado'
    : `t = ${nf(ic.t_stat, 1)} em ${ic.days} dias fora da amostra`);
}

/* -------------------------------------------------------------- the ML book */

async function loadLabBook(overview) {
  const [signals, status] = await Promise.all([
    api('/lab/signals').catch(() => ({ rows: [] })),
    api('/lab/status'),
  ]);
  state.lab = { overview, signals, status };
  renderLabModel(overview, status);
  renderLabFolds(overview.model);
  renderLabRanking(signals, overview.model);

  const toggle = $('#btn-lab-toggle');
  toggle.textContent = status.running ? 'Parar' : 'Ligar';
  toggle.className = `btn btn-small ${status.running ? 'btn-danger' : 'btn-primary'}`;
}

function renderLabModel(overview, status) {
  const model = overview.model;
  $('#lab-model-empty').hidden = Boolean(model);
  $('#lab-model-cards').hidden = !model;
  if (!model) { $('#lab-model-cards').innerHTML = ''; return; }

  const ic = model.information_coefficient || {};
  const cards = [
    {
      label: 'Vantagem mediana',
      value: `${pct(model.median_edge)}/dia`,
      tone: cls(model.median_edge),
      note: `acima de segurar as 18 em partes iguais, em ${model.usable_folds} janelas`,
    },
    {
      label: 'Janelas positivas',
      value: `${model.positive_folds}/${model.usable_folds}`,
      note: `${model.beat_baseline_folds} bateram a referência no total acumulado`,
    },
    {
      label: 'Controle embaralhado',
      value: model.null_edge == null ? '—' : `${pct(model.null_edge)}/dia`,
      tone: model.null_edge < 0 ? 'pos' : 'neg',
      note: 'mesmo teste com os resultados trocados entre as moedas do dia. '
          + 'Perto de zero é o esperado; perto do número de cima significaria '
          + 'que a vantagem nunca foi escolha de moeda.',
    },
    {
      label: 'Giro médio',
      value: `${nf(model.mean_turnover * 100, 1)}%/dia`,
      note: `rebalanceia a cada ${model.rebalance_days} dias · cada troca completa `
          + `custa ${nf(overview.cost_per_trade_pct, 2)}%`,
    },
    {
      label: 'Capital em uso',
      value: money(overview.capital_at_work, 0),
      note: `de ${money(overview.start_capital, 0)} · cesta de ${model.top_k} a `
          + `${money(overview.capital_at_work / model.top_k, 0)} cada · retorno `
          + `sobre o que está em uso: ${pct(overview.return_on_capital_at_work_pct)}`,
    },
    {
      label: 'Último ciclo',
      value: status.last_day || '—',
      note: status.last_rebalance
        ? `último rebalanceamento em ${status.last_rebalance}`
        : 'ainda não rebalanceou',
    },
  ];

  if (!model.skill_is_coin_picking) {
    cards.push({
      label: 'Atenção', warn: true, value: 'controle não ficou atrás',
      note: 'O teste com rótulos embaralhados foi tão bem quanto o modelo. '
          + 'Isso significa que a vantagem medida não é escolha de moeda.',
    });
  }

  $('#lab-model-cards').innerHTML = cards.map((card) => `
    <div class="stat-card${card.warn ? ' warn' : ''}">
      <span class="stat-label">${escape(card.label)}</span>
      <strong class="stat-value ${card.tone || ''}">${escape(card.value)}</strong>
      <span class="stat-note">${escape(card.note)}</span>
    </div>`).join('');
}

function renderLabFolds(model) {
  const body = $('#lab-cv-table tbody');
  const folds = model?.folds || [];
  body.innerHTML = folds.map((fold) => `
    <tr>
      <td>${escape(fold.test_from)} — ${escape(fold.test_to)}</td>
      <td class="num">${fold.days}</td>
      <td class="num">${nf(fold.turnover * 100, 1)}%</td>
      <td class="num ${cls(fold.net_per_day)}">${pct(fold.net_per_day, 3)}</td>
      <td class="num muted">${pct(fold.baseline_per_day, 3)}</td>
      <td class="num ${cls(fold.edge)}">${pct(fold.edge, 3)}</td>
      <td class="num ${Math.abs(fold.t_stat) > 2 ? cls(fold.t_stat) : 'muted'}">${nf(fold.t_stat, 2)}</td>
      <td class="num ${cls(fold.total_pct)}">${pct(fold.total_pct, 1)}</td>
      <td class="num muted">${pct(fold.baseline_total_pct, 1)}</td>
    </tr>`).join('');
  $('#lab-cv-note').textContent = model
    ? `${model.total_trade_days} dias fora da amostra · t mediano ${nf(model.median_t_stat, 2)}`
      + ` (acima de 2 seria significativo)`
    : '—';
}

function renderLabRanking(signals, model) {
  const rows = signals.rows || [];
  $('#lab-rank-empty').hidden = rows.length > 0;
  $('#lab-rank-table').hidden = rows.length === 0;
  $('#lab-rank-note').textContent = rows.length
    ? `${signals.day} · a cesta são os ${signals.top_k} primeiros`
    : (signals.error || '—');
  $('#lab-rank-table tbody').innerHTML = rows.map((row) => `
    <tr${row.wanted ? ' class="row-on"' : ''}>
      <td class="num">${row.rank}</td>
      <td><strong>${escape(row.symbol)}</strong></td>
      <td class="num">${nf(row.probability, 3)}</td>
      <td class="num">${nf(row.close, row.close < 1 ? 4 : 2)}</td>
      <td class="num ${cls(row.ret_7)}">${row.ret_7 == null ? '—' : pct(row.ret_7, 1)}</td>
      <td class="num muted">${row.rsi_14 == null ? '—' : nf(row.rsi_14, 0)}</td>
      <td class="num muted">${row.funding_bp == null ? '—' : nf(row.funding_bp, 2)}</td>
      <td>${row.wanted ? '<span class="chip ok">na cesta</span>' : ''}</td>
    </tr>`).join('');
}

/* The panel that answers "why has nothing happened". A book of seventeen
   allocations is silent most of the time, and silence from a working bot and
   silence from a broken one look identical unless the interface shows the
   number each strategy is watching and how far it is from the line. */
async function loadSignals() {
  const { rows } = await api('/signals');
  const body = $('#signals-table tbody');
  $('#signals-empty').hidden = rows.length > 0;
  $('#signals-table').hidden = rows.length === 0;

  const ready = rows.filter((r) => r.trigger && r.trigger.met).length;
  const holding = rows.filter((r) => r.holding).length;
  setText('#signals-summary', rows.length
    ? `${plural(rows.length, 'alocação', 'alocações')} · ${holding} comprada${holding === 1 ? '' : 's'}`
      + ` · ${ready} com o gatilho atendido`
    : '—');

  body.innerHTML = rows.map((row) => {
    if (!row.trigger) {
      return `<tr><td class="mono">${esc(row.symbol || '—')}</td>
        <td class="muted" colspan="7">${esc(row.error || 'sem gatilho declarado')}</td></tr>`;
    }
    const t = row.trigger;
    const distance = t.distance_pct == null ? num(t.gap) : `${signed(t.distance_pct, 1)}%`;
    return `
    <tr>
      <td class="mono">${esc(row.symbol)} <span class="muted">${esc(row.interval)}</span></td>
      <td class="muted">${esc(row.strategy_label)}</td>
      <td>${row.holding
        ? '<span class="chip warn">saída</span>'
        : '<span class="chip">entrada</span>'}
        <span class="muted">${esc(triggerText(t))}</span></td>
      <td class="num mono">${num(t.left_value)}</td>
      <td class="num mono">${num(t.right_value)}</td>
      <td class="num ${t.met ? 'pos' : 'muted'}">${distance}</td>
      <td class="num mono">${triggerPrice(row)}</td>
      <td>${t.met ? '<span class="chip ok">atendido</span>' : ''}</td>
    </tr>`;
  }).join('');
}

/* The same trigger in the unit that is actually on the screen a trader is
   watching. "ROC 40 abaixo de 0" is exact and unwatchable; "vira em 1.4960"
   is the same fact as a line on the chart.

   It is a level for the next close, not a standing order: the indicator's
   reference bars roll forward every candle, so the level moves on its own even
   if the price does not. */
function triggerPrice(row) {
  const level = row.trigger_price;
  if (level == null) return '<span class="muted">—</span>';
  const move = row.price ? (level / row.price - 1) * 100 : null;
  return `${num(level)} <span class="muted">${move == null ? '' : signed(move, 1) + '%'}</span>`;
}

/* "RSI 14 abaixo de 25" - the comparison in words, so the two numbers beside
   it do not have to be read against an operator symbol. */
function triggerText(trigger) {
  const OP = { '>': 'acima de', '>=': 'pelo menos', '<': 'abaixo de', '<=': 'no máximo' };
  const left = indicatorText(trigger.left);
  const right = trigger.right ? indicatorText(trigger.right) : num(trigger.right_value);
  return `${left} ${OP[trigger.operator] || trigger.operator} ${right}`;
}

/* The decision itself, on one line, above the full indicator list. */
function triggerBox(trigger) {
  if (!trigger) return '';
  return `<p class="sigtrigger ${trigger.met ? 'is-met' : ''}">
    <span class="sigtrigger-label">${esc(triggerText(trigger))}</span>
    <span class="sigtrigger-nums mono">${num(trigger.left_value)}
      <span class="muted">vs</span> ${num(trigger.right_value)}</span>
  </p>`;
}

/* The waterfall exists because a single "resultado total" number hides the two
   things that make it, and they are not the same kind of money: one is banked
   and one can still evaporate. Reading it top to bottom gives the whole
   arithmetic - what was put in, what closed trades did to it, what open trades
   are currently doing to it, and what is left. */
const MODE_TEXT = {
  live: 'CONTA REAL — dinheiro de verdade',
  paper: 'papel — nenhuma ordem sai daqui',
  testnet: 'conta de teste (testnet) — dinheiro fictício',
};

function renderPnl(overview) {
  setText('#pnl-mode', MODE_TEXT[overview.mode] || MODE_TEXT.testnet);
  $('#pnl-mode').className = overview.mode === 'live' ? 'neg' : 'muted';

  const rows = [
    { label: 'Capital inicial', tone: '',
      sub: overview.capital_at_work
        ? `ponto de partida · ${money(overview.capital_at_work, 0)} podem estar aplicados de cada vez`
        : 'ponto de partida',
      value: money(overview.start_capital) },
    { label: 'Resultado realizado', tone: cls(overview.realised_pnl),
      sub: `${plural(overview.closed_trades, 'operação encerrada', 'operações encerradas')} · ${overview.wins}G / ${overview.losses}P`,
      value: signed(overview.realised_pnl) },
    { label: 'Resultado em aberto', tone: cls(overview.unrealised_pnl),
      sub: `${plural(overview.open_positions, 'posição', 'posições')} · ${money(overview.invested)} aplicados`,
      value: signed(overview.unrealised_pnl) },
    { label: 'Patrimônio agora', tone: cls(overview.total_pnl), total: true,
      sub: `${pct(overview.total_return_pct)} sobre o capital inicial`,
      value: money(overview.total_value) },
  ];
  $('#pnl-waterfall').innerHTML = rows.map((r) => `
    <div class="wf-row${r.total ? ' wf-total' : ''}">
      <div class="wf-text">
        <span class="wf-label">${r.label}</span>
        <span class="wf-sub">${r.sub}</span>
      </div>
      <strong class="wf-value ${r.tone}">${r.value}</strong>
    </div>`).join('')
    + `<p class="wf-note">Todos os valores já descontam taxas e escorregamento.
       ${feeNote(overview)}</p>`;
}

/* The testnet charges nothing, so "estimated fees" there is a number the
   account never paid. Saying which of the two is being shown matters more than
   the number: the estimate is what a real account would have cost, and telling
   the operator that is the whole point of showing it before going live. */
function feeNote(overview) {
  if (overview.mode === 'paper') {
    return `Livro em papel: nada é enviado à corretora. Cada operação já desconta`
      + ` ${nf(overview.cost_per_trade_pct, 2)}% de ida e volta — taxa mais`
      + ` escorregamento, a mesma conta que o livro ao vivo usa.`;
  }
  const measured = overview.fees_measured_orders || 0;
  const total = overview.fees_total_orders || 0;
  const turnover = money(overview.turnover);
  if (measured && overview.fees_charged > 0) {
    return `Taxas cobradas: ${money(overview.fees_charged, 4)} sobre ${turnover}`
      + ` negociados${measured < total
        ? ` (${measured} de ${total} ordens com taxa medida)` : ''}.`;
  }
  if (measured) {
    return `A corretora não cobrou taxa em nenhuma das ${measured} ordens medidas`
      + ` — normal na testnet. Numa conta real as mesmas ${turnover} negociados`
      + ` custariam cerca de ${money(overview.fees_estimate)}.`;
  }
  return `Taxas estimadas até agora: ${money(overview.fees_estimate)} sobre`
    + ` ${turnover} negociados.`;
}

function renderEquity(rows, overview) {
  const canvas = $('#equity-chart');
  const empty = $('#equity-empty');
  if (rows.length < 2) {
    canvas.style.display = 'none';
    empty.hidden = false;
    $('#equity-range').textContent = '—';
    return;
  }
  canvas.style.display = 'block';
  empty.hidden = true;

  const points = rows.map((row) => ({ t: row.ts, y: row.total_value }));
  const up = points[points.length - 1].y >= points[0].y;
  drawChart(canvas, [{
    points,
    color: up ? '#19d69b' : '#ff5f70',
    fillColor: up ? 'rgba(25,214,155,0.22)' : 'rgba(255,95,112,0.20)',
  }, {
    points: points.map((p) => ({ t: p.t, y: overview.start_capital })),
    color: 'rgba(255,255,255,0.18)', width: 1, dash: [4, 4], fill: false,
  }], { tipTarget: $('#equity-tip') });

  $('#equity-range').textContent = `${dt(rows[0].ts)} — ${dt(rows[rows.length - 1].ts)}`;
}

function renderPositions(positions) {
  const body = $('#positions-table tbody');
  $('#positions-empty').hidden = positions.length > 0;
  $('#positions-table').style.display = positions.length ? '' : 'none';
  body.innerHTML = positions.map((p) => `
    <tr>
      <td class="sym">${p.symbol}</td>
      <td>${p.strategy
        ? `<span class="chip">${escape(p.strategy)}</span> <span class="muted">${escape(p.interval)}</span>`
        : `<span class="chip">ranking</span> <span class="muted">p ${nf(p.entry_prob, 3)}</span>`}</td>
      <td class="num">${money(p.entry_quote)}</td>
      <td class="num">${nf(p.entry_price, 4)}</td>
      <td class="num">${nf(p.mark_price, 4)}</td>
      <td class="num">${money(p.value)}</td>
      <td class="num ${cls(p.unrealised_pnl)}">${signed(p.unrealised_pnl)} <span class="muted">${pct(p.unrealised_pct)}</span></td>
    </tr>`).join('');
}

function renderBreakdown(rows) {
  const box = $('#breakdown-list');
  $('#breakdown-empty').hidden = rows.length > 0;
  if (!rows.length) { box.innerHTML = ''; return; }
  const scale = Math.max(...rows.map((r) => Math.abs(r.pnl))) || 1;
  box.innerHTML = rows.map((r) => `
    <div class="bar-row">
      <span class="bar-name">${r.name}</span>
      <span class="bar-value ${cls(r.pnl)}">${signed(r.pnl)}</span>
      <div class="bar-track">
        <div class="bar-fill ${r.pnl >= 0 ? 'pos' : 'neg'}"
             style="left:0;width:${Math.abs(r.pnl) / scale * 100}%"></div>
      </div>
      <span class="bar-meta">${r.trades} ops · acerto ${nf(r.win_rate_pct, 0)}% · média ${pct(r.avg_return_pct)}</span>
    </div>`).join('');
}

// Event messages carry exception text, which can contain anything.
function escape(value) {
  return String(value).replace(/[&<>"]/g,
    (ch) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[ch]));
}

function renderEvents(events, target = '#events-list') {
  $(target).innerHTML = events.length
    ? events.map((e) => {
      // A collapsed row covers a span, so show where it started as well as the
      // count - "120x" without "since 01:40" says nothing about the outage.
      const repeats = e.repeats > 1
        ? `<span class="repeats" title="primeira em ${dt(e.first_ts)}">${e.repeats}x</span>`
        : '';
      const since = e.repeats > 1 && e.first_ts
        ? `<span class="muted">desde ${dt(e.first_ts)}</span>` : '';
      return `
      <li>
        <time>${dt(e.ts)}</time>
        <span class="level ${e.level}">${e.level}</span>
        <span>${escape(e.message)} ${repeats} ${since}</span>
      </li>`;
    }).join('')
    : '<li><span class="muted">Sem atividade ainda.</span></li>';
}

/* --------------------------------------------------------------------- lab */

async function loadLab() {
  const onlyValidated = $('#chk-validated').checked;
  const rows = await api(`/research/leaderboard?limit=50&only_validated=${onlyValidated}`);
  state.leaderboard = rows;
  renderLeaderboard(rows);
  const status = await api('/research/status');
  renderResearchProgress(status);
  const [feeds, headlines] = await Promise.all([
    api('/feeds'), api('/feeds/headlines?limit=12'),
  ]);
  renderFeeds(feeds);
  renderHeadlines(headlines);
}

// Series whose past can be downloaded are already researchable; the rest are
// worth showing precisely because the number that matters is how long they have
// been accumulating, and that only goes up if the process stays alive.
const FEED_LABELS = {
  funding: ['Financiamento', 'perpétuos Binance, histórico completo desde 2020'],
  open_interest: ['Contratos em aberto', 'retenção de 30 dias, só acumula daqui'],
  long_short: ['Posição comprada', 'retenção de 30 dias, só acumula daqui'],
  fear_greed: ['Medo e ganância', 'índice diário, histórico desde 2018'],
  headlines: ['Manchetes', 'RSS público, carimbado na hora em que vimos'],
};

function renderFeeds(data) {
  const chip = $('#feeds-state');
  chip.textContent = data.running ? 'coletando' : 'parado';
  chip.className = `chip ${data.running ? 'ok' : 'warn'}`;

  const rows = [...data.feeds, data.news];
  $('#feeds-list').innerHTML = rows.map((row) => {
    const [name, note] = FEED_LABELS[row.feed] || [row.feed, ''];
    const status = row.status || {};
    // A feed that has never failed shows nothing; one that has shows the error,
    // because a collector quietly returning zero rows for a week is the exact
    // failure this whole panel exists to make impossible to miss.
    const error = status.last_error
      ? `<span class="feed-error" title="${escape(status.last_error)}">falhou</span>` : '';
    return `
    <div class="feed">
      <div class="feed-name">${name} ${error}<span class="muted">${note}</span></div>
      <div class="feed-nums">
        <span><b>${(row.rows || 0).toLocaleString('pt-BR')}</b> linhas</span>
        <span><b>${row.days || 0}</b> dias</span>
        ${row.sources ? `<span><b>${row.sources}</b> fontes</span>` : ''}
        ${row.symbols ? `<span><b>${row.symbols}</b> pares</span>` : ''}
        <span class="muted">visto ${dt(status.last_run)}</span>
      </div>
    </div>`;
  }).join('');
}

function renderHeadlines(rows) {
  $('#feeds-headlines').innerHTML = rows.length
    ? rows.map((row) => {
      // Both timestamps are shown on purpose. When they disagree by hours, the
      // reason not to train on the publisher's one is visible rather than
      // asserted.
      const lag = row.published_at
        ? `<span class="muted" title="hora declarada pela fonte">publicado ${dt(row.published_at)}</span>` : '';
      return `
      <li>
        <time>${dt(row.observed_at)}</time>
        <span class="src">${escape(row.source)}</span>
        <span>${escape(row.title)} ${lag}</span>
      </li>`;
    }).join('')
    : '<li><span class="muted">Nenhuma manchete coletada ainda.</span></li>';
}

function renderLeaderboard(rows) {
  const body = $('#leaderboard-table tbody');
  $('#leaderboard-empty').hidden = rows.length > 0;
  $('#leaderboard-table').style.display = rows.length ? '' : 'none';
  body.innerHTML = rows.map((row) => {
    const test = row.test;
    const beats = test.total_return_pct > test.buy_hold_return_pct;
    return `
    <tr class="clickable ${state.selected.has(row.id) ? 'selected' : ''}" data-id="${row.id}">
      <td class="tight"><input type="checkbox" data-pick="${row.id}" ${state.selected.has(row.id) ? 'checked' : ''}></td>
      <td class="sym">${row.symbol}</td>
      <td>${row.interval}</td>
      <td>${row.label}<br><span class="muted">${paramText(row.params)}</span></td>
      <td class="num ${cls(test.total_return_pct)}">${pct(test.total_return_pct)}</td>
      <td class="num muted">${pct(test.buy_hold_return_pct)}</td>
      <td class="num">${nf(test.sharpe, 2)}</td>
      <td class="num neg">${nf(test.max_drawdown_pct, 1)}%</td>
      <td class="num">${test.trades}</td>
      <td class="num">${nf(row.score, 2)}</td>
      <td>${row.validated
        ? '<span class="chip ok">aprovada</span>'
        : `<span class="chip ${beats ? 'warn' : 'bad'}">${beats ? 'parcial' : 'reprovada'}</span>`}</td>
    </tr>`;
  }).join('');

  $$('#leaderboard-table tbody tr').forEach((tr) => {
    tr.addEventListener('click', (event) => {
      const id = Number(tr.dataset.id);
      if (event.target.matches('input[data-pick]')) {
        if (event.target.checked) state.selected.add(id); else state.selected.delete(id);
        tr.classList.toggle('selected', state.selected.has(id));
        return;
      }
      showDetail(id);
    });
  });
}

const paramText = (params) =>
  Object.entries(params).filter(([, v]) => v !== 0).map(([k, v]) => `${k}=${v}`).join(' ');

async function showDetail(id) {
  const row = await api(`/research/result/${id}`);
  $('#detail-panel').hidden = false;
  $('#detail-title').textContent = `${row.label} · ${row.symbol} ${row.interval}`;

  const cards = [
    ['Retorno fora da amostra', pct(row.test.total_return_pct), cls(row.test.total_return_pct)],
    ['Retorno no treino', pct(row.train.total_return_pct), cls(row.train.total_return_pct)],
    ['Buy & hold (OOS)', pct(row.test.buy_hold_return_pct), ''],
    ['Sharpe OOS', nf(row.test.sharpe, 2), ''],
    ['Drawdown OOS', `${nf(row.test.max_drawdown_pct, 1)}%`, 'neg'],
    ['Operações OOS', String(row.test.trades), ''],
    ['Acerto OOS', `${nf(row.test.win_rate_pct, 0)}%`, ''],
    ['Fator de lucro', nf(row.test.profit_factor, 2), ''],
    ['Exposição', `${nf(row.test.exposure_pct, 0)}%`, ''],
    ['Consistência OOS', `${nf(row.test.consistency_pct, 0)}%`, ''],
    ['Risco', riskText(row.risk), ''],
  ];
  $('#detail-metrics').innerHTML = cards.map(([label, value, klass]) =>
    `<div class="detail-item"><span>${label}</span><strong class="${klass}">${value}</strong></div>`).join('');

  const points = row.curve.map((p) => ({ t: p.time, y: p.equity }));
  drawChart($('#detail-chart'), [{
    points, color: '#5b7cfa', fillColor: 'rgba(91,124,250,0.24)',
  }]);
  $('#detail-panel').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

function riskText(risk) {
  const parts = [];
  if (risk.stop_pct) parts.push(`stop ${(risk.stop_pct * 100).toFixed(0)}%`);
  if (risk.take_pct) parts.push(`alvo ${(risk.take_pct * 100).toFixed(0)}%`);
  if (risk.trail_pct) parts.push(`trailing ${(risk.trail_pct * 100).toFixed(0)}%`);
  return parts.join(' · ') || 'só sinal';
}

function renderResearchProgress(status) {
  const box = $('#research-progress');
  if (!status || status.status !== 'running') {
    box.hidden = true;
    $('#btn-research').disabled = false;
    $('#btn-research').textContent = 'Rodar pesquisa';
    return;
  }
  box.hidden = false;
  $('#btn-research').disabled = true;
  $('#btn-research').textContent = 'Pesquisando…';
  $('#research-stage').textContent = status.stage || 'processando';
  $('#research-count').textContent = `${status.progress}/${status.total} · ${status.results} candidatos`;
  $('#research-bar').style.width = `${status.total ? (status.progress / status.total) * 100 : 0}%`;
}

function watchResearch() {
  if (state.researchTimer) return;
  state.researchTimer = setInterval(async () => {
    const status = await api('/research/status');
    renderResearchProgress(status);
    if (!status || status.status !== 'running') {
      clearInterval(state.researchTimer);
      state.researchTimer = null;
      if (status?.status === 'error') toast(`Pesquisa falhou: ${status.error?.slice(0, 90)}`, 'error');
      else toast('Pesquisa concluída', 'ok');
      if (state.view === 'lab') loadLab();
    } else if (state.view === 'lab') {
      loadLab();
    }
  }, 2500);
}

/* ------------------------------------------------------------------ trades */

const REASON_PT = {
  signal: 'sinal de saída da estratégia',
  stop: 'stop de perda',
  target: 'alvo de lucro',
  'trailing stop': 'stop móvel',
  end: 'ainda aberta no fim da janela',
  manual: 'fechada manualmente',
  stale: 'sem saldo para vender',
};

/* The strategy rules live in the engine in English, because the code, the README
   and the roadmap are English. The interface is not, and a card that mixes the
   two is the one place a reader has to stop and translate to check whether the
   numbers beside it make sense. Keyed by the exact string in strategies.py. */
const RULE_PT = {
  'fast EMA rises above the slow EMA': 'a média exponencial rápida cruza acima da lenta',
  'fast EMA falls back below the slow EMA': 'a média exponencial rápida volta a cair abaixo da lenta',
  'MACD histogram turns positive': 'o histograma do MACD fica positivo',
  'MACD histogram turns negative': 'o histograma do MACD fica negativo',
  'Supertrend flips bullish': 'o Supertrend vira para alta',
  'Supertrend flips bearish': 'o Supertrend vira para baixa',
  'price closes above the N-bar high': 'o preço fecha acima da máxima do período',
  'price closes below the M-bar low': 'o preço fecha abaixo da mínima do período',
  'price closes above the upper Bollinger band': 'o preço fecha acima da banda superior de Bollinger',
  'price falls back below the moving average': 'o preço volta a cair abaixo da média móvel',
  'price closes below the lower Bollinger band': 'o preço fecha abaixo da banda inferior de Bollinger',
  'price recovers above the moving average': 'o preço se recupera acima da média móvel',
  'RSI drops below the oversold threshold': 'o RSI cai abaixo do limite de sobrevenda',
  'RSI recovers above the upper threshold': 'o RSI se recupera acima do limite superior',
  '%K crosses above %D while still near oversold': 'a %K cruza acima da %D ainda perto da sobrevenda',
  '%K reaches the overbought threshold': 'a %K atinge o limite de sobrecompra',
  'rate of change rises above the threshold': 'a taxa de variação sobe acima do limite',
  'rate of change falls back below the threshold': 'a taxa de variação volta a cair abaixo do limite',
  'fast EMA above slow EMA while ADX confirms a trending market':
    'média rápida acima da lenta com o ADX confirmando mercado em tendência',
  'EMA trend reverses or ADX drops below the minimum':
    'a tendência das médias inverte ou o ADX cai abaixo do mínimo',
  'price falls the entry z-score below rolling VWAP':
    'o preço cai o z-score de entrada abaixo do VWAP móvel',
  'price returns to the exit z-score above VWAP':
    'o preço volta ao z-score de saída acima do VWAP',
  'enough member sleeves vote long at once': 'estratégias suficientes votam comprado ao mesmo tempo',
  'votes fall back below the minimum': 'os votos caem abaixo do mínimo',
  'always in': 'sempre comprado',
  'never exits': 'nunca sai',
};

/* Values span many magnitudes (a price of 0.32, an ADX of 27, a VWAP of
   64 000), so pick the precision per number instead of fixing it. */
function num(value) {
  if (value == null) return '—';
  const size = Math.abs(value);
  if (size === 0) return '0';
  if (size >= 1000) return nf(value, 2);
  if (size >= 1) return nf(value, 4);
  return nf(value, 6);
}

function dur(seconds) {
  if (seconds == null) return '—';
  const days = Math.floor(seconds / 86400);
  const hours = Math.floor((seconds % 86400) / 3600);
  const mins = Math.floor((seconds % 3600) / 60);
  if (days) return hours ? `${days}d ${hours}h` : `${days}d`;
  if (hours) return mins ? `${hours}h ${mins}min` : `${hours}h`;
  if (mins) return `${mins}min`;
  return `${seconds}s`;
}

const esc = (text) => String(text ?? '').replace(/[<>&]/g, (c) => (
  { '<': '&lt;', '>': '&gt;', '&': '&amp;' }[c]));

function valueList(values) {
  const entries = Object.entries(values || {});
  if (!entries.length) return '<p class="muted">Sem indicadores registrados.</p>';
  return `<dl class="sigvals">${entries.map(([name, value]) =>
    `<div><dt>${esc(indicatorText(name))}</dt><dd>${num(value)}</dd></div>`).join('')}</dl>`;
}

/* Indicator names come from the engine too, and several carry their own period
   ("MA 14", "20-bar high"), so an exact map is not enough: translate the whole
   name where it is fixed, and the words around the number where it is not. */
const INDICATOR_PT = {
  price: 'preço',
  'upper band': 'banda superior',
  'lower band': 'banda inferior',
  'signal line': 'linha de sinal',
  histogram: 'histograma',
  'Supertrend direction': 'direção do Supertrend',
  'exit level': 'nível de saída',
  'oversold level': 'nível de sobrevenda',
  'overbought level': 'nível de sobrecompra',
  threshold: 'limite',
  'ADX minimum': 'ADX mínimo',
  'entry z-score': 'z-score de entrada',
  'exit z-score': 'z-score de saída',
  'z-score': 'z-score',
  votes: 'votos',
  'votes required': 'votos necessários',
  'realised volatility': 'volatilidade realizada',
  'volatility cap': 'teto de volatilidade',
};

const INDICATOR_PATTERNS = [
  [/^MA (\d+)$/, 'média móvel $1'],
  [/^EMA (\d+) \(trend filter\)$/, 'média exponencial $1 (filtro de tendência)'],
  [/^EMA (\d+)$/, 'média exponencial $1'],
  [/^VWAP (\d+)$/, 'VWAP $1'],
  [/^(\d+)-bar high$/, 'máxima de $1 candles'],
  [/^(\d+)-bar low$/, 'mínima de $1 candles'],
  [/^vote: (.+)$/, 'voto: $1'],
];

function indicatorText(name) {
  if (INDICATOR_PT[name]) return INDICATOR_PT[name];
  for (const [pattern, replacement] of INDICATOR_PATTERNS) {
    if (pattern.test(name)) return name.replace(pattern, replacement);
  }
  // ADX, ATR, RSI, ROC and MACD read the same in both languages.
  return name;
}

/* `exit_rule` carries the strategy's own rule when the strategy exited, and the
   bare reason code ("end", "stop") when something else did. Translate both, so
   the detail card never shows an English string the rest of the UI translates.
   A protective exit arrives already worded by the engine, with its own
   percentages in it, and falls through unchanged. */
function ruleText(rule) {
  return RULE_PT[rule] || REASON_PT[rule] || rule || '—';
}

/* The candle the rule fired on is not always the candle the order was sent on.
   Strategies hold a position between their entry and exit pulses, so one that
   is added to the book while its signal is already long buys candles after the
   move that justified it - and the indicator values shown belong to that
   earlier candle, not to the fill. Saying so is the difference between "it
   bought a breakout" and "it bought into a breakout that was ten days old". */
function triggerLine(signal) {
  if (!signal || !signal.bar_time) return '';
  const late = signal.bars_since_trigger;
  const when = `candle de ${dt(signal.bar_time)}`;
  const close = signal.bar_close == null ? '' : `, fechamento ${num(signal.bar_close)}`;
  return `<p class="sigmeta muted">sinal disparou no ${when}${close}${late
    ? ` · ${late} ${late === 1 ? 'candle' : 'candles'} antes da ordem` : ''}</p>`;
}

function sideCard(title, rule, values, price, time, signal) {
  return `
    <div class="sigcard">
      <h4>${title}</h4>
      <p class="sigrule">${esc(ruleText(rule))}</p>
      <p class="sigmeta">preço ${num(price)}${time ? ` · ${dt(time)}` : ''}</p>
      ${triggerLine(signal)}
      ${triggerBox(signal && signal.trigger)}
      ${valueList(values)}
    </div>`;
}

/* One row plus its hidden explanation row. Trades are normalised upstream so
   live positions and simulated ones render through the same code. */
function tradeRow(trade, key) {
  const open = !trade.exit_time;
  const reason = REASON_PT[trade.reason] || trade.reason || '—';
  return `
    <tr class="trade-row" data-detail="${key}">
      <td class="expander">&#9656;</td>
      <td>${dt(trade.entry_time)}</td>
      <td>${open ? '<span class="chip warn">aberta</span>' : dt(trade.exit_time)}</td>
      <td>${dur(trade.duration_seconds)}</td>
      <td class="num">${num(trade.entry_price)}</td>
      <td class="num">${trade.exit_price == null ? num(trade.mark_price) : num(trade.exit_price)}</td>
      <td class="num ${cls(trade.pnl)}">${trade.pnl == null ? '—' : signed(trade.pnl)}</td>
      <td class="num ${cls(trade.return_pct)}">${trade.return_pct == null ? '—' : pct(trade.return_pct)}</td>
      <td class="mono num">${trade.entry_signal && trade.entry_signal.trigger
        ? `${num(trade.entry_signal.trigger.left_value)} <span class="muted">vs</span>`
          + ` ${num(trade.entry_signal.trigger.right_value)}`
        : '<span class="muted">—</span>'}</td>
      <td class="muted">${open ? 'em andamento' : esc(reason)}</td>
    </tr>
    <tr class="trade-detail" id="${key}" hidden>
      <td colspan="10">
        <div class="sigpair">
          ${sideCard('Sinal de entrada', trade.entry_rule, trade.entry_values,
                     trade.entry_price, trade.entry_time, trade.entry_signal)}
          ${open
            ? `<div class="sigcard"><h4>Saída</h4>
                 <p class="sigrule">${esc(ruleText(trade.exit_rule))}</p>
                 <p class="sigmeta muted">Ainda não ocorreu — é a regra que o robô
                   está esperando. Marcada a ${num(trade.mark_price)}.</p></div>`
            : sideCard('Sinal de saída', trade.exit_rule, trade.exit_values,
                       trade.exit_price, trade.exit_time, trade.exit_signal)}
        </div>
      </td>
    </tr>`;
}

function tradeGroup(group, index) {
  const trades = group.trades;
  const closed = trades.filter((t) => t.exit_time);
  const wins = closed.filter((t) => t.pnl > 0).length;
  const pnl = trades.reduce((sum, t) => sum + (t.pnl || 0), 0);
  const spans = closed.map((t) => t.duration_seconds).filter((v) => v != null);
  const avg = spans.length ? spans.reduce((a, b) => a + b, 0) / spans.length : null;
  const params = Object.entries(group.params || {}).map(([k, v]) => `${k}=${v}`).join(' ');

  return `
    <div class="tgroup">
      <div class="tgroup-head">
        <span class="sym">${esc(group.symbol)}</span>
        <span class="chip">${esc(group.strategy_label || group.strategy)}</span>
        <span class="muted">${esc(group.interval)}</span>
        <span class="muted mono">${esc(params)}</span>
        <span class="tgroup-stats">
          <b class="${cls(pnl)}">${signed(pnl)}</b>
          <span class="muted">${trades.length} ops · ${closed.length
            ? `acerto ${nf(wins / closed.length * 100, 0)}%` : 'nenhuma fechada'}${avg == null
            ? '' : ` · duração média ${dur(Math.round(avg))}`}</span>
        </span>
      </div>
      <div class="table-wrap">
        <table class="trades-table">
          <thead>
            <tr><th></th><th>Entrada</th><th>Saída</th><th>Duração</th>
                <th class="num">Preço entrada</th><th class="num">Preço saída</th>
                <th class="num">Resultado</th><th class="num">%</th>
                <th class="num">Sinal de compra</th><th>Motivo da saída</th></tr>
          </thead>
          <tbody>${trades.map((t, i) => tradeRow(t, `d-${index}-${i}`)).join('')}</tbody>
        </table>
      </div>
    </div>`;
}

/* Live positions carry their snapshot in entry_signal/exit_signal; simulated
   ones arrive flattened. Normalise so one renderer serves both. */
function normaliseLive(row) {
  return {
    ...row,
    entry_rule: row.entry_signal?.rule
      || 'não registrado (posição aberta antes do detalhamento de sinais)',
    exit_rule: row.exit_signal?.rule
      || (row.exit_time ? row.reason : row.pending_exit_rule),
    entry_values: row.entry_signal?.values,
    exit_values: row.exit_signal?.values,
  };
}

function groupBySymbol(rows) {
  const groups = new Map();
  for (const row of rows) {
    const key = `${row.symbol}|${row.strategy}|${row.interval}`;
    if (!groups.has(key)) {
      groups.set(key, {
        symbol: row.symbol, strategy: row.strategy, interval: row.interval,
        strategy_label: row.strategy_label, params: row.params, trades: [],
      });
    }
    groups.get(key).trades.push(row);
  }
  return [...groups.values()];
}

/* The ledger is deliberately not grouped. Grouping answers "how is this coin
   doing"; the panel above it already does that. This one answers "what did the
   robot do, in order", which is the question a statement answers, and a
   statement that reorders itself is not a statement. */
async function loadLedger() {
  const { orders, totals } = await api('/orders?limit=200');
  const body = $('#ledger-table tbody');

  $('#ledger-count').textContent = totals.orders
    ? `${plural(totals.orders, 'ordem', 'ordens')} · ${plural(totals.buys, 'compra', 'compras')} · ${plural(totals.sells, 'venda', 'vendas')}`
    : '—';
  $('#ledger-empty').hidden = totals.orders > 0;
  $('#ledger-table').hidden = totals.orders === 0;

  const cards = [
    ['Saiu do caixa', money(totals.spent), 'total das compras'],
    ['Voltou ao caixa', money(totals.received), 'total das vendas'],
    ['Resultado realizado', money(totals.realised_pnl), 'só de posições encerradas',
      cls(totals.realised_pnl)],
    (totals.fees_measured_orders && totals.fees_charged > 0
      ? ['Taxas cobradas', money(totals.fees_charged, 4), 'medidas na corretora']
      : ['Taxas estimadas', money(totals.fees_estimate),
        totals.fees_measured_orders ? 'testnet não cobrou nada' : '0,1% por ordem']),
  ];
  $('#ledger-totals').innerHTML = cards.map(([label, value, sub, tone]) => `
    <div class="ltot">
      <span class="ltot-label">${label}</span>
      <strong class="ltot-value ${tone || ''}">${value}</strong>
      <span class="ltot-sub">${sub}</span>
    </div>`).join('');

  body.innerHTML = orders.map((row) => `
    <tr>
      <td>${dt(row.ts)}</td>
      <td><span class="side ${row.is_buy ? 'buy' : 'sell'}">${row.is_buy ? 'COMPRA' : 'VENDA'}</span></td>
      <td class="mono">${esc(row.symbol)}</td>
      <td class="muted">${esc(row.strategy_label)}</td>
      <td class="num mono">${num(row.qty)}</td>
      <td class="num mono">${num(row.price)}</td>
      <td class="num">${money(row.quote)}</td>
      <td class="num ${cls(row.cash_delta)}">${signed(row.cash_delta)}</td>
      <td class="num ${cls(row.pnl)}">${row.pnl == null ? '—'
        : `${signed(row.pnl)} <span class="muted">${pct(row.return_pct)}</span>`}</td>
      <td class="muted">${row.is_buy ? 'entrada' : esc(ruleText(row.note))}${row.duration_seconds
        ? ` · ${dur(row.duration_seconds)}` : ''}</td>
    </tr>`).join('');
}

async function loadTrades() {
  const mode = state.tradesMode;
  const box = $('#trades-groups');
  box.innerHTML = '<p class="muted">Carregando…</p>';

  let groups;
  if (mode === 'live') {
    $('#trades-hint').textContent = 'Operações reais do robô, agrupadas por moeda. '
      + 'Clique em uma linha para ver os indicadores que dispararam a entrada e a saída.';
    groups = groupBySymbol((await api('/trades?limit=200')).map(normaliseLive));
  } else {
    $('#trades-hint').textContent = 'As mesmas estratégias em operação, aplicadas ao histórico '
      + 'recente, com o mesmo valor por ordem que o robô usa. Mostra como cada uma se '
      + 'comporta — é simulação, não dinheiro ganho.';
    groups = (await api('/trades/history')).filter((g) => !g.error);
  }

  const total = groups.reduce((sum, g) => sum + g.trades.length, 0);
  $('#trades-empty').hidden = total > 0;
  $('#trades-count').textContent = `${total} operações · ${groups.length} moedas`;
  box.innerHTML = groups.map(tradeGroup).join('');

  $$('.trade-row', box).forEach((row) => row.addEventListener('click', () => {
    const detail = $(`#${row.dataset.detail}`, box);
    detail.hidden = !detail.hidden;
    row.classList.toggle('is-open', !detail.hidden);
    $('td.expander', row).innerHTML = detail.hidden ? '&#9656;' : '&#9662;';
  }));
}

/* ---------------------------------------------------------------- settings */

/* The kill switch has to state what it is doing right now, not only what it is
   set to: "desligado" and "armado mas longe do limite" look identical otherwise. */
function renderRisk(risk) {
  const settings = risk.settings || {};
  const dd = risk.drawdown || {};
  $('#in-maxdd').value = settings.max_drawdown_pct ?? 0;
  $('#in-resumedd').value = settings.resume_drawdown_pct ?? 0;
  $('#in-maxcorr').value = settings.max_correlation ?? 0;
  $('#in-volsize').checked = Boolean(settings.volatility_sizing);

  const chip = $('#risk-state');
  const active = dd.enabled || settings.volatility_sizing || settings.max_correlation > 0;
  chip.textContent = dd.halted ? 'entradas bloqueadas' : (active ? 'ativo' : 'desligado');
  chip.className = `chip ${dd.halted ? 'bad' : (active ? 'ok' : '')}`;

  const parts = [];
  if (dd.enabled) {
    parts.push(`Queda atual ${num(dd.drawdown_pct)}% do topo de ${num(dd.peak)} USDT`
      + ` — limite ${num(dd.limit_pct)}%, volta a operar em ${num(dd.resume_pct)}%.`);
  }
  const pairs = risk.correlations || [];
  if (pairs.length) {
    const worst = pairs[0];
    parts.push(`Par mais correlacionado em carteira: ${esc(worst.a)} e ${esc(worst.b)},`
      + ` ${worst.correlation}.`);
  }
  $('#risk-detail').textContent = parts.join(' ');
}

async function loadSettings() {
  const [config, catalog, risk] = await Promise.all([
    api('/bot/config'), api('/strategies'), api('/risk')]);
  renderRisk(risk);
  $('#in-mode').value = config.mode;
  $('#in-poll').value = config.poll_seconds;
  $('#in-quote').value = config.quote_per_trade;
  $('#in-maxpos').value = config.max_positions;
  $('#in-capital').value = config.start_capital;

  const list = config.allocations || [];
  $('#allocations-empty').hidden = list.length > 0;
  $('#allocations-list').innerHTML = list.map((a, index) => `
    <div class="alloc">
      <div class="alloc-main">
        <strong>${a.symbol} · ${a.label || a.strategy}</strong>
        <span>${a.interval} · ${paramText(a.params || {})} · ${riskText(a.risk || {})}</span>
      </div>
      <button class="btn btn-small btn-danger" data-drop="${index}">Remover</button>
    </div>`).join('');

  $$('[data-drop]').forEach((button) => button.addEventListener('click', async () => {
    const next = list.filter((_, i) => i !== Number(button.dataset.drop));
    await api('/bot/allocations', { method: 'POST', body: { allocations: next } });
    toast('Estratégia removida');
    loadSettings();
  }));

  $('#catalog-list').innerHTML = catalog.map((c) => `
    <div class="catalog-card">
      <strong>${c.label}</strong>
      <span class="family">${c.family} · ${c.grid_size} combinações</span>
      <p>${c.description}</p>
    </div>`).join('');
}

/* -------------------------------------------------------------- validation */

/* A verdict is only useful if the reader can see what it was based on, so each
   card carries its own per-window table rather than a single summary number. */
const REGIME_TONE = { bull: 'pos', bear: 'neg', chop: 'muted' };

function windowRow(w) {
  const period = `${w.test_start.slice(0, 10)} a ${w.test_end.slice(0, 10)}`;
  return `<tr>
    <td class="mono">${period}</td>
    <td class="${REGIME_TONE[w.regime] || ''}">${esc(w.regime_label || '—')}</td>
    <td class="num ${cls(w.return_pct)}">${num(w.return_pct)}%</td>
    <td class="num muted">${num(w.buy_hold_pct)}%</td>
    <td class="num">${num(w.sharpe)}</td>
    <td class="num neg">${num(w.max_drawdown_pct)}%</td>
    <td class="num">${w.trades}</td>
  </tr>`;
}

function validationCard(report, index) {
  const tone = report.passes ? 'ok' : 'bad';
  const windows = (report.windows || []).map(windowRow).join('');
  const regimes = (report.regimes || []).map((r) => `
    <span class="legend-item"><b class="${REGIME_TONE[r.regime] || ''}">${esc(r.label)}</b>
      ${r.profitable_windows}/${r.windows} no lucro,
      mediana ${num(r.median_return_pct)}%</span>`).join('');
  const detail = windows ? `<table class="compact">
      <thead><tr><th>Trimestre</th><th>Regime</th><th class="num">Retorno</th>
      <th class="num">Comprar e segurar</th>
      <th class="num">Sharpe</th><th class="num">Queda máx.</th><th class="num">Ops.</th></tr></thead>
      <tbody>${windows}</tbody></table>
      ${regimes ? `<div class="legend">${regimes}</div>` : ''}`
    : '<p class="muted">Sem janelas suficientes.</p>';

  return `<div class="vcard">
    <div class="vcard-head" data-vtoggle="${index}">
      <div>
        <strong>${esc(report.symbol)}</strong>
        <span class="muted">${esc(report.interval)} · ${esc(report.label || report.strategy)}</span>
      </div>
      <span class="chip ${tone}">${esc(report.verdict)}</span>
      <span class="expander" id="vexp-${index}">▾</span>
    </div>
    <div class="vcard-stats">
      <div><span class="muted">Trimestres no lucro</span><strong>${report.profitable_pct ?? 0}%</strong></div>
      <div><span class="muted">Bateu comprar e segurar</span><strong>${report.beat_buy_hold_pct ?? 0}%</strong></div>
      <div><span class="muted">Composto</span><strong class="${cls(report.compounded_return_pct)}">${num(report.compounded_return_pct)}%</strong></div>
      <div><span class="muted">Mediana</span><strong class="${cls(report.median_return_pct)}">${num(report.median_return_pct)}%</strong></div>
      <div><span class="muted">Pior trimestre</span><strong class="neg">${num(report.worst_window_pct)}%</strong></div>
      <div><span class="muted">Operações</span><strong>${report.total_trades ?? 0}</strong></div>
    </div>
    <div class="vcard-detail" id="vdet-${index}" hidden>${detail}</div>
  </div>`;
}

/* Coverage is reported per timeframe rather than as one number because a
   missed daily close costs six times what a missed 4h close costs, and one
   average would hide which of the two is actually being lost. */
async function loadCoverage() {
  const data = await api('/coverage');
  setText('#coverage-summary', data.since
    ? `${nf(data.coverage_pct, 1)}% dos fechamentos · ligado ${nf(data.uptime_pct, 1)}% do tempo`
    : 'nenhum ciclo registrado ainda');
  // Fechamentos perdidos nunca expiram, então a conta é cumulativa: um marco
  // move o início da contagem quando a hospedagem muda. O que ficou de fora
  // continua escrito aqui — uma porcentagem que esconde metade da própria
  // história é pior que a porcentagem incômoda que ela substituiu.
  const note = $('#coverage-note');
  if (data.excluded) {
    note.hidden = false;
    note.innerHTML = `Contagem reiniciada em <b>${dt(data.baseline)}</b>:`
      + ` ${plural(data.excluded.missed, 'fechamento perdido', 'fechamentos perdidos')}`
      + ` de ${data.excluded.closes} antes dessa data ficaram fora da conta,`
      + ` sob a hospedagem anterior.`;
  } else if (note) {
    note.hidden = true;
  }
  $('#coverage-table tbody').innerHTML = (data.intervals || []).map((row) => `
    <tr>
      <td class="mono">${row.interval}</td>
      <td class="num">${row.closes}</td>
      <td class="num">${row.covered}</td>
      <td class="num ${row.missed ? 'neg' : ''}">${row.missed}</td>
      <td class="num ${row.coverage_pct >= 90 ? 'pos' : 'neg'}">${nf(row.coverage_pct, 1)}%</td>
      <td class="num">${row.median_delay_minutes === null ? '—'
        : `${nf(row.median_delay_minutes, 0)} min`}</td>
    </tr>`).join('');
}

/* Each row is one live trade against its own backtest twin. The comparison is
   pairwise on purpose: pooling live results into an average would need dozens
   of trades to say anything, while a twin comparison catches a timing or
   pricing defect on the first one. */
async function loadParity() {
  const { trades, totals } = await api('/parity?limit=50');
  const aside = totals.unscored
    ? ` · ${plural(totals.unscored, 'operação anterior', 'operações anteriores')}`
      + ' à guarda, fora da conta'
    : '';
  setText('#parity-summary', (totals.evaluated
    ? `${totals.matched} de ${totals.evaluated} conferem`
      + (totals.median_entry_slippage_bps === null ? ''
        : ` · escorregamento mediano ${nf(totals.median_entry_slippage_bps, 0)} bps`
          + ` (tolerância ${nf(totals.tolerance_bps, 0)})`)
    : 'nenhuma operação pontuada ainda') + aside);
  $('#parity-empty').hidden = trades.length > 0;
  $('#parity-table').hidden = trades.length === 0;
  $('#parity-table tbody').innerHTML = trades.map((row) => {
    const good = row.verdict === 'igual ao modelo';
    // A trade the engine is no longer judged on is grey, not red: it is history,
    // not a failing check.
    const mark = row.scored === false ? '' : (good ? 'ok' : 'bad');
    const slip = row.entry_slippage_bps;
    return `
    <tr>
      <td class="mono">${row.symbol} <span class="muted">${row.interval || ''}</span></td>
      <td>${dt(row.entry_time)}${row.entry_bars_late
        ? ` <span class="muted">(${plural(row.entry_bars_late, 'vela', 'velas')} depois)</span>` : ''}</td>
      <td class="num mono">${row.actual_entry_price === undefined ? '—' : nf(row.actual_entry_price, 4)}</td>
      <td class="num mono">${row.expected_entry_price === undefined || row.expected_entry_price === null
        ? '—' : nf(row.expected_entry_price, 4)}</td>
      <td class="num ${slip === undefined ? '' : cls(-slip)}">${slip === undefined
        ? '—' : `${signed(slip, 0)} bps`}</td>
      <td class="num ${cls(row.actual_return_pct)}">${row.actual_return_pct === null
        ? '<span class="muted">aberta</span>' : pct(row.actual_return_pct)}</td>
      <td class="num ${cls(row.expected_return_pct)}">${row.expected_return_pct === undefined
        ? '—' : pct(row.expected_return_pct)}</td>
      <td><span class="chip ${mark}">${row.verdict}</span></td>
    </tr>`;
  }).join('');
}

/* The go-live checklist. Deliberately a list of gates and not a score: a score
   averages away the one missing thing, and the one missing thing is exactly
   what the operator needs to know before risking real money. */
async function loadReadiness() {
  const data = await api('/readiness');
  const verdict = $('#readiness-verdict');
  verdict.textContent = data.ready ? 'sim, com ressalvas' : 'ainda não';
  verdict.className = `chip ${data.ready ? 'ok' : 'bad'}`;

  $('#readiness-gates').innerHTML = data.gates.map((gate) => `
    <div class="gate ${gate.ok ? 'ok' : ''}">
      <span class="gate-mark">${gate.ok ? '✓' : '○'}</span>
      <div class="gate-text">
        <span class="gate-label">${gate.label}</span>
        <span class="gate-detail">${gate.detail}</span>
      </div>
      ${gate.progress === undefined ? '' : `
        <div class="gate-bar"><div style="width:${Math.round(gate.progress * 100)}%"></div></div>`}
    </div>`).join('');

  const na = (value, suffix = '') => (value === null || value === undefined
    ? '<span class="muted">calculando…</span>' : `${value}${suffix}`);
  $('#readiness-expect').innerHTML = `
    <div class="expect-col">
      <h3>Esperado pelo teste histórico</h3>
      <div class="expect-row"><span>Operações por mês</span>
        <strong>${na(data.expected_trades_per_month)}</strong></div>
      <div class="expect-row"><span>Resultado mensal</span>
        <strong class="${cls(data.expected_return_pct_month)}">
          ${data.expected_return_pct_month === null ? '—' : pct(data.expected_return_pct_month)}</strong></div>
      <div class="expect-row"><span>Pior trimestre</span>
        <strong class="neg">${data.expected_worst_quarter_pct === null ? '—'
          : pct(data.expected_worst_quarter_pct)}</strong></div>
      <div class="expect-row"><span>Capital exposto</span>
        <strong>${money(data.deployed)} <span class="muted">de ${money(data.start_capital, 0)}</span></strong></div>
    </div>
    <div class="expect-col">
      <h3>Obtido ao vivo (${data.mode === 'live' ? 'conta real' : 'testnet'})</h3>
      <div class="expect-row"><span>Dias rodando</span><strong>${nf(data.days_live, 1)}</strong></div>
      <div class="expect-row"><span>Operações encerradas</span>
        <strong>${data.closed_trades}</strong></div>
      <div class="expect-row"><span>Resultado realizado</span>
        <strong class="${cls(data.realised_pnl)}">${signed(data.realised_pnl)}</strong></div>
      <div class="expect-row"><span>Rebaixamento observado</span>
        <strong class="${cls(data.observed_drawdown_pct)}">${nf(data.observed_drawdown_pct, 2)}%</strong></div>
    </div>`;
}

/* The realised curve against the band that was written down before it existed.
   Two separate honesty devices are at work here: the expectation is frozen at
   deployment (a recomputed one would already contain the period it is judging),
   and the band widens with the square root of elapsed time rather than
   linearly, so a fortnight is not asked to land inside a quarterly tolerance. */
async function loadTracking() {
  const data = await api('/tracking');
  const points = data.points || [];
  const chip = $('#tracking-verdict');
  const now = data.current;

  $('#tracking-empty').hidden = points.length > 1;
  $('#tracking-empty').textContent = data.status === 'ok'
    ? 'Ainda sem pontos suficientes para traçar.' : `${data.status}.`;

  if (!now || points.length < 2) {
    chip.textContent = '—';
    chip.className = 'chip';
    $('#tracking-legend').innerHTML = '';
    $('#tracking-now').innerHTML = '';
    return;
  }

  const below = now.realised_pct < now.lower_pct;
  chip.textContent = now.verdict;
  chip.className = `chip ${below ? 'bad' : (now.inside_band ? 'ok' : '')}`;

  const live = points.filter((p) => p.realised_pct !== null);
  drawChart($('#tracking-chart'), [
    {
      points: points.map((p) => ({ t: p.time, y: p.upper_pct })),
      bandTo: points.map((p) => p.lower_pct),
      bandColor: 'rgba(91,124,250,0.13)',
    },
    {
      points: points.map((p) => ({ t: p.time, y: p.expected_pct })),
      color: 'rgba(139,148,178,0.9)', width: 1.5, dash: [5, 4], fill: false,
    },
    {
      points: live.map((p) => ({ t: p.time, y: p.realised_pct })),
      color: below ? '#f87171' : '#5b7cfa', width: 2, fill: false,
    },
  ], { fill: false, format: (value) => pct(value) });

  $('#tracking-legend').innerHTML = `
    <span class="legend-item"><i class="legend-swatch" style="border-top-color:${
      below ? '#f87171' : '#5b7cfa'}"></i>realizado</span>
    <span class="legend-item"><i class="legend-swatch" style="border-top-color:rgba(139,148,178,0.9);border-top-style:dashed"></i>trimestre mediano previsto</span>
    <span class="legend-item"><i class="legend-swatch is-band" style="background:rgba(91,124,250,0.28)"></i>faixa até o pior trimestre previsto</span>`;

  const base = (data.baselines || [])[data.baselines.length - 1] || {};
  $('#tracking-now').innerHTML = `
    <div class="expect-col">
      <h3>Previsto quando o livro entrou</h3>
      <div class="expect-row"><span>Congelado em</span>
        <strong>${dt(base.recorded_at, false)}</strong></div>
      <div class="expect-row"><span>Retorno mensal</span>
        <strong class="${cls(base.return_pct_month)}">${pct(base.return_pct_month || 0)}</strong></div>
      <div class="expect-row"><span>Pior trimestre</span>
        <strong class="neg">${pct(base.worst_quarter_pct || 0)}</strong></div>
      <div class="expect-row"><span>Revisões do livro</span>
        <strong>${data.segments}</strong></div>
    </div>
    <div class="expect-col">
      <h3>Onde está hoje</h3>
      <div class="expect-row"><span>Realizado</span>
        <strong class="${cls(now.realised_pct)}">${pct(now.realised_pct)}</strong></div>
      <div class="expect-row"><span>Previsto para ${nf(now.days_live, 0)} dias</span>
        <strong>${pct(now.expected_pct)}</strong></div>
      <div class="expect-row"><span>Faixa hoje</span>
        <strong>${pct(now.lower_pct)} a ${pct(now.upper_pct)}</strong></div>
      <div class="expect-row"><span>Divergência</span>
        <strong class="${cls(now.divergence_pct)}">${pct(now.divergence_pct)}</strong></div>
    </div>`;
}

/* The book pooled by market condition. Shown next to the per-allocation slice
   because the two answer different questions at different sample sizes: eight
   windows cannot separate three buckets, and 136 can. */
function renderRegimes(data) {
  const rows = data.rows || [];
  const chip = $('#regime-verdict');
  chip.textContent = rows.length ? data.verdict : '—';
  chip.className = `chip ${/^ganha/.test(data.verdict || '') ? 'ok'
    : (/^n[aã]o ganha/.test(data.verdict || '') ? 'bad' : '')}`;
  $('#regime-empty').hidden = rows.length > 0;
  $('#regime-table').hidden = rows.length === 0;
  $('#regime-table tbody').innerHTML = rows.map((r) => `
    <tr>
      <td><b class="${REGIME_TONE[r.regime] || ''}">${esc(r.label)}</b></td>
      <td class="num">${r.windows}</td>
      <td class="num muted">${nf(r.share_pct, 0)}%</td>
      <td class="num ${r.profitable_pct >= 50 ? 'pos' : 'neg'}">${nf(r.profitable_pct, 0)}%</td>
      <td class="num ${cls(r.median_return_pct)}">${pct(r.median_return_pct)}</td>
      <td class="num ${cls(r.median_alpha_pct)}">${pct(r.median_alpha_pct)}</td>
      <td class="num neg">${pct(r.worst_pct)}</td>
      <td class="num muted">${pct(r.median_buy_hold_pct)}</td>
      <td class="num">${r.trades}</td>
    </tr>`).join('');
}

async function loadValidation(refresh = false) {
  const state_ = await api(`/validation${refresh ? '?refresh=true' : ''}`);
  const label = { idle: 'nunca calculado', running: 'calculando…', done: '', error: 'erro' };
  $('#validation-state').textContent = state_.status === 'done'
    ? `atualizado ${new Date(state_.checked_at * 1000).toLocaleString('pt-BR')}`
    : (label[state_.status] || state_.status);

  const reports = state_.reports || [];
  $('#validation-empty').hidden = reports.length > 0 || state_.status === 'running';
  $('#validation-empty').textContent = state_.status === 'running'
    ? 'Calculando — cada estratégia percorre três anos de histórico.'
    : 'Nenhuma estratégia em operação para validar.';
  $('#validation-cards').innerHTML = reports.map(validationCard).join('');
  renderRegimes(state_.regimes || { rows: [], verdict: '' });

  $$('[data-vtoggle]').forEach((head) => head.addEventListener('click', () => {
    const detail = $(`#vdet-${head.dataset.vtoggle}`);
    detail.hidden = !detail.hidden;
    $(`#vexp-${head.dataset.vtoggle}`).textContent = detail.hidden ? '▾' : '▴';
  }));

  /* The first request only kicks the background thread off. */
  if (state_.status === 'running') setTimeout(() => {
    if (state.view === 'validation') loadValidation();
  }, 4000);
}

/* ------------------------------------------------------------------ router */

function switchView(view) {
  state.view = view;
  $$('.nav-item').forEach((item) => item.classList.toggle('active', item.dataset.view === view));
  $$('.view').forEach((section) => section.classList.toggle('active', section.id === `view-${view}`));
  const [title, subtitle] = VIEW_META[view];
  $('#view-title').textContent = title;
  $('#view-subtitle').textContent = subtitle;
  refresh();
}

async function refresh() {
  try {
    await loadStatus();
    if (state.view === 'dashboard') await loadDashboard();
    else if (state.view === 'lab') await loadLab();
    else if (state.view === 'trades') { await loadParity(); await loadLedger(); await loadTrades(); }
    else if (state.view === 'validation') {
      await loadReadiness(); await loadTracking();
      await loadCoverage(); await loadValidation();
    }
    else if (state.view === 'settings') await loadSettings();
  } catch (error) {
    toast(error.message, 'error');
  }
}

/* ------------------------------------------------------------------- wire */

$$('.nav-item').forEach((item) =>
  item.addEventListener('click', () => switchView(item.dataset.view)));

$('#btn-save-risk').addEventListener('click', async () => {
  try {
    renderRisk(await api('/risk', { method: 'POST', body: {
      max_drawdown_pct: Number($('#in-maxdd').value),
      resume_drawdown_pct: Number($('#in-resumedd').value),
      max_correlation: Number($('#in-maxcorr').value),
      volatility_sizing: $('#in-volsize').checked,
    } }));
    toast('Controles de risco salvos');
  } catch (error) {
    toast(error.message, 'error');
  }
});

$('#btn-validate').addEventListener('click', () => {
  $('#validation-state').textContent = 'calculando…';
  loadValidation(true).catch((error) => toast(error.message, 'error'));
});

$('#btn-refresh').addEventListener('click', (event) => {
  event.currentTarget.querySelector('svg').classList.add('spin');
  refresh().finally(() =>
    setTimeout(() => event.currentTarget.querySelector('svg').classList.remove('spin'), 400));
});

$('#btn-toggle-bot').addEventListener('click', async () => {
  const running = state.status?.bot?.running;
  try {
    const result = await api(running ? '/bot/stop' : '/bot/start', { method: 'POST' });
    toast(result.message === 'no strategies allocated'
      ? 'Nenhuma estratégia alocada — escolha no Laboratório'
      : (running ? 'Robô parado' : 'Robô ligado'), result.running || !running ? 'ok' : 'error');
    refresh();
  } catch (error) { toast(error.message, 'error'); }
});

$('#btn-close-all').addEventListener('click', async () => {
  const book = state.book === 'ml' ? 'do laboratório' : 'do livro validado';
  if (!confirm(`Encerrar todas as posições abertas ${book} a mercado?`)) return;
  try {
    const result = await api(
      state.book === 'ml' ? '/lab/close-all' : '/bot/close-all', { method: 'POST' });
    toast(`${result.closed.length} posição(ões) encerrada(s)`, 'ok');
    refresh();
  } catch (error) { toast(error.message, 'error'); }
});

$('#btn-research').addEventListener('click', async () => {
  const symbols = $('#in-symbols').value.split(',').map((s) => s.trim().toUpperCase()).filter(Boolean);
  const intervals = $('#in-intervals').value.split(',').map((s) => s.trim()).filter(Boolean);
  const candles = Number($('#in-candles').value);
  try {
    const result = await api('/research/start', { method: 'POST', body: { symbols, intervals, candles } });
    if (!result.started) { toast(`Já existe uma pesquisa rodando (#${result.run_id})`, 'error'); }
    else toast('Pesquisa iniciada');
    state.selected.clear();
    watchResearch();
    renderResearchProgress(await api('/research/status'));
  } catch (error) { toast(error.message, 'error'); }
});

$('#chk-validated').addEventListener('change', loadLab);

$$('#book-toggle .seg-btn').forEach((button) => button.addEventListener('click', () => {
  if (state.book === button.dataset.book) return;
  state.book = button.dataset.book;
  loadDashboard().catch((error) => toast(error.message, 'error'));
}));

$('#btn-exit-toggle').addEventListener('click', async () => {
  const running = state.exit?.running;
  try {
    const result = await api(running ? '/mirror/stop' : '/mirror/start', { method: 'POST' });
    toast(result.running ? 'Estudo de saída ligado' : 'Estudo de saída parado', 'ok');
    await loadDashboard();
  } catch (error) { toast(error.message, 'error'); }
});

$('#btn-exit-tick').addEventListener('click', async () => {
  try {
    const result = await api('/mirror/tick', { method: 'POST' });
    toast(`${result.actions.length} movimento(s)`, 'ok');
    await loadDashboard();
  } catch (error) { toast(error.message, 'error'); }
});

$('#btn-lab-toggle').addEventListener('click', async () => {
  const running = state.lab?.status?.running;
  try {
    const result = await api(running ? '/lab/stop' : '/lab/start', { method: 'POST' });
    if (!running && !result.running) toast('Treine um modelo antes de ligar', 'error');
    else toast(result.running ? 'Laboratório ligado' : 'Laboratório parado', 'ok');
    refresh();
  } catch (error) { toast(error.message, 'error'); }
});

$('#btn-lab-tick').addEventListener('click', async () => {
  try {
    const result = await api('/lab/tick?force=true', { method: 'POST' });
    toast(result.skipped
      ? `Ciclo pulado: ${result.skipped}`
      : `Ciclo executado: ${(result.actions || []).length} ação(ões)`, 'ok');
    refresh();
  } catch (error) { toast(error.message, 'error'); }
});

/* Training takes about a minute, so the button starts it and then watches. */
$('#btn-lab-train').addEventListener('click', async () => {
  try {
    await api('/lab/train', { method: 'POST' });
    toast('Treinando — leva cerca de um minuto');
    const watch = setInterval(async () => {
      const status = await api('/lab/train/status');
      if (status.running) return;
      clearInterval(watch);
      if (status.error) toast(status.error, 'error');
      else if (status.result?.error) toast(status.result.error, 'error');
      else if (status.result) toast(`Modelo ${status.result.model_id} treinado`, 'ok');
      refresh();
    }, 4000);
  } catch (error) { toast(error.message, 'error'); }
});

$$('#breakdown-toggle .seg-btn').forEach((button) => button.addEventListener('click', () => {
  $$('#breakdown-toggle .seg-btn').forEach((other) => other.classList.toggle('is-on', other === button));
  state.breakdownGroup = button.dataset.group;
  if (state.breakdown) renderBreakdown(state.breakdown[state.breakdownGroup]);
}));

$$('#trades-mode .seg-btn').forEach((button) => button.addEventListener('click', () => {
  $$('#trades-mode .seg-btn').forEach((other) => other.classList.toggle('is-on', other === button));
  state.tradesMode = button.dataset.mode;
  loadTrades();
}));

$('#btn-allocate').addEventListener('click', async () => {
  if (!state.selected.size) { toast('Marque ao menos uma estratégia na tabela', 'error'); return; }
  try {
    const config = await api('/bot/allocations', {
      method: 'POST', body: { result_ids: [...state.selected] },
    });
    toast(`${config.allocations.length} estratégia(s) prontas para operar`, 'ok');
    state.selected.clear();
    loadLab();
  } catch (error) { toast(error.message, 'error'); }
});

$('#btn-close-detail').addEventListener('click', () => { $('#detail-panel').hidden = true; });

$('#btn-save-config').addEventListener('click', async () => {
  try {
    await api('/bot/config', {
      method: 'POST',
      body: {
        mode: $('#in-mode').value,
        poll_seconds: Number($('#in-poll').value),
        quote_per_trade: Number($('#in-quote').value),
        max_positions: Number($('#in-maxpos').value),
        start_capital: Number($('#in-capital').value),
      },
    });
    toast('Ajustes salvos', 'ok');
    refresh();
  } catch (error) { toast(error.message, 'error'); }
});

$('#btn-tick').addEventListener('click', async () => {
  try {
    const result = await api('/bot/tick', { method: 'POST' });
    toast(`Ciclo executado: ${result.actions.length} ação(ões) em ${result.checked} estratégia(s)`, 'ok');
    refresh();
  } catch (error) { toast(error.message, 'error'); }
});

$('#btn-reset').addEventListener('click', async () => {
  if (!confirm('Apagar todo o histórico de operações, ordens e patrimônio?')) return;
  await api('/bot/reset', { method: 'POST' });
  toast('Histórico zerado', 'ok');
  refresh();
});

window.addEventListener('resize', () => {
  if (state.view === 'dashboard' && state.equity.length > 1 && state.overview) {
    renderEquity(state.equity, state.overview);
  }
});

refresh();
setInterval(() => { if (!document.hidden) refresh(); }, 15000);
