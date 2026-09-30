/*!
 * CREST for SILVER — Catch the Run, Exit on Structural Trend                  v3.0
 * Not financial advice. Research tool; it places no trades and connects to no account.
 * Long or cash, daily bars. Input rows: [{t: epoch ms UTC, o, h, l, c}] ascending.   Node 18+ or browser.
 *   const CREST = require('./crest_silver.js');  const r = CREST.run(rows);  CREST.tenX(r);
 * Built for: LBMA silver fix (USD) from 1968-01-02, then Yahoo silver futures (SI=F) from 2000-08-30; second source the SLV ETF; third the Hyperliquid xyz:SILVER perp.
 * Settings: Full-history fit (1968-01-02 to 2026-09-26, 4360 blind candidates and a hill-climb): most dollars passing the ±5% test and rule 6 (misses a crash); it beat buy & hold on all 50 altered histories (±5%, worst 3.932x).
 * Full-history fit (live where the walk-forward wins; passed the ±5% rule): $1,263,062 vs buy & hold $296,570 from $10,000 per ticker.
 * Walk-forward check (each year traded by a setting that never saw it): $829,431 vs buy & hold $296,570, never liquidated.
 *
 * 17 MARKET RULES
 *  1. Fit ............... fitted on the whole history (1968-01-02 to 2026-09-26): the most dollars among 4360 blind candidates and a hill-climb, accepted because it beat buy & hold, never liquidated, on all 50 altered histories (a slow wave and daily noise moving every price by up to ±5%; the worst still 3.932x buy & hold), every nudge beating buy & hold; misses 2 cycle crashes (crash rule). Walk-forward check (each year traded by a setting fitted only on the years before): $829,431 vs buy & hold $296,570.
 *  2. Start ............. buy at the first daily close of the data (new listings run hardest early)
 *  3. Trend line ........ 200-day exponential moving average of the close
 *  4. Crash depth ....... the deepest fall from the all-time high within the past 365 days; a "post-crash" market is one down 60% or more that is still within 2x of its one-year low
 *  5. Cycle entry ....... a close above the trend line AND above the highest close of the prior 20 days, while the trend line is higher than 50 days ago, and not within 60 days of a losing exit
 *  6. Generational entry  the first cycle entry in a post-crash market is labelled "generational" (at most one a year)
 *  7. Trend exit ........ 3 closes in a row below 80% of the trend line; sell at that close
 *  8. Generational sell . once a trade is up 2x, sell if price closes 50% below its highest close since entry — the run is over
 *  9. After a big run ... after a trade that reached 10x, buy again only above that run's highest close; at least 540 days between entries
 * 10. Capitulation ...... a down day at least 30% below the all-time high on volume above 3x the 50-day average, then within 10 days a close above the close before that day, buys (the panic was absorbed); held like a spring buy (not used without volume data)
 * 11. Volume climax ..... once a trade has doubled, sell everything on a down day with volume more than 5x the average of the previous 20 days — a blow-off top (not used without volume data)
 * 12. Cross-market ...... buy only while M2 is below its 20-day EMA (moves against it)
 * 13. Five a year ....... at most 5 entries per calendar year; when used up, wait in cash for 1 January
 * 14. Leverage .......... 2x at most once per calendar year, never (leverage off); the borrowed half stays on until the exit
 * 15. Sizing ............ position = min(equity, $10,000 x (1 + 0.5 x (equity/$10,000 - 1))): it grows at half the rate of the account; the rest earns 4% in cash
 * 16. Costs and liquidation  0.1% fee per side; borrowing costs 8% a year, charged per calendar day; liquidated if a day's low leaves equity under 10% of the position (a low more than 50% below both open and close is a bad print and is capped)
 * 17. Long or cash, no look-ahead  no shorts; every decision uses data up to that day’s close and fills at that close (take-profit and leverage-stop levels are fixed at entry)
 */
(function (root) {
  const VERSION = '3.0';
  // fixed by the brief: maxPerYear 5, lev 2 (at most once a year), slowFactor 0.5; costs: fee, borrowAPR, cashAPY, maintenance
  const SETTINGS = {
   "ticker": "SILVER",
   "start": 10000,
   "from": 0,
   "to": 0,
   "trendLen": 200,
   "breakLen": 20,
   "exitBuf": 0.2,
   "exitDays": 3,
   "dayOne": true,
   "levWhen": "none",
   "levExit": 0,
   "levStop": 0,
   "trail": 0.5,
   "trailArm": 2,
   "genLen": 0,
   "genDepth": 0.6,
   "slopeLen": 50,
   "cooldown": 60,
   "tp1": 0,
   "tp1Frac": 0.5,
   "tp2": 20,
   "tp2Frac": 1,
   "tp3": 0,
   "tp3Frac": 1,
   "stretch": 0,
   "stretchFrac": 1,
   "trail2": 0,
   "trailArm2": 10,
   "minGap": 540,
   "retire": 0,
   "athAfter": 10,
   "corrLen": 60,
   "corrThr": 0.2,
   "valSell": 0,
   "valMax": 0,
   "valLen": 1400,
   "momLen": 0,
   "volMax": 0,
   "volExit": 0,
   "volLen": 30,
   "hlLen": 0,
   "ddMax": 0,
   "vcMult": 0,
   "vcLen": 20,
   "obvLen": 0,
   "vxMult": 5,
   "vxLen": 20,
   "halvWin": 0,
   "halvSell": 0,
   "halvEnd": 1000,
   "seasonOff": 0,
   "seasonLen": 2,
   "holdOnly": false,
   "genBuy": 0,
   "genBuyLen": 50,
   "genTrail": 0,
   "gcBuy": 0,
   "gcLen": 50,
   "gcTrail": 0,
   "genWin": 365,
   "cbCycle": 0,
   "cbFrom": 0,
   "cbTo": 0,
   "rfxAcc": 0,
   "rfxStretch": 0,
   "rfxLen": 200,
   "rfxFast": 20,
   "sqzLen": 0,
   "divGap": 0,
   "divLen": 60,
   "paraX": 0,
   "paraLen": 60,
   "springLen": 0,
   "capMult": 3,
   "capDepth": 0.3,
   "ctxRef": "M2",
   "ctxMode": "down",
   "ctxLen": 20,
   "ctxRef2": null,
   "ctxMode2": "corr",
   "ctxLen2": 100,
   "ctxRef3": null,
   "ctxMode3": "corr",
   "ctxLen3": 100,
   "ctxRef4": null,
   "ctxMode4": "corr",
   "ctxLen4": 100,
   "ctxRef5": null,
   "ctxMode5": "corr",
   "ctxLen5": 100,
   "ctxRef6": null,
   "ctxMode6": "corr",
   "ctxLen6": 100,
   "ctxRef7": null,
   "ctxMode7": "corr",
   "ctxLen7": 100,
   "ctxRef8": null,
   "ctxMode8": "corr",
   "ctxLen8": 100,
   "ctxRef9": null,
   "ctxMode9": "corr",
   "ctxLen9": 100,
   "ctxRef10": null,
   "ctxMode10": "corr",
   "ctxLen10": 100,
   "ctxRef11": null,
   "ctxMode11": "corr",
   "ctxLen11": 100,
   "ctxRef12": null,
   "ctxMode12": "corr",
   "ctxLen12": 100,
   "ctxRef13": null,
   "ctxMode13": "corr",
   "ctxLen13": 100,
   "ctxRef14": null,
   "ctxMode14": "corr",
   "ctxLen14": 100,
   "ctxRef15": null,
   "ctxMode15": "corr",
   "ctxLen15": 100,
   "maxPerYear": 5,
   "lev": 2,
   "slowFactor": 0.5,
   "cashAPY": 0.04,
   "borrowAPR": 0.08,
   "fee": 0.001,
   "maintenance": 0.1
  };
  const RULES = [["Fit","fitted on the whole history (1968-01-02 to 2026-09-26): the most dollars among 4360 blind candidates and a hill-climb, accepted because it beat buy & hold, never liquidated, on all 50 altered histories (a slow wave and daily noise moving every price by up to ±5%; the worst still 3.932x buy & hold), every nudge beating buy & hold; misses 2 cycle crashes (crash rule). Walk-forward check (each year traded by a setting fitted only on the years before): $829,431 vs buy & hold $296,570."],["Start","buy at the first daily close of the data (new listings run hardest early)"],["Trend line","200-day exponential moving average of the close"],["Crash depth","the deepest fall from the all-time high within the past 365 days; a \"post-crash\" market is one down 60% or more that is still within 2x of its one-year low"],["Cycle entry","a close above the trend line AND above the highest close of the prior 20 days, while the trend line is higher than 50 days ago, and not within 60 days of a losing exit"],["Generational entry","the first cycle entry in a post-crash market is labelled \"generational\" (at most one a year)"],["Trend exit","3 closes in a row below 80% of the trend line; sell at that close"],["Generational sell","once a trade is up 2x, sell if price closes 50% below its highest close since entry — the run is over"],["After a big run","after a trade that reached 10x, buy again only above that run's highest close; at least 540 days between entries"],["Capitulation","a down day at least 30% below the all-time high on volume above 3x the 50-day average, then within 10 days a close above the close before that day, buys (the panic was absorbed); held like a spring buy (not used without volume data)"],["Volume climax","once a trade has doubled, sell everything on a down day with volume more than 5x the average of the previous 20 days — a blow-off top (not used without volume data)"],["Cross-market","buy only while M2 is below its 20-day EMA (moves against it)"],["Five a year","at most 5 entries per calendar year; when used up, wait in cash for 1 January"],["Leverage","2x at most once per calendar year, never (leverage off); the borrowed half stays on until the exit"],["Sizing","position = min(equity, $10,000 x (1 + 0.5 x (equity/$10,000 - 1))): it grows at half the rate of the account; the rest earns 4% in cash"],["Costs and liquidation","0.1% fee per side; borrowing costs 8% a year, charged per calendar day; liquidated if a day's low leaves equity under 10% of the position (a low more than 50% below both open and close is a bad print and is capped)"],["Long or cash, no look-ahead","no shorts; every decision uses data up to that day’s close and fills at that close (take-profit and leverage-stop levels are fixed at entry)"]];

  const DAY = 864e5, SLOTS = ['', '2', '3', '4', '5', '6', '7', '8', '9', '10', '11', '12', '13', '14', '15'];
  // generational crash levels of the markets other scripts may read (owner B19): crypto 75%, stocks 65%, every other market 45%
  const GENLV = { BTC: 0.75, SOL: 0.75, XRP: 0.75, HYPE: 0.75, ZEC: 0.75, ETH: 0.75, NVDA: 0.65, AAPL: 0.65, AVGO: 0.65, TSLA: 0.65, AMZN: 0.65, AMD: 0.65, AXON: 0.65, INTC: 0.65 };
  // ---- engine (identical in every crest_*.js script) ----
  function ema(a, n) { const k = 2 / (n + 1), r = new Array(a.length); let e = a[0]; for (let i = 0; i < a.length; i++) { e = i ? a[i] * k + e * (1 - k) : a[i]; r[i] = e; } return r; }
  // highest value of a[i-n .. i-1] (strictly before bar i)
  function priorMax(a, n) { const r = new Array(a.length).fill(NaN), q = []; for (let i = 0; i < a.length; i++) { while (q.length && q[0] < i - n) q.shift(); if (i >= 1 && q.length) r[i] = a[q[0]]; while (q.length && a[q[q.length - 1]] <= a[i]) q.pop(); q.push(i); } return r; }

  function run(input, params, ctx) {
    const P0 = Object.assign({}, SETTINGS, params || {});
    let P = P0;
    const rows = input.filter(r => (!P.from || r.t >= P.from) && (!P.to || r.t <= P.to) && r.c > 0);
    const n = rows.length, S = P.start; if (n < 3) throw new Error('need at least 3 daily bars');
    // bad-print guard: a daily low more than 50% below both the open and the close is capped there (and reported)
    const cleaned = [];
    const L = rows.map(r => { const ref = Math.min(r.o > 0 ? r.o : r.c, r.c); if (!(r.l > 0)) return r.c; if (r.l < ref * 0.5) { cleaned.push({ date: new Date(r.t).toISOString().slice(0, 10), low: r.l, used: ref * 0.5 }); return ref * 0.5; } return r.l; });
    const T = rows.map(r => r.t), C = rows.map(r => r.c);
    // highs for take-profit fills; a high more than 100% above both open and close is a bad print and is capped there
    const Hh = rows.map(r => { const ref = Math.max(r.o > 0 ? r.o : r.c, r.c); return r.h > 0 ? Math.min(r.h, ref * 2) : r.c; });
    // cross-market filter (TradFi): another market's trend and its correlation with this one, from ctx[P.ctxRef] rows.
    // No look-ahead: the other market's trend at bar i uses its bars strictly BEFORE this bar's date (other markets can
    // close later the same day); the correlation uses completed days up to bar i-1 only.
    // up to fifteen filters: (ctxRef, ctxMode, ctxLen), (ctxRef2, ctxMode2, ctxLen2) … (ctxRef15 …); corrLen/corrThr shared;
    // an entry needs every active filter to agree
    const ctxTrace = P.ctxTrace ? [] : null;
    function makeFilter(ref, mode, len, slot) {
      const refRows = ref && ctx && ctx[ref] ? ctx[ref].filter(r => r.c > 0) : null;
      if (!refRows || refRows.length < 3) return () => true;
      const rc = refRows.map(r => r.c), rema = ema(rc, len || 100), before = new Array(n).fill(-1), upto = new Array(n).fill(-1);
      for (let i = 0, j = -1, k = -1; i < n; i++) { while (j + 1 < refRows.length && refRows[j + 1].t < T[i]) j++; while (k + 1 < refRows.length && refRows[k + 1].t <= T[i]) k++; before[i] = j; upto[i] = k; }
      // mode 'gen' (owner, 27 Sep 2026, B23): the other market's generational state, a fixed rule on its own closes: IN from its
      // generational buy (a fall of GENLV from its latest all-time high, then a close within 2x of the low since that high, above
      // its len-day EMA and above its prior 20 closes; once per all-time high) until its generational sell (a close GENLV/2 below
      // its highest close since that buy); an entry here needs that market IN, read strictly before this bar's date
      let gs = null; if (mode === 'gen') { const lv = GENLV[ref] || 0.45, hi20 = priorMax(rc, 20); gs = new Array(rc.length).fill(false); let mx = 0, lo = Infinity, used = 0, inS = false, pk = 0;
        for (let j = 0; j < rc.length; j++) { mx = Math.max(mx, rc[j]); lo = rc[j] >= mx ? rc[j] : Math.min(lo, rc[j]);
          if (!inS) { if (mx > used && 1 - lo / mx >= lv && rc[j] <= 2 * lo && rc[j] > rema[j] && rc[j] > hi20[j]) { inS = true; used = mx; pk = rc[j]; } }
          else { pk = Math.max(pk, rc[j]); if (rc[j] <= pk * (1 - lv / 2)) inS = false; }
          gs[j] = inS; } }
      const up = i => before[i] >= 0 && (gs ? gs[before[i]] : rc[before[i]] > rema[before[i]]), m = P.corrLen || 60, thr = P.corrThr == null ? 0.3 : P.corrThr;
      const ra = new Array(n).fill(0), rb = new Array(n).fill(0);
      for (let i = 1; i < n; i++) { ra[i] = Math.log(C[i] / C[i - 1]); rb[i] = upto[i] > 0 && upto[i - 1] >= 0 ? Math.log(rc[upto[i]] / rc[upto[i - 1]]) : 0; }
      const corr = i => { if (i - m < 1) return 0; let sa = 0, sb = 0, saa = 0, sbb = 0, sab = 0; for (let k = i - m; k < i; k++) { sa += ra[k]; sb += rb[k]; saa += ra[k] * ra[k]; sbb += rb[k] * rb[k]; sab += ra[k] * rb[k]; }
        const va = saa - sa * sa / m, vb = sbb - sb * sb / m; return va > 0 && vb > 0 ? (sab - sa * sb / m) / Math.sqrt(va * vb) : 0; };
      // no row of the other market before this bar: no filter (rather than an arbitrary pass or block)
      return i => { if (before[i] < 0) return true; let ok; if (mode === 'up' || mode === 'gen') ok = up(i); else if (mode === 'down') ok = !up(i); else { const c = corr(i); ok = c > thr ? up(i) : c < -thr ? !up(i) : true; }
        if (ctxTrace) (ctxTrace[i] = ctxTrace[i] || [])[slot] = [up(i) ? 1 : 0, mode === 'corr' ? corr(i) : null, ok ? 1 : 0]; return ok; };
    }
    // ---- walk-forward schedule (owner, 26 Sep 2026: Live = walk-forward optimisation) ----
    // P.wf = [{from: 'YYYY-MM-DD', set: {...}}, ...] ascending: from each 1 January the settings "set" (re-fitted on data up to
    // the day before, research/wfo.js) replace the previous ones; the account carries on as it stands (an open position is
    // kept and the new settings decide when it sells; owner's answer). Before the first segment (warm-up, the first re-fit
    // needs 365 days of data) the account stays in cash (owner, 27 Sep 2026; it held until then). Settings fixed by the
    // brief and the costs (start, fee, maxPerYear, lev, slowFactor, cashAPY, borrowAPR, maintenance, from, to, flow) never
    // change between segments. P.wfBase (optional): the values every segment starts from before its own "set" (the scripts
    // store each year's setting as its difference from wfBase). No P.wf (or an empty one): one setting for the whole run.
    const FIXED = ['ticker', 'start', 'from', 'to', 'fee', 'maxPerYear', 'lev', 'slowFactor', 'cashAPY', 'borrowAPR', 'maintenance', 'flow', 'ctxTrace', 'wf', 'wfBase', 'wfBy', 'wfId'];
    // per-ticker schedules (owner, 26 Sep 2026, B13: inside RUNNERS, RIDERS, OIL and MARKET every ticker re-fits alone): P.wfBy =
    // {TICKER: [{from, set}]}; the caller names the ticker with P.wfId. A ticker without a schedule of its own (a new listing,
    // not re-fitted yet) holds, as in the warm-up. A group script run without P.wfId is an error, never a silent default.
    if (P0.wfBy && P0.wfId == null) throw new Error('this script re-fits each ticker alone: pass params.wfId (the ticker id)');
    const WF = P0.wfBy ? (P0.wfBy[P0.wfId] && P0.wfBy[P0.wfId].length ? P0.wfBy[P0.wfId] : [{ from: '2999-01-01', set: {} }]) : Array.isArray(P0.wf) && P0.wf.length ? P0.wf : null;
    const segs = WF ? WF.map(sg => { const Q = Object.assign({}, P0, P0.wfBase || {}, sg.set || {}); for (const k of FIXED) Q[k] = P0[k]; const t = Date.parse(sg.from + 'T00:00:00Z'); let i0 = 0; while (i0 < n && T[i0] < t) i0++; return { from: sg.from, i: i0, P: Q }; }) : null;
    let FILTERS, trend, fast, hiPrior, levLine, gbLine, gcLine, rfxL, rfxF, cbDepth, cbLow, sqzOn, divA, spSup, useVal, useVol, VLEN, VOLN, valAvg, volRatio, hlMin, OL, obv, obvLine;
    const ema50 = ema(C, 50), ema20 = ema(C, 20);
    const ctxOK = i => { let ok = true; for (const f of FILTERS) ok = f(i) && ok; return ok; };   // every filter evaluated (trace), all must agree
    // the setting-dependent series (recomputed at each walk-forward segment; every one is causal: bar i uses bars <= i only)
    function setupA() {
      FILTERS = SLOTS.map((x, slot) => makeFilter(P['ctxRef' + x], P['ctxMode' + x], P['ctxLen' + x], slot));
      trend = ema(C, P.trendLen); fast = ema(C, P.genLen || P.trendLen); hiPrior = priorMax(C, P.breakLen);
      levLine = ema(C, P.levExit || 50);
      gbLine = P.genBuy > 0 ? ema(C, P.genBuyLen || 50) : null;
      gcLine = P.gcBuy > 0 ? ema(C, P.gcLen || 50) : null;
      rfxL = P.rfxStretch > 0 || P.rfxAcc > 0 ? ema(C, P.rfxLen || 200) : null; rfxF = P.rfxStretch > 0 ? ema(C, P.rfxFast || 20) : null;
      // cycle-buy crash window (genWin days, default 365; a longer window for a longer cycle): the deepest fall from the
      // all-time high within it and its lowest close (same method as the 365-day crash depth below)
      cbDepth = cbLow = null; if (P.genBuy > 0) { const W = P.genWin || 365, q = [], ql = []; cbDepth = new Array(n).fill(0); cbLow = [];
        for (let i = 0; i < n; i++) { const dd = 1 - C[i] / runMax[i]; while (q.length && q[0][0] < i - W) q.shift(); while (q.length && q[q.length - 1][1] <= dd) q.pop(); q.push([i, dd]); cbDepth[i] = q[0][1];
          while (ql.length && ql[0] < i - W) ql.shift(); while (ql.length && C[ql[ql.length - 1]] >= C[i]) ql.pop(); ql.push(i); cbLow.push(C[ql[0]]); } }
    }
    // crash depth (no look-ahead): deepest fall from the all-time high within the prior 365 days, and the 365-day low
    const runMax = []; { let m = 0; for (let i = 0; i < n; i++) { m = Math.max(m, C[i]); runMax.push(m); } }
    const depth = new Array(n).fill(0); { const q = []; for (let i = 0; i < n; i++) { const dd = 1 - C[i] / runMax[i]; while (q.length && q[0][0] < i - 365) q.shift(); while (q.length && q[q.length - 1][1] <= dd) q.pop(); q.push([i, dd]); depth[i] = q[0][1]; } }
    const low365 = []; { const q = []; for (let i = 0; i < n; i++) { while (q.length && q[0] < i - 365) q.shift(); while (q.length && C[q[q.length - 1]] >= C[i]) q.pop(); q.push(i); low365.push(C[q[0]]); } }
    const postCrash = i => depth[i] >= P.genDepth && C[i] <= 2 * low365[i];
    // generational crash (owner, 27 Sep 2026: "Buffett generational buy after a generational crash"; skill B19): the fall
    // from the latest all-time high to the lowest close since it (a new all-time high starts a new count)
    const lowAth = [], gDepth = []; { let lo = Infinity; for (let i = 0; i < n; i++) { lo = C[i] >= runMax[i] ? C[i] : Math.min(lo, C[i]); lowAth.push(lo); gDepth.push(1 - lo / runMax[i]); } }
    // generational buy (owner, 26 Sep 2026: "after a huge generational/cycle crash there is a generational buy opportunity like
    // Warren Buffett likes"; off when genBuy is 0 or unset): the market fell genBuy or more from its all-time high within the
    // past 365 days (the crash depth above) and is still within 2x of its one-year low; the first close above its
    // genBuyLen-day EMA (default 50) that is also above the highest close of the prior 20 days buys, even where the normal
    // entry rules would wait (trend line, slope, cooldown, filters, signals, minGap, athAfter, retire): at most one a year,
    // still within the 5 entries a year. Holding it (Buffett style): with genTrail > 0 the trend exit is ignored and the
    // position is sold only on a close genTrail below its highest close since the buy ('generational hold over'), or by the
    // setting's other exits (take profit, run over, signals); with genTrail 0 it uses the normal trend line, armed once a close
    // is above it, and until then is sold only if the close breaks the crash low. Every value uses closes up to bar i only.
    const gbHi = priorMax(C, 20);
    // (never on a day a sell signal is firing: that position would be sold at the next close)
    const genBuyOK = i => P.genBuy > 0 && i - lastGenBuy >= 365 && cbDepth[i] >= P.genBuy && C[i] <= 2 * cbLow[i] && C[i] > gbLine[i] && C[i] > gbHi[i] && cbGate(i) && !sigSell(i) && !cycleSell(i);
    // generational buy (gcBuy; skill B19, owner's levels: crypto 75%, stocks 65%, every other market 45%): the market fell gcBuy
    // or more from its latest all-time high (gDepth) and is still within 2x of the lowest close since that high; the first
    // close above its gcLen-day EMA (default 50) that also tops the prior 20 days' highest close buys, with the same overrides
    // as the cycle buy; at most one per crash (the next needs a new all-time high first). Held with gcTrail as genTrail above.
    const gcBuyOK = i => P.gcBuy > 0 && runMax[i] > gcAth && gDepth[i] >= P.gcBuy && C[i] <= 2 * lowAth[i] && C[i] > gcLine[i] && C[i] > gbHi[i] && !sigSell(i) && !cycleSell(i);
    // cycle phase of the cycle buy (owner, 27 Sep 2026: tune it to the longer cycles, crypto's 4-year cycle and stocks' own):
    // cbCycle 1 = days since the latest Bitcoin halving; 2 = days since 1 January of the year after a US presidential election
    // (1977, 1981, ... 2025: the 4-year presidential cycle); 3 = days since 1 January of a 7-year stock-cycle year (owner, 27 Sep
    // 2026: "cycle for stocks can be 7 years": 1980, 1987, 1994, 2001, 2008, 2015, 2022, 2029 ...). A cycle buy only while that count is in [cbFrom, cbTo). Dates only
    // (point-in-time); before the first halving on record the gate is open.
    const cbPhase = i => { if (P.cbCycle === 1) return halvDays(i); if (P.cbCycle === 2) { const y = year(i), y1 = y - (((y - 1) % 4) + 4) % 4; return (T[i] - Date.UTC(y1, 0, 1)) / DAY; }
      if (P.cbCycle === 3) { const y = year(i), y7 = y - (((y - 1980) % 7) + 7) % 7; return (T[i] - Date.UTC(y7, 0, 1)) / DAY; } return NaN; };
    const cbGate = i => { if (!(P.cbCycle > 0)) return true; const d = cbPhase(i); return !(d === d) || (d >= (P.cbFrom || 0) && d < (P.cbTo || 1e9)); };
    // tiers: generational buy = first entry after a crash of genDepth or more, near the lows (at most one a year);
    // cycle buy = every other entry
    let lastGen = -1e9;
    // ---- codified signals (optional; each one is off when its setting is 0 or unset, so a script without them is unchanged) ----
    // Price only (the rows carry no volume); every value at bar i uses closes up to and including bar i (no look-ahead).
    // Lengths count daily bars. A signal whose window is not yet full (too little history) is inactive: it blocks nothing
    // and sells nothing. Entry filters apply to NEW entries only (cycle and post-crash alike); sells apply to an open position.
    //  value       valLen-bar simple average of the close (default 1400 bars, about 200 weeks) = long-run fair value.
    //              valSell: sell everything on a close above valSell x that average ('overvalued (valSell)');
    //              valMax: no new entry on a close above valMax x that average ('overvalued (valMax)')
    //  momentum    momLen: a new entry needs the close above the close momLen bars earlier ('no momentum (momLen)')
    //  volatility  ratio = standard deviation of daily log returns over the last volLen bars (default 30) / over the last 365.
    //              volMax: no new entry while the ratio is above volMax ('volatility spike (volMax)');
    //              volExit: sell everything while long when the ratio is above volExit ('volatility spike (volExit)')
    //  structure   hlLen: a new entry needs a higher low: lowest close of the last hlLen bars above the lowest close of the
    //              hlLen bars before those ('no higher low (hlLen)')
    //  drawdown    ddMax: no new entry while the close is more than ddMax below its all-time high, unless the market is
    //              post-crash (generational entries stay allowed) ('deep below the high (ddMax)')
    //  A sell signal that is still firing also blocks a new entry (the next bar would sell again).
    function setupB() {
    useVal = P.valSell > 0 || P.valMax > 0; useVol = P.volMax > 0 || P.volExit > 0; VLEN = P.valLen || 1400; VOLN = P.volLen || 30;
    valAvg = new Array(n).fill(NaN); if (useVal) { let s = 0; for (let i = 0; i < n; i++) { s += C[i]; if (i >= VLEN) s -= C[i - VLEN]; if (i >= VLEN - 1) valAvg[i] = s / VLEN; } }
    volRatio = new Array(n).fill(NaN); if (useVol) { const s1 = [0], s2 = [0]; for (let i = 1; i < n; i++) { const x = Math.log(C[i] / C[i - 1]); s1.push(s1[i - 1] + x); s2.push(s2[i - 1] + x * x); }
      const vr = (i, w) => { const a = s1[i] - s1[i - w], b = s2[i] - s2[i - w]; return (b - a * a / w) / (w - 1); };
      for (let i = Math.max(VOLN, 365); i < n; i++) { const lv = vr(i, 365), sv = Math.max(0, vr(i, VOLN)); if (lv > 0) volRatio[i] = Math.sqrt(sv / lv); } }
    // squeeze (sqzLen; rule 1, 27 Sep 2026): width = highest / lowest close of the last sqzLen bars; a squeeze bar has the narrowest
    // width of the last 120 bars (ties count); a new entry needs a squeeze bar within the last 10 bars (a breakout from a coil).
    // sqzOn: 1 squeeze seen, 0 none (blocks), -1 not enough history (inactive)
    sqzOn = null; if (P.sqzLen > 0) { const Lq = P.sqzLen, A = Lq + 118, w = new Array(n).fill(NaN), sq = new Array(n).fill(false); sqzOn = new Array(n).fill(-1);
      for (let i = Lq - 1; i < n; i++) { let hi = -Infinity, lo = Infinity; for (let j = i - Lq + 1; j <= i; j++) { if (C[j] > hi) hi = C[j]; if (C[j] < lo) lo = C[j]; } w[i] = hi / lo; }
      for (let i = A; i < n; i++) { let m = Infinity; for (let j = i - 119; j < i; j++) if (w[j] < m) m = w[j]; sq[i] = w[i] <= m; }
      for (let i = A; i < n; i++) { let s = 0; for (let j = Math.max(A, i - 9); j <= i; j++) if (sq[j]) s = 1; sqzOn[i] = s; } }
    // bearish divergence (divGap; rule 1): RSI(14, Wilder). A divLen-bar closing high (no close in the divLen-1 bars before is
    // higher) whose RSI is more than divGap points below the highest RSI of the closing highs in the divLen bars before it
    // starts a divergence for divLen bars; a closing high without one ends it. Sell signal: divergence and a close below the
    // 20-day EMA (the trend is breaking while momentum already faded)
    divA = null; if (P.divGap > 0) { const DL = P.divLen || 60, rsi = new Array(n).fill(NaN); let ag = 0, al = 0;
      for (let i = 1; i < n; i++) { const d = C[i] - C[i - 1], g = d > 0 ? d : 0, l = d < 0 ? -d : 0; if (i <= 14) { ag += g / 14; al += l / 14; } else { ag = (ag * 13 + g) / 14; al = (al * 13 + l) / 14; } if (i >= 14) rsi[i] = al > 0 ? 100 - 100 / (1 + ag / al) : 100; }
      divA = new Array(n).fill(false); const hb = []; let until = -1;
      for (let i = 0; i < n; i++) { if (i >= DL - 1 && rsi[i] === rsi[i]) { let top = true; for (let j = i - DL + 1; j < i; j++) if (C[j] > C[i]) { top = false; break; }
          if (top) { while (hb.length && hb[0] < i - DL) hb.shift(); let ref = -Infinity; for (const j of hb) if (rsi[j] > ref) ref = rsi[j]; until = ref > -Infinity && rsi[i] < ref - P.divGap ? i + DL : -1; hb.push(i); } }
        divA[i] = i <= until; } }
    // spring (springLen; rule 1): the lowest close of the springLen bars before each bar (for a breakdown below it)
    spSup = P.springLen > 0 ? priorMax(C.map(x => -x), P.springLen).map(x => -x) : null;
    hlMin = new Array(n).fill(NaN); if (P.hlLen > 0) { const q = []; for (let i = 0; i < n; i++) { while (q.length && q[0] <= i - P.hlLen) q.shift(); while (q.length && C[q[q.length - 1]] >= C[i]) q.pop(); q.push(i); if (i >= P.hlLen - 1) hlMin[i] = C[q[0]]; } }
    }
    //  parabola   paraX: sell everything while long on a close at least paraX x the close paraLen bars earlier (default 60): a
    //              blow-off (rule 1, 27 Sep 2026), and no new entry while it holds
    //  divergence divGap: see setupB; sells and blocks like the other sell signals
    const sigSell = i => P.valSell > 0 && C[i] > P.valSell * valAvg[i] ? 'overvalued (valSell)' : P.volExit > 0 && volRatio[i] > P.volExit ? 'volatility spike (volExit)'
      : P.paraX > 0 && i >= (P.paraLen || 60) && C[i] >= P.paraX * C[i - (P.paraLen || 60)] ? 'parabolic blow-off (paraX)' : P.divGap > 0 && divA[i] && C[i] < ema20[i] ? 'bearish divergence (divGap)' : '';
    const sigBlock = i => sigSell(i) || (P.valMax > 0 && C[i] > P.valMax * valAvg[i] ? 'overvalued (valMax)'
      : P.momLen > 0 && i >= P.momLen && !(C[i] > C[i - P.momLen]) ? 'no momentum (momLen)'
      : P.volMax > 0 && volRatio[i] > P.volMax ? 'volatility spike (volMax)'
      : P.hlLen > 0 && i >= 2 * P.hlLen - 1 && !(hlMin[i] > hlMin[i - P.hlLen]) ? 'no higher low (hlLen)'
      : P.ddMax > 0 && 1 - C[i] / runMax[i] > P.ddMax && !postCrash(i) ? 'deep below the high (ddMax)'
      // reflexivity (rule 2, 27 Sep 2026; Soros's boom-bust): a new entry needs the self-reinforcing phase, the rfxLen-day EMA
      // (default 200) rising AND rising faster than before: its rise over the last rfxAcc bars above its rise over the rfxAcc bars before
      : P.rfxAcc > 0 && i >= 2 * P.rfxAcc && !(rfxL[i] > rfxL[i - P.rfxAcc] && rfxL[i] - rfxL[i - P.rfxAcc] > rfxL[i - P.rfxAcc] - rfxL[i - 2 * P.rfxAcc]) ? 'no reflexive acceleration (rfxAcc)'
      : P.sqzLen > 0 && sqzOn[i] === 0 ? 'no squeeze (sqzLen)' : '') || volBlock(i) || cycleBlock(i);
    // volume signals (rows' v; absent or 0 = no volume on that bar). Volume is exchange-specific, so a bar's volume is only
    // compared with this same series' own past volume. A volume signal is INACTIVE on a bar (blocks nothing, sells nothing)
    // when that bar has no volume or more than half of its look-back window has none (spliced older sources, index fixes).
    //  confirmation vcMult: a new entry needs the bar's volume above vcMult x the mean volume of the prior vcLen bars (default
    //              20; bars with volume only) ('no volume confirmation (vcMult)')
    //  accumulation obvLen: a new entry needs on-balance volume (running sum of +volume on up closes, -volume on down closes)
    //              above its obvLen-bar EMA ('no accumulation (obvLen)')
    //  climax      vxMult: while long and the close is at least 2x the entry, sell everything on a down close (below the
    //              previous close) whose volume is above vxMult x the mean of the prior vxLen bars (default 50) ('volume climax (vxMult)')
    const V = rows.map(r => r.v > 0 ? r.v : 0), psV = [0], psN = [0]; for (let i = 0; i < n; i++) { psV.push(psV[i] + V[i]); psN.push(psN[i] + (V[i] > 0 ? 1 : 0)); }
    const volMean = (i, w) => { if (!(w > 0) || i < w || !(V[i] > 0)) return NaN; const k = psN[i] - psN[i - w]; return 2 * k >= w ? (psV[i] - psV[i - w]) / k : NaN; };
    function setupC() {
    OL = P.obvLen > 0 ? P.obvLen : 0; obv = new Array(n).fill(0); if (OL) for (let i = 1; i < n; i++) obv[i] = obv[i - 1] + (C[i] > C[i - 1] ? V[i] : C[i] < C[i - 1] ? -V[i] : 0);
    obvLine = OL ? ema(obv, OL) : null;
    }
    const obvOn = i => OL && i >= OL && 2 * (psN[i + 1] - psN[i + 1 - OL]) >= OL;
    const climax = i => P.vxMult > 0 && C[i] >= 2 * entryPx && C[i] < C[i - 1] && V[i] > P.vxMult * volMean(i, P.vxLen || 50);
    const volBlock = i => { const m = P.vcMult > 0 ? volMean(i, P.vcLen || 20) : NaN;
      return m === m && !(V[i] > P.vcMult * m) ? 'no volume confirmation (vcMult)' : obvOn(i) && !(obv[i] > obvLine[i]) ? 'no accumulation (obvLen)' : ''; };
    // cycle and calendar signals (26 Sep 2026, owner's rule 33: "halving/credit cycles"; off when 0 or unset). Dates only, so
    // point-in-time by construction: a halving counts from its own UTC day (the block is mined during that day, so it is known
    // at that day's close); only halvings on or before bar i's date are used, never an estimate of the next one.
    //  halving     days since the latest Bitcoin block-reward halving (blocks 210,000 / 420,000 / 630,000 / 840,000). The
    //              late-cycle window runs from day halvWin (or halvSell) to day halvEnd (default 1000) after that halving.
    //              halvWin: no new entry inside [halvWin, halvEnd) ('halving cycle late (halvWin)');
    //              halvSell: sell everything while long inside [halvSell, halvEnd) ('halving cycle top (halvSell)'), and no
    //              new entry there. Before the first halving on record the signal is inactive. The next halving (block
    //              1,050,000, expected around April 2028) must be added to HALVINGS when it happens; until then the days
    //              since 2024-04-20 run past halvEnd and the signal goes inactive (it never blocks for ever).
    //  season      seasonOff = first calendar month (1-12, UTC) of a window of seasonLen months (default 2, wraps past
    //              December) in which no new entry is made ('season off (seasonOff)'); positions already held are kept.
    //  Like every entry filter, neither applies to the forced first-day buy (dayOne).
    const HALVINGS = [Date.UTC(2012, 10, 28), Date.UTC(2016, 6, 9), Date.UTC(2020, 4, 11), Date.UTC(2024, 3, 20)];
    const halvDays = i => { let h = -1; for (const x of HALVINGS) if (x <= T[i]) h = x; return h < 0 ? NaN : (T[i] - h) / DAY; };
    const halvLate = (i, from) => { if (!(from > 0)) return false; const d = halvDays(i); return d >= from && d < (P.halvEnd || 1000); };
    const seasonIn = i => { if (!(P.seasonOff > 0)) return false; const m = new Date(T[i]).getUTCMonth() + 1; return (m - P.seasonOff + 12) % 12 < (P.seasonLen || 2); };
    const cycleSell = i => halvLate(i, P.halvSell) ? 'halving cycle top (halvSell)' : '';
    const cycleBlock = i => cycleSell(i) || (halvLate(i, P.halvWin) ? 'halving cycle late (halvWin)' : seasonIn(i) ? 'season off (seasonOff)' : '');
    // spring (springLen; rule 1): a close below the lowest close of the springLen bars before it (a breakdown), then within 10
    // bars a close back above that level, above the 20-day EMA and above the previous close: the breakdown failed (Wyckoff)
    const springOK = i => { for (let j = i - 1; j >= Math.max(P.springLen, i - 10); j--) if (C[j] < spSup[j]) return C[i] > spSup[j] && C[i] > ema20[i] && C[i] > C[i - 1]; return false; };
    // capitulation (capMult; rule 1): a down close at least capDepth (default 30%) below the all-time high on volume above capMult
    // x the mean of the prior 50 bars (volume rules above); within 10 bars, a close above the close before that day buys (the
    // panic was absorbed). Spring and capitulation buys pass the same guards, filters and entry signals as a breakout, not the
    // trend line; like a cycle buy, the trend exit is armed once a close is above the trend line, and before that the
    // position is sold only on a close below the lowest close of the 10 bars up to the buy ('pattern buy failed (new low)')
    const capOK = i => { for (let k = i - 1; k >= Math.max(1, i - 10); k--) if (C[k] < C[k - 1] && 1 - C[k] / runMax[k] >= (P.capDepth || 0.3) && V[k] > P.capMult * volMean(k, 50)) return C[i] > C[k - 1]; return false; };
    const blocked = {}, blockedAt = [];   // entries a signal stopped: count by signal, and [bar, signal] in order
    const tierOf = (i, mark) => { if (postCrash(i) && i - lastGen >= 365) { if (mark) lastGen = i; return 'generational'; } return 'cycle'; };

    const year = i => new Date(T[i]).getUTCFullYear(), ds = i => new Date(T[i]).toISOString().slice(0, 10);
    const grow = (rate, days) => Math.pow(1 + rate, days / 365) - 1;
    let cash = S, units = 0, lunits = 0, debt = 0, inPos = false, below = 0, dead = false, entryPx = 0, peak = 0, early = false, lastLossExit = -1e9, tp1Done = false, tp2Done = false, tp3Done = false, peakH = 0, stretchDone = false, exitBar = -1,
      retired = false, reentryAbove = 0, lastEntry = -1e9, levStopAt = 0, levEqAt = 0, genPos = false, genArmed = false, genFloor = 0, lastGenBuy = -1e9, gcAth = 0, genKind = '', boomSeen = false;   // retire / athAfter / minGap (see the entry rule below)
    const entries = {}, levYears = {}, trades = [], curve = [], exposure = [], hold = [], flows = [];
    // cash flows (P.flow): null = keep compounding; {kind:'reset'} = back to the start amount at each year start
    // (profit taken out, or a top-up after a loss); {kind:'take', pct, month} = take pct of the account out at the end of
    // month (12 = December). Applied at the first bar after the month ends, at the previous close, pro rata across cash
    // and position (so the exposure is unchanged). Buy & hold gets the same flows (without fees).
    let holdU = S / C[0], withdrawn = 0, deposited = 0, hWithdrawn = 0, hDeposited = 0, idx = 1, base = S, pkI = 1, ddI = 0;
    const flowAt = i => { if (!P.flow || i === 0) return false; const b = Date.UTC(year(i), P.flow.kind === 'reset' ? 0 : P.flow.month % 12, 1); return T[i - 1] < b && b <= T[i]; };
    const equityAt = px => cash + (units + lunits) * px - debt;
    const notionalFor = e => Math.max(0, Math.min(e, S * (1 + P.slowFactor * (e / S - 1))));
    const levOK = (i, tier) => P.levWhen === 'first' ? true : P.levWhen === 'generational' ? tier === 'generational'
      : P.levWhen === 'rising' ? (i >= 30 && trend[i] > trend[i - 30] && C[i] > ema50[i]) : false;
    function buy(i, why, tier) {
      const y = year(i), px = C[i], eq = cash, useLev = P.lev > 1 && !levYears[y] && i > 0 && levOK(i, tier);
      const N = Math.min(notionalFor(eq), eq / (1 + (useLev ? P.lev : 1) * P.fee));
      units = N / px; lunits = useLev ? (P.lev - 1) * N / px : 0; debt = lunits * px;
      cash = eq - units * px - (units + lunits) * px * P.fee;
      if (useLev) levYears[y] = true;
      // the leverage stop level is fixed at the 2x entry and stays in force whatever setting a later walk-forward year uses (a
      // later setting without a stop, or a buy & hold year, must not leave borrowed money unprotected: found 27 Sep 2026, AMD's
      // walk-forward carried a 1982 2x position into hold years without a stop and was liquidated in 2012)
      levStopAt = useLev && P.levStop ? px * (1 - P.levStop) : 0;
      // ... and in equity terms: the share of the position the account would keep at that level on the entry day. Interest on
      // the borrowed half (borrowAPR) raises the liquidation price while 2x is held, so the stop also fires when equity falls to
      // that share, from the debt owed that day (found 27 Sep 2026: META's 2016 2x position, held six years, was liquidated in
      // October 2022 by a gap to 26% below its entry, before its 20% price stop)
      levEqAt = levStopAt > 0 ? (cash + (units + lunits) * levStopAt - debt) / ((units + lunits) * levStopAt) : 0;
      entries[y] = (entries[y] || 0) + 1; inPos = true; below = 0; boomSeen = false; entryPx = px; peak = px; peakH = px; lastEntry = i; tp1Done = tp2Done = tp3Done = stretchDone = false;
      trades.push({ date: ds(i), i, side: 'BUY', why, price: px, lev: useLev ? P.lev : 1, equity: equityAt(px), tier, depth: depth[i] });
    }
    // selling the borrowed half repays all borrowing: if that half sold at a loss, enough of the rest is sold too so
    // cash never goes below zero (no hidden borrowing left)
    function sellLev(i, px, why) { px = px == null ? C[i] : px; cash += lunits * px * (1 - P.fee) - debt; lunits = 0; debt = 0; if (cash < 0) { units -= -cash / (px * (1 - P.fee)); cash = 0; } trades.push({ date: ds(i), i, side: 'TRIM', why: why || 'leverage off', price: px, lev: 1, equity: equityAt(px), tier: 'cycle' }); }
    // partial sale: fraction f of the whole position (and of any borrowing), e.g. a take-profit level or an overextension
    function sellFrac(i, px, f, why) { cash += f * (units + lunits) * px * (1 - P.fee) - f * debt; units *= 1 - f; lunits *= 1 - f; debt *= 1 - f;
      trades.push({ date: ds(i), i, side: 'TRIM', why, frac: f, price: px, lev: 1, equity: equityAt(px), tier: px >= 3 * entryPx ? 'generational' : 'cycle', gain: px / entryPx - 1 }); }
    function flow(i) {
      const px = C[i - 1], eq = dead ? 0 : equityAt(px), hv = holdU * px; let f = 1, amt = 0, revive = false;
      if (P.flow.kind === 'reset') {
        if (dead || eq <= 0) { revive = true; dead = false; cash = S; units = lunits = debt = 0; inPos = false; amt = -S; }
        else { f = S / eq; const fee = f > 1 ? P.fee * (units + lunits) * (f - 1) * px : 0; amt = f < 1 ? eq - S - P.fee * (units + lunits) * (1 - f) * px : -(S - eq); cash = cash * f - fee; units *= f; lunits *= f; debt *= f; }
        if (hv > S) hWithdrawn += hv - S; else hDeposited += S - hv; holdU = S / px;
      } else {
        if (dead || eq <= 0) return; f = 1 - P.flow.pct; amt = P.flow.pct * eq - P.flow.pct * P.fee * (units + lunits) * px;
        cash *= f; units *= f; lunits *= f; debt *= f; hWithdrawn += P.flow.pct * hv; holdU *= f;
      }
      if (amt > 0) withdrawn += amt; else deposited -= amt;
      base = equityAt(px); flows.push({ date: ds(i), i, f, px, revive, amount: amt });
    }
    function sellAll(i, why, px) {
      px = px == null ? C[i] : px;
      cash += (units + lunits) * px * (1 - P.fee) - debt; units = 0; lunits = 0; debt = 0; inPos = false; exitBar = i; genPos = false;
      if (cash <= 0) { cash = 0; dead = true; why = 'LIQUIDATED'; }
      // sell tier (known at the exit): generational = the run is over (trailing sell, or the trade at least tripled)
      const tier = why === 'LIQUIDATED' ? 'liquidated' : why === 'run over' || why === 'overvalued (valSell)' || why === 'halving cycle top (halvSell)' || px >= 3 * entryPx ? 'generational' : 'cycle';
      if (px < entryPx) lastLossExit = i;
      // after a big run (the trade's highest close, known at the exit): retire = never buy this ticker again once a trade
      // has peaked at retire x its entry; athAfter = after a trade that peaked at athAfter x, buy again only above that peak
      const pm = Math.max(peak, px) / entryPx;
      if (P.retire && pm >= P.retire) retired = true;
      if (P.athAfter && pm >= P.athAfter) reentryAbove = Math.max(reentryAbove, peak, px);
      trades.push({ date: ds(i), i, side: 'SELL', why, price: px, lev: 1, equity: cash, tier, gain: px / entryPx - 1, peakX: pm, peakPx: Math.max(peak, px) });
    }
    // walk-forward: the first segment's settings from bar 0, then each segment's from its day. The warm-up bars before the first
    // segment stay in cash (owner, 27 Sep 2026: "entry only when signal fires", no day-one entries anywhere): the account waits
    // for the first yearly re-fit's own buy. Exception: a placeholder schedule (first segment dated 2999 or later: no re-fit yet,
    // a new listing) holds from the first close, like a useless-for-now ticker (owner's answer: those keep buy & hold)
    let seg = segs ? 0 : -1; if (segs) P = segs[0].P; setupA(); setupB(); setupC();
    const warmTo = segs ? segs[0].i : 0, warmHold = !!segs && segs[0].from >= '2999';   // bars before warmTo are warm-up
    for (let i = 0; i < n; i++) {
      if (segs && seg + 1 < segs.length && i >= segs[seg + 1].i) {
        while (seg + 1 < segs.length && i >= segs[seg + 1].i) seg++;
        P = segs[seg].P; setupA(); setupB(); setupC();
        // the account's own closed trades, judged by the new settings' retire / athAfter levels
        retired = false; reentryAbove = 0; boomSeen = false;   // a reflexive boom is judged by the new setting's own lines
        for (const t of trades) if (t.side === 'SELL' && t.why !== 'LIQUIDATED') { if (P.retire && t.peakX >= P.retire) retired = true; if (P.athAfter && t.peakX >= P.athAfter) reentryAbove = Math.max(reentryAbove, t.peakPx); }
        // an open position: the new take-profit levels the price has already traded through since the entry count as passed
        // (a past price cannot be sold at); the levels above stay live
        if (inPos) { tp1Done = !!P.tp1 && peakH >= entryPx * P.tp1; tp2Done = !!P.tp2 && peakH >= entryPx * P.tp2; tp3Done = !!P.tp3 && peakH >= entryPx * P.tp3; }
      }
      const warm = i < warmTo;
      if (i > 0) { const days = (T[i] - T[i - 1]) / DAY; if (cash > 0) cash *= 1 + grow(P.cashAPY, days); if (debt > 0) debt *= 1 + grow(P.borrowAPR, days); }
      if (flowAt(i)) flow(i);
      if (dead) { curve.push(0); exposure.push(0); hold.push(holdU * C[i]); idx = 0; continue; }
      // leverage stop: the borrowed half is sold if the day trades down to levStop below the entry (a level fixed at
      // entry); it fills at that level, or at the open if the day opens below it
      const levLvl = lunits > 0 && levStopAt > 0 ? Math.max(levStopAt, levEqAt < 1 ? (debt - cash) / ((units + lunits) * (1 - levEqAt)) : 0) : 0;
      if (!warm && inPos && lunits > 0 && levLvl > 0 && L[i] <= levLvl) {
        const o = rows[i].o > 0 ? rows[i].o : C[i - 1], px = Math.min(levLvl, o), pos = (units + lunits) * px;
        if (cash + pos - debt > P.maintenance * pos) sellLev(i, px, 'leverage stop');
      }
      // liquidation if the bar's low leaves equity below the maintenance margin (only possible while borrowing)
      if (inPos && debt > 0) { const pos = (units + lunits) * L[i]; if (cash + pos - debt <= P.maintenance * pos) { sellAll(i, 'LIQUIDATED', L[i]); dead = true; cash = 0; curve.push(0); exposure.push(0); hold.push(holdU * C[i]); idx = 0; ddI = -1; continue; } }
      // take-profit ladder: sell tpNFrac of the position when the day trades up to tpN x the entry (levels fixed at entry);
      // fills at the level, or at the open if the day opens above it; a fraction of 1 closes the trade
      if (!warm && inPos && i > 0) for (const [lvl, f, k] of [[P.tp1, P.tp1Frac, 1], [P.tp2, P.tp2Frac, 2], [P.tp3, P.tp3Frac, 3]]) {
        if (!inPos || !lvl || !f || (k === 1 ? tp1Done : k === 2 ? tp2Done : tp3Done) || Hh[i] < entryPx * lvl) continue;
        const o = rows[i].o > 0 ? rows[i].o : C[i - 1], px = Math.min(Math.max(entryPx * lvl, o), Math.max(Hh[i], entryPx * lvl));
        if (k === 1) tp1Done = true; else if (k === 2) tp2Done = true; else tp3Done = true;
        if (f >= 1) sellAll(i, 'take profit ' + lvl + 'x', px); else sellFrac(i, px, f, 'take profit ' + lvl + 'x');
      }
      if (warm) { if (warmHold && i === 0) buy(0, 'hold (no re-fit yet)', 'cycle'); else if (inPos) peak = Math.max(peak, C[i]); }
      // holdOnly (a walk-forward re-fit where buy & hold made the most dollars): buy at the first close in cash, never sell
      else if (P.holdOnly) { if (inPos) peak = Math.max(peak, C[i]); else if (cash > 0 && (entries[year(i)] || 0) < P.maxPerYear && exitBar !== i && !retired && C[i] > reentryAbove && i - lastEntry >= (P.minGap || 0)) buy(i, 'hold (buy & hold made the most dollars)', tierOf(i, true)); }   // the same entry guards as every buy
      else if (i === 0) { if (P.dayOne) buy(0, 'day one', 'cycle'); }
      else if (inPos) {
        peak = Math.max(peak, C[i]);
        if (rfxL && P.rfxStretch > 0 && C[i] >= P.rfxStretch * rfxL[i]) boomSeen = true;   // reflexive boom reached (see below)
        if (early && C[i] > trend[i]) early = false;                    // an early post-crash entry graduates to the main trend line
        const line = early ? fast[i] : trend[i];
        // a generational position (see genBuyOK): genTrail > 0 holds it through the cycle, sold only on a genTrail fall from its
        // top; otherwise its trend exit is armed once a close is above the line, and before that only a break of the crash low
        // (the lowest close of the year before the buy) sells it
        if (genPos && !genArmed && C[i] > line) genArmed = true;
        below = C[i] < line * (1 - P.exitBuf) ? below + 1 : 0;
        const gTrail = genKind === 'gc' ? P.gcTrail : genKind === 'cb' ? P.genTrail : 0, gName = genKind === 'gc' ? 'generational buy' : genKind === 'cb' ? 'cycle buy' : 'pattern buy';
        if (genPos && gTrail > 0) { below = 0; if (C[i] <= peak * (1 - gTrail)) sellAll(i, gName + ' hold over'); }
        else if (genPos && !genArmed) { below = 0; if (C[i] < genFloor) sellAll(i, gName + ' failed (new low)'); }
        if (!inPos) {}
        else if (below >= P.exitDays) sellAll(i, 'trend exit');
        // second, tighter trailing sell once the run reaches trailArm2 x the entry (let a run go far, then protect it)
        else if (P.trail2 && peak >= entryPx * P.trailArm2 && C[i] < peak * (1 - P.trail2)) sellAll(i, 'run over');
        else if (P.trail && peak >= entryPx * P.trailArm && C[i] < peak * (1 - P.trail)) sellAll(i, 'run over');
        // reflexive bust (rule 2): once the trade has closed rfxStretch x or more above the rfxLen-day EMA (a self-reinforcing
        // boom), sell on the first close below the faster rfxFast-day EMA (default 20): the feedback loop has turned
        else if (P.rfxStretch > 0 && boomSeen && C[i] < rfxF[i]) sellAll(i, 'reflexive bust (rfxStretch)');
        else if (P.stretch && !stretchDone && C[i] >= trend[i] * P.stretch) { stretchDone = true; if ((P.stretchFrac || 1) >= 1) sellAll(i, 'overextended'); else sellFrac(i, C[i], P.stretchFrac, 'overextended'); }
        else if (sigSell(i)) sellAll(i, sigSell(i));
        else if (climax(i)) sellAll(i, 'volume climax (vxMult)');
        else if (cycleSell(i)) sellAll(i, cycleSell(i));
        else if (lunits > 0 && P.levExit && C[i] < levLine[i]) sellLev(i);
      } else if (exitBar !== i && cash > 0 && (entries[year(i)] || 0) < P.maxPerYear && gcBuyOK(i)) {
        genFloor = lowAth[i]; buy(i, 'generational buy (after a generational crash)', 'generational'); lastGen = i; lastGenBuy = i; gcAth = runMax[i]; genPos = true; genKind = 'gc'; genArmed = false; early = false;
      } else if (exitBar !== i && cash > 0 && (entries[year(i)] || 0) < P.maxPerYear && genBuyOK(i)) {
        genFloor = cbLow[i]; buy(i, 'cycle buy (after a cycle crash)', 'cycle'); lastGenBuy = i; genPos = true; genKind = 'cb'; genArmed = false; early = false;
      } else if (exitBar !== i && !retired && C[i] > reentryAbove && i - lastEntry >= (P.minGap || 0) && (entries[year(i)] || 0) < P.maxPerYear && cash > 0) {   // never re-buy on an exit day; minGap days between entries
        let why = '', pat = false;
        if (C[i] > hiPrior[i]) {
          // cycle entries need a rising trend line (slopeLen) and no losing exit in the past cooldown days; post-crash
          // entries are exempt: they are the bottom-catching entries
          const pc = postCrash(i), rising = !P.slopeLen || (i >= P.slopeLen && trend[i] > trend[i - P.slopeLen]), cooled = pc || i - lastLossExit > (P.cooldown || 0);
          const normal = C[i] > trend[i] && (rising || pc) && cooled, earlyOK = P.genLen > 0 && pc && C[i] > fast[i];
          if (normal || earlyOK) why = normal ? 'breakout' : 'post-crash breakout';
        }
        if (!why && P.springLen > 0 && springOK(i)) { why = 'spring (failed breakdown)'; pat = true; }
        if (!why && P.capMult > 0 && capOK(i)) { why = 'capitulation reversal (capMult)'; pat = true; }
        if (why && ctxOK(i)) { const no = sigBlock(i); if (no) { blocked[no] = (blocked[no] || 0) + 1; blockedAt.push([i, no]); } else { const tier = tierOf(i, true); buy(i, why, tier); early = why === 'post-crash breakout';
          if (pat) { genPos = true; genKind = 'pat'; genArmed = false; genFloor = Math.min(...C.slice(Math.max(0, i - 10), i + 1)); } } }
      }
      if (inPos && i > lastEntry) peakH = Math.max(peakH, Hh[i]);   // highs after the entry bar (the entry fills at the close)
      const eq = Math.max(0, equityAt(C[i])); curve.push(eq); exposure.push(eq > 0 ? (units + lunits) * C[i] / eq : 0); hold.push(holdU * C[i]);
      // time-weighted index (cash flows taken out), for drawdown and growth rate
      if (i > 0 && base > 0) idx *= eq / base; base = eq; pkI = Math.max(pkI, idx); ddI = Math.min(ddI, idx / pkI - 1);
    }
    for (const t of trades) t.seg = segs ? segs.reduce((k, sg, j) => t.i >= sg.i ? j : k, -1) : 0;   // -1 = warm-up
    let dd = ddI, hpeak = 0, hdd = 0;
    for (let i = 0; i < n; i++) { hpeak = Math.max(hpeak, C[i]); hdd = Math.min(hdd, C[i] / hpeak - 1); }
    const lastIdx = {}; T.forEach((t, i) => { lastIdx[year(i)] = i; });
    const fy = {}; for (const f of flows) { const y = year(f.i - 1); fy[y] = (fy[y] || 0) + f.amount; }
    const annual = Object.keys(lastIdx).map(Number).sort((a, b) => a - b).map(y => ({ year: y, crest: curve[lastIdx[y]], hold: hold[lastIdx[y]], entries: entries[y] || 0, lev: !!levYears[y], flow: fy[y] || 0 }));
    const yrs = (T[n - 1] - T[0]) / (365.25 * DAY);
    const nowTier = postCrash(n - 1) && n - 1 - lastGen >= 365 ? 'generational' : 'cycle';
    return { ticker: P.ticker, params: P, wf: segs ? segs.map(sg => ({ from: sg.from, i: sg.i, P: sg.P })) : null, first: ds(0), last: ds(n - 1), bars: n, dates: T, close: C, curve, hold, exposure, trades, annual,
      final: curve[n - 1], holdFinal: hold[n - 1], maxDD: dd * 100, holdMaxDD: hdd * 100,
      flows, withdrawn, deposited, holdWithdrawn: hWithdrawn, holdDeposited: hDeposited,
      cagr: idx > 0 && yrs > 0 ? (Math.pow(idx, 1 / yrs) - 1) * 100 : -100,
      holdCagr: yrs > 0 ? (Math.pow(C[n - 1] / C[0], 1 / yrs) - 1) * 100 : 0,
      entries: trades.filter(t => t.side === 'BUY').length, maxEntriesInAYear: Math.max(0, ...Object.values(entries)),
      levEntries: trades.filter(t => t.side === 'BUY' && t.lev > 1).length, liquidated: dead, position: inPos ? 'LONG' : 'CASH', cleaned, ctxTrace, blocked, blockedAt,
      now: { date: ds(n - 1), depth: depth[n - 1], tier: nowTier, trend: trend[n - 1], fast: fast[n - 1], early, line: (early ? fast[n - 1] : trend[n - 1]) * (1 - P.exitBuf),
        hi: Math.max(...C.slice(Math.max(0, n - P.breakLen))), postCrash: postCrash(n - 1), peak, entryPx, retired, reentryAbove, trailLevel: P.trail && peak >= entryPx * P.trailArm ? peak * (1 - P.trail) : null, athDist: C[n - 1] / runMax[n - 1] - 1, genPos, genKind: genPos ? genKind : '', genBuyLine: gbLine ? gbLine[n - 1] : null, genBuyReady: P.genBuy > 0 && n - 1 - lastGenBuy >= 365 && cbDepth[n - 1] >= P.genBuy && C[n - 1] <= 2 * cbLow[n - 1] && cbGate(n - 1),
        gDepth: gDepth[n - 1], gcLine: gcLine ? gcLine[n - 1] : null, gcReady: P.gcBuy > 0 && runMax[n - 1] > gcAth && gDepth[n - 1] >= P.gcBuy && C[n - 1] <= 2 * lowAth[n - 1] } };
  }

  // 10x runs: zigzag on closes; a run ends when price falls 70% from its peak; runs of 10x or more are reported.
  // priceCapture = share of the run's log gain earned while long (signal quality, independent of position size);
  // equityMultiple = account growth over the run (includes the 50%-slower sizing and leverage).
  function tenX(res, fall) {
    fall = fall == null ? 0.7 : fall; const C = res.close, out = []; let lo = 0, hi = 0, up = true;
    const legs = [];
    for (let i = 0; i < C.length; i++) {
      if (up) { if (C[i] > C[hi]) hi = i; if (C[i] <= C[hi] * (1 - fall)) { legs.push([lo, hi]); up = false; lo = i; } }
      else { if (C[i] < C[lo]) lo = i; if (C[i] >= C[lo] / (1 - fall)) { up = true; hi = i; } }
    }
    if (up) legs.push([lo, hi]);
    // share of the original position still held each day (partial sales count only for what is left)
    const long = new Array(C.length).fill(0); { let on = 0, k = 0; for (let i = 0; i < C.length; i++) { long[i] = on; while (k < res.trades.length && res.trades[k].i === i) { const t = res.trades[k]; if (t.side === 'BUY') on = 1; if (t.side === 'SELL') on = 0; if (t.side === 'TRIM' && t.frac) on *= 1 - t.frac; k++; } } }
    for (const [a, b] of legs) {
      const m = C[b] / C[a]; if (m < 10) continue;
      let lg = 0; for (let i = a + 1; i <= b; i++) if (long[i]) lg += long[i] * Math.log(C[i] / C[i - 1]);
      const pc = lg / Math.log(m);
      out.push({ from: res.first && new Date(res.dates[a]).toISOString().slice(0, 10), to: new Date(res.dates[b]).toISOString().slice(0, 10), holdMultiple: m,
        equityMultiple: res.curve[a] > 0 ? res.curve[b] / res.curve[a] : 0, priceCapture: pc, caught: pc >= 0.5 });
    }
    return out;
  }
  const api = { run, tenX, SETTINGS, VERSION, RULES };
  if (typeof module !== 'undefined' && module.exports) module.exports = api; else root.CREST_SILVER = api;
})(typeof window !== 'undefined' ? window : globalThis);
