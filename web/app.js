/* Trader Bot dashboard ---------------------------------------------------- */

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
};

const VIEW_META = {
  dashboard: ['Painel', 'Resultado consolidado das estratégias em operação'],
  lab: ['Laboratório', 'Otimiza no histórico antigo e valida no que ficou de fora'],
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

function drawChart(canvas, series, { fill = true, tipTarget = null } = {}) {
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

  const pad = { top: 14, right: 56, bottom: 22, left: 10 };
  const plotW = width - pad.left - pad.right;
  const plotH = height - pad.top - pad.bottom;

  let min = Math.min(...points.map((p) => p.y));
  let max = Math.max(...points.map((p) => p.y));
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
    ctx.fillText(nf(value, value > 1000 ? 0 : 2), pad.left + plotW + 8, y);
  }

  series.forEach((s) => {
    const len = s.points.length;
    if (len < 2) return;
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

  if (tipTarget) attachTip(canvas, tipTarget, series[0], { xAt, yAt, pad, plotH });
  return { xAt, yAt };
}

function attachTip(canvas, tip, series, geo) {
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
    tip.innerHTML = `<b>${money(point.y)}</b><span>${dt(point.t)}</span>`;
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
  const [overview, equity, breakdown, events] = await Promise.all([
    api('/overview'), api('/equity'), api('/breakdown'), api('/events?limit=30'),
  ]);
  state.overview = overview;
  state.equity = equity;

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
  setText('#kpi-sharpe', nf(overview.sharpe, 2));

  renderPnl(overview);
  renderEquity(equity, overview);
  renderPositions(overview.positions);
  state.breakdown = breakdown;
  renderBreakdown(breakdown[state.breakdownGroup || 'by_strategy']);
  renderEvents(events);
}

/* The waterfall exists because a single "resultado total" number hides the two
   things that make it, and they are not the same kind of money: one is banked
   and one can still evaporate. Reading it top to bottom gives the whole
   arithmetic - what was put in, what closed trades did to it, what open trades
   are currently doing to it, and what is left. */
function renderPnl(overview) {
  setText('#pnl-mode', overview.mode === 'live'
    ? 'CONTA REAL — dinheiro de verdade'
    : 'conta de teste (testnet) — dinheiro fictício');
  $('#pnl-mode').className = overview.mode === 'live' ? 'neg' : 'muted';

  const rows = [
    { label: 'Capital inicial', sub: 'ponto de partida',
      value: money(overview.start_capital), tone: '' },
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
       Taxas estimadas até agora: ${money(overview.fees_estimate)} sobre
       ${money(overview.turnover)} negociados.</p>`;
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
      <td><span class="chip">${p.strategy}</span> <span class="muted">${p.interval}</span></td>
      <td class="num">${nf(p.entry_price, 4)}</td>
      <td class="num">${nf(p.mark_price, 4)}</td>
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

function renderEvents(events) {
  $('#events-list').innerHTML = events.length
    ? events.map((e) => `
      <li>
        <time>${dt(e.ts)}</time>
        <span class="level ${e.level}">${e.level}</span>
        <span>${e.message}</span>
      </li>`).join('')
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
      <td class="muted">${open ? 'em andamento' : esc(reason)}</td>
    </tr>
    <tr class="trade-detail" id="${key}" hidden>
      <td colspan="9">
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
                <th class="num">Resultado</th><th class="num">%</th><th>Motivo da saída</th></tr>
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
    ['Taxas estimadas', money(totals.fees_estimate), '0,1% por ordem'],
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
function windowRow(w) {
  const period = `${w.test_start.slice(0, 10)} a ${w.test_end.slice(0, 10)}`;
  return `<tr>
    <td class="mono">${period}</td>
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
  const detail = windows ? `<table class="compact">
      <thead><tr><th>Trimestre</th><th class="num">Retorno</th><th class="num">Comprar e segurar</th>
      <th class="num">Sharpe</th><th class="num">Queda máx.</th><th class="num">Ops.</th></tr></thead>
      <tbody>${windows}</tbody></table>` : '<p class="muted">Sem janelas suficientes.</p>';

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
  setText('#parity-summary', totals.evaluated
    ? `${totals.matched} de ${totals.evaluated} conferem`
      + (totals.median_entry_slippage_bps === null ? ''
        : ` · escorregamento mediano ${nf(totals.median_entry_slippage_bps, 0)} bps`
          + ` (tolerância ${nf(totals.tolerance_bps, 0)})`)
    : '—');
  $('#parity-empty').hidden = trades.length > 0;
  $('#parity-table').hidden = trades.length === 0;
  $('#parity-table tbody').innerHTML = trades.map((row) => {
    const good = row.verdict === 'igual ao modelo';
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
      <td><span class="chip ${good ? 'ok' : 'bad'}">${row.verdict}</span></td>
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
    else if (state.view === 'validation') { await loadReadiness(); await loadCoverage(); await loadValidation(); }
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
  if (!confirm('Encerrar todas as posições abertas a mercado?')) return;
  try {
    const result = await api('/bot/close-all', { method: 'POST' });
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
