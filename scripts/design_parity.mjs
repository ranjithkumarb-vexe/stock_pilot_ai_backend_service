// Regenerates tests/trading/golden/design_parity.json by running the design's OWN logic (calc / check / pq /
// propInfo copied verbatim from `StockPilot AI.dc.html`, class Component) on `design/sp-data.js` personas.
//   node scripts/design_parity.mjs /path/to/stock_pilot_ai_web_app/design > tests/trading/golden/design_parity.json
import fs from 'node:fs';
import vm from 'node:vm';
import path from 'node:path';

const dir = process.argv[2];
const win = {};
vm.runInNewContext(fs.readFileSync(path.join(dir, 'sp-data.js'), 'utf8'), { window: win });
const D = win.SPDATA;

const state = { pd: {}, open: true, fail: { offline: false }, feed: 'live' };
for (const k of ['priya', 'alex']) {
  const b = D.PERSONAS[k];
  state.pd[k] = {
    cash: b.cash, rules: { ...b.rules },
    hold: b.hold.map(h => ({ t: h[0], q: h[1], c: h[2] })),
    orders: b.orders.map(o => ({ d: o[0], tk: o[1], side: o[2], q: o[3], px: o[4], status: 'filled' })),
  };
}
const C = {
  state, D,
  calc(pid) {
    const p = this.state.pd[pid], U = this.D.U;
    const pos = p.hold.map(h => { const u = U[h.t], v = h.q * u.px; return { t: h.t, n: u.n, s: u.s, q: h.q, px: u.px, ch: u.ch, v, c: h.c, pnl: v - h.q * h.c, pp: (u.px / h.c - 1) * 100 }; });
    const inv = pos.reduce((a, x) => a + x.v, 0), T = inv + p.cash;
    pos.forEach(x => (x.w = x.v / T * 100));
    const sm = {}; pos.forEach(x => (sm[x.s] = (sm[x.s] || 0) + x.v));
    const sec = Object.keys(sm).map(s => ({ s, v: sm[s], w: sm[s] / T * 100 })).sort((a, b) => b.w - a.w);
    const hhi = inv ? pos.reduce((a, x) => a + Math.pow(x.v / inv, 2), 0) : 0;
    const day = pos.reduce((a, x) => a + x.v * x.ch / (100 + x.ch), 0);
    return { pos, inv, T, sec, hhi, eff: hhi ? 1 / hhi : 0, day, dayPct: T ? day / T * 100 : 0 };
  },
  check(pid, t, side, q) {
    const p = this.state.pd[pid], c = this.calc(pid), u = this.D.U[t], r = p.rules, cur = c.pos.find(x => x.t === t), val = q * u.px, rows = []; let blocked = false;
    const add = (ok, l, d, soft) => { rows.push({ l, d, mark: ok ? 'Pass' : (soft ? 'Note' : 'Blocked') }); if (!ok && !soft) blocked = true; };
    if (!(q >= 1)) add(false, 'Quantity', 'Enter at least 1 share');
    if (side === 'sell') { const held = cur ? cur.q : 0; add(q <= held, 'No shorting (MVP)', q <= held ? 'Selling ' + q + ' of ' + held + ' shares held' : 'You hold ' + held + ' shares and cannot sell ' + q); }
    else {
      add(val <= p.cash, 'Paper cash', 'Cost $' + Math.round(val).toLocaleString() + ' of $' + Math.round(p.cash).toLocaleString() + ' available');
      const nw = ((cur ? cur.v : 0) + val) / c.T * 100; add(nw <= r.pos, 'Max position ' + r.pos + '%', t + ' would be ' + nw.toFixed(1) + '%');
      const sv = ((c.sec.find(x => x.s === u.s) || { v: 0 }).v + val) / c.T * 100; add(sv <= r.sec, 'Max sector ' + r.sec + '%', u.s + ' would be ' + sv.toFixed(1) + '%');
      const buf = r.cash || 0; if (buf > 0) { const ca = (p.cash - val) / c.T * 100; add(ca >= buf, 'Min cash buffer ' + buf + '%', 'Cash after would be ' + ca.toFixed(1) + '%'); }
    }
    const wk = p.orders.filter(o => o.d <= 7 && o.status !== 'cancelled').length; add(wk < r.trades, 'Trades per week ' + r.trades, wk + ' in the last 7 days', side === 'sell');
    return { rows, blocked };
  },
  pq(pid, pr) {
    const c = this.calc(pid), p = this.state.pd[pid], cur = c.pos.find(x => x.t === pr.tk); if (!cur || pr.kind !== 'trade') return 0;
    let ex = 0;
    if (pr.rule === 'pos') ex = cur.v - p.rules.pos / 100 * c.T;
    else { const s = c.sec.find(x => x.s === cur.s); ex = s ? s.v - p.rules.sec / 100 * c.T : 0; }
    return Math.max(0, Math.min(cur.q, Math.ceil(ex / cur.px)));
  },
  propInfo(pid, pr) {
    const c = this.calc(pid), p = this.state.pd[pid], cur = c.pos.find(x => x.t === pr.tk); const q = this.pq(pid, pr);
    if (pr.kind !== 'trade' || !cur) return { q: 0 };
    const px = cur.px; let w, aw, cap, name;
    if (pr.rule === 'pos') { w = cur.w; aw = (cur.v - q * px) / c.T * 100; cap = p.rules.pos; name = pr.tk; }
    else { const s = c.sec.find(x => x.s === cur.s); w = s.w; aw = (s.v - q * px) / c.T * 100; cap = p.rules.sec; name = cur.s; }
    return { q, w, aw, cap, name };
  },
};

const out = { personas: {}, checks: [] };
for (const k of ['priya', 'alex']) {
  const c = C.calc(k);
  out.personas[k] = {
    inv: c.inv, T: c.T, hhi: c.hhi, eff: c.eff, day: c.day, dayPct: c.dayPct,
    pos: c.pos.map(x => ({ t: x.t, q: x.q, px: x.px, v: x.v, c: x.c, pnl: x.pnl, pp: x.pp, w: x.w, ch: x.ch, s: x.s })),
    sec: c.sec,
    proposals: D.PERSONAS[k].proposals.filter(p => p.kind === 'trade').map(p => ({ tk: p.tk, rule: p.rule, ...C.propInfo(k, { ...p }) })),
  };
}
const scenarios = [
  ['priya', 'JNJ', 'buy', 10], ['priya', 'KO', 'sell', 3], ['priya', 'AAPL', 'sell', 25], ['priya', 'MSFT', 'buy', 2], ['priya', 'PG', 'buy', 100],
  ['priya', 'XOM', 'buy', 0], ['priya', 'GOOGL', 'buy', 5], ['priya', 'PEP', 'buy', 1000],
  ['alex', 'NVDA', 'buy', 5], ['alex', 'TSLA', 'sell', 10], ['alex', 'AMD', 'buy', 20], ['alex', 'AAPL', 'buy', 30], ['alex', 'META', 'buy', 100], ['alex', 'AVGO', 'sell', 40],
];
for (const [pid, t, side, q] of scenarios) out.checks.push({ pid, symbol: t, side, qty: q, ...C.check(pid, t, side, q) });
process.stdout.write(JSON.stringify(out, null, 1) + '\n');
