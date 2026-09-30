// No-code strategy builder UI → NoCodeSpec JSON (SPEC §10). DOM via core h(); state kept in the spec object.
import { h, mount, button, field } from "../../core/ui.js";
import type { NoCodeSpec, NoCodeRule, NoCodeCondition, Operand, Comparator, IndicatorKind, PriceSource } from "./types.js";
import { INDICATOR_KINDS, PRICE_SOURCES, MAX_MARKETS, PLATFORM_MAX_LEVERAGE, defaultSpec, validateSpec, operandLabel } from "./nocode.js";

const COIN_RE = /^(?:[a-z0-9]{1,12}:)?[A-Za-z0-9]{1,20}$/;
const OPS: Comparator[] = [">", ">=", "<", "<="];

export interface NoCodeBuilder {
  el: HTMLElement;
  spec(): NoCodeSpec;
  errors(): string[];
}

export function noCodeBuilder(initial?: NoCodeSpec, onChange?: (spec: NoCodeSpec) => void): NoCodeBuilder {
  const spec: NoCodeSpec = initial ? structuredClone(initial) : defaultSpec();
  const root = h("div", { class: "stack" });
  const errBox = h("div", { class: "stack tight", "aria-live": "polite" });
  const jsonBox = h("pre", { class: "code small" });

  const changed = (): void => {
    const errs = validateSpec(spec);
    mount(errBox, errs.length ? h("ul", { class: "small neg" }, ...errs.map((e) => h("li", null, e))) : h("p", { class: "small pos" }, "Spec looks valid. The server compiles it and runs the same checks as uploaded Python."));
    jsonBox.textContent = JSON.stringify(spec, null, 2);
    onChange?.(spec);
  };

  const sel = <T extends string>(value: T, opts: { v: T; l: string }[], set: (v: T) => void, rerender = false, label?: string): HTMLSelectElement => {
    const s = h("select", { "aria-label": label ?? "" }, ...opts.map((o) => h("option", { value: o.v }, o.l)));
    s.value = value;
    s.addEventListener("change", () => {
      set(s.value as T);
      if (rerender) draw();
      else changed();
    });
    return s;
  };
  const numIn = (value: number, set: (n: number) => void, attrs: Record<string, string | number> = {}, label?: string): HTMLInputElement => {
    const i = h("input", { type: "number", value: String(value), inputmode: "decimal", "aria-label": label ?? "", ...attrs });
    i.addEventListener("input", () => {
      const n = Number(i.value);
      set(Number.isFinite(n) ? n : NaN);
      changed();
    });
    return i;
  };
  const coinOpts = (): { v: string; l: string }[] => spec.markets.map((m) => ({ v: m, l: m }));

  const operandEditor = (get: () => Operand, set: (o: Operand) => void, label: string): HTMLElement => {
    const o = get();
    const type: "ref" | "price" | "const" = "ref" in o ? "ref" : "price" in o ? "price" : "const";
    const typeSel = sel(
      type,
      [
        { v: "ref", l: "Indicator" },
        { v: "price", l: "Price" },
        { v: "const", l: "Number" },
      ],
      (t) => {
        if (t === "ref") set({ ref: spec.indicators[0]?.id ?? "" });
        else if (t === "price") set({ price: "c", coin: spec.markets[0] ?? "" });
        else set({ const: 0 });
      },
      true,
      `${label} type`,
    );
    let valueCtl: HTMLElement;
    if ("ref" in o) {
      valueCtl = sel(o.ref, spec.indicators.map((i) => ({ v: i.id, l: i.id })), (v) => set({ ref: v }), false, `${label} indicator`);
    } else if ("price" in o) {
      const cur = o;
      valueCtl = h(
        "span",
        { class: "nc-operand" },
        sel(cur.coin, coinOpts(), (v) => set({ price: cur.price, coin: v }), false, `${label} coin`),
        sel(cur.price, PRICE_SOURCES.map((p) => ({ v: p.src, l: p.label })), (v) => set({ price: v as PriceSource, coin: cur.coin }), false, `${label} field`),
      );
    } else {
      valueCtl = numIn(o.const, (n) => set({ const: n }), { step: "any" }, `${label} number`);
    }
    return h("div", { class: "field" }, h("span", { class: "fl" }, label), h("span", { class: "nc-operand" }, typeSel, valueCtl));
  };

  const draw = (): void => {
    const marketsIn = h("input", { type: "text", value: spec.markets.join(", "), placeholder: "BTC, xyz:SILVER", spellcheck: "false" });
    marketsIn.addEventListener("change", () => {
      const list = [...new Set(marketsIn.value.split(/[,\s]+/).map((x) => x.trim()).filter(Boolean))].filter((x) => COIN_RE.test(x)).slice(0, MAX_MARKETS);
      if (list.length) {
        spec.markets = list;
        // keep references valid
        for (const ind of spec.indicators) if (!list.includes(ind.coin)) ind.coin = list[0];
        for (const r of spec.rules) if (!list.includes(r.coin)) r.coin = list[0];
      }
      draw();
    });

    const indRows = spec.indicators.map((ind, idx) => {
      const idIn = h("input", { type: "text", value: ind.id, "aria-label": "Indicator name", maxlength: 24, spellcheck: "false" });
      idIn.addEventListener("change", () => {
        const old = ind.id;
        ind.id = idIn.value.trim().toLowerCase();
        // rename references
        for (const r of spec.rules) for (const c of r.conditions) for (const side of ["left", "right"] as const) {
          const op = c[side];
          if ("ref" in op && op.ref === old) c[side] = { ref: ind.id };
        }
        draw();
      });
      const usesSource = INDICATOR_KINDS.find((k) => k.kind === ind.kind)?.usesSource !== false;
      return h(
        "div",
        { class: "nc-row ind" },
        field("Name", idIn),
        field("Indicator", sel(ind.kind, INDICATOR_KINDS.map((k) => ({ v: k.kind, l: k.label })), (v) => { ind.kind = v as IndicatorKind; }, true)),
        field("Coin", sel(ind.coin, coinOpts(), (v) => { ind.coin = v; })),
        usesSource ? field("Of", sel(ind.source, PRICE_SOURCES.map((p) => ({ v: p.src, l: p.label })), (v) => { ind.source = v as PriceSource; })) : h("div", { class: "small muted" }, "uses high/low/close"),
        field("Period", numIn(ind.period, (n) => { ind.period = Math.trunc(n); }, { min: 2, max: 1000, step: 1 })),
        button("Remove", { kind: "ghost", onClick: () => { spec.indicators.splice(idx, 1); draw(); } }),
      );
    });

    const ruleBlocks = spec.rules.map((rule: NoCodeRule, ri) =>
      h(
        "div",
        { class: "nc-rule" },
        h(
          "div",
          { class: "row between" },
          h("b", null, `Rule ${ri + 1}`),
          button("Remove rule", { kind: "ghost", onClick: () => { spec.rules.splice(ri, 1); draw(); } }),
        ),
        h(
          "div",
          { class: "form-grid two-col" },
          field("Coin", sel(rule.coin, coinOpts(), (v) => { rule.coin = v; })),
          field("Match", sel(rule.combine, [{ v: "all", l: "ALL conditions (and)" }, { v: "any", l: "ANY condition (or)" }], (v) => { rule.combine = v as "all" | "any"; })),
        ),
        ...rule.conditions.map((c: NoCodeCondition, ci) =>
          h(
            "div",
            { class: "nc-row cond" },
            operandEditor(() => c.left, (o) => { c.left = o; changed(); }, "Left"),
            field("Is", sel(c.op, OPS.map((o) => ({ v: o, l: o })), (v) => { c.op = v as Comparator; })),
            operandEditor(() => c.right, (o) => { c.right = o; changed(); }, "Right"),
            button("×", { kind: "ghost", title: "Remove condition", onClick: () => { rule.conditions.splice(ci, 1); draw(); } }),
          ),
        ),
        h(
          "div",
          { class: "row" },
          button("+ Condition", {
            kind: "ghost",
            onClick: () => {
              rule.conditions.push({ left: { price: "c", coin: rule.coin }, op: ">", right: spec.indicators[0] ? { ref: spec.indicators[0].id } : { const: 0 } });
              draw();
            },
          }),
          field("Then target weight", numIn(rule.weight, (n) => { rule.weight = n; }, { step: "0.1", min: -PLATFORM_MAX_LEVERAGE, max: PLATFORM_MAX_LEVERAGE })),
        ),
        h("p", { class: "small muted" }, `When the conditions match, hold ${rule.weight}× of the allocation in ${rule.coin} (negative = short). Example: `, rule.conditions.map((c) => `${operandLabel(c.left)} ${c.op} ${operandLabel(c.right)}`).join(rule.combine === "all" ? " AND " : " OR ") || "—"),
      ),
    );

    mount(
      root,
      h(
        "div",
        { class: "form-grid two-col" },
        field("Markets (1–5, comma separated)", marketsIn, "Hyperliquid perp coins, e.g. BTC, SOL, xyz:SILVER."),
        field("Timeframe", sel(spec.timeframe, [{ v: "1d", l: "1 day" }, { v: "4h", l: "4 hours" }, { v: "1h", l: "1 hour" }], (v) => { spec.timeframe = v as NoCodeSpec["timeframe"]; })),
        field("Lookback (bars)", numIn(spec.lookback, (n) => { spec.lookback = Math.trunc(n); }, { min: 50, max: 1000, step: 1 })),
        field("Max leverage", sel(String(spec.max_leverage), Array.from({ length: PLATFORM_MAX_LEVERAGE }, (_, i) => ({ v: String(i + 1), l: `${i + 1}×` })), (v) => { spec.max_leverage = Number(v); })),
        field("Default weight (no rule matches)", numIn(spec.default_weight, (n) => { spec.default_weight = n; }, { step: "0.1" })),
      ),
      h("h3", null, "Indicators"),
      ...indRows,
      h(
        "div",
        { class: "btns" },
        button("+ Indicator", {
          kind: "ghost",
          onClick: () => {
            let n = spec.indicators.length + 1;
            while (spec.indicators.some((i) => i.id === `ind${n}`)) n++;
            spec.indicators.push({ id: `ind${n}`, kind: "ema", coin: spec.markets[0] ?? "BTC", source: "c", period: 20 });
            draw();
          },
        }),
      ),
      h("h3", null, "Rules"),
      h("p", { class: "small muted" }, "Rules are checked in order for each coin at every bar close; the first matching rule sets that coin's target weight."),
      ...ruleBlocks,
      h(
        "div",
        { class: "btns" },
        button("+ Rule", {
          kind: "ghost",
          onClick: () => {
            spec.rules.push({ coin: spec.markets[0] ?? "BTC", combine: "all", conditions: [], weight: 1 });
            draw();
          },
        }),
      ),
      errBox,
      h("details", null, h("summary", null, "Show JSON spec"), jsonBox),
    );
    changed();
  };
  draw();

  return {
    el: root,
    spec: () => structuredClone(spec),
    errors: () => validateSpec(spec),
  };
}
