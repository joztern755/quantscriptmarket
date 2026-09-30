// No-code strategy builder UI → NoCodeSpec JSON in the exact backend format (sandbox/nocode.py, SPEC §10).
// DOM via core h(); state is the spec object itself.
import { h, mount, button, field } from "../../core/ui.js";
import type { IndicatorKind, NcCondition, NcOperand, NcOp, NcRule, NcSource, NcWhen, NoCodeSpec } from "./types.js";
import {
  INDICATOR_KINDS,
  SOURCES,
  PRICE_FIELDS,
  OPS,
  MAX_MARKETS,
  PLATFORM_MAX_LEVERAGE,
  MAX_PERIOD,
  MAX_SHIFT,
  defaultSpec,
  validateSpec,
  toServerSpec,
  operandLabel,
  opLabel,
  isCondition,
  whenItems,
} from "./nocode.js";

const COIN_RE = /^(?:[a-z][a-z0-9]{0,15}:)?[A-Za-z0-9]{1,20}$/;

export interface NoCodeBuilder {
  el: HTMLElement;
  spec(): NoCodeSpec;
  errors(): string[];
}

type OperandKind = "ind" | "px" | "num";
const kindOf = (o: NcOperand): OperandKind => (typeof o === "number" ? "num" : (PRICE_FIELDS as readonly string[]).includes(o) ? "px" : "ind");

export function noCodeBuilder(initial?: NoCodeSpec, onChange?: (spec: NoCodeSpec) => void): NoCodeBuilder {
  const spec: NoCodeSpec = initial ? toServerSpec(initial) : defaultSpec();
  const root = h("div", { class: "stack" });
  const errBox = h("div", { class: "stack tight", "aria-live": "polite" });
  const jsonBox = h("pre", { class: "code small" });

  const changed = (): void => {
    const errs = validateSpec(spec);
    mount(errBox, errs.length ? h("ul", { class: "small neg" }, ...errs.map((e) => h("li", null, e))) : h("p", { class: "small pos" }, "Spec looks valid. The server compiles it and runs the same checks as uploaded Python."));
    jsonBox.textContent = JSON.stringify(toServerSpec(spec), null, 2);
    onChange?.(toServerSpec(spec));
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
      set(i.value.trim() !== "" && Number.isFinite(n) ? n : NaN);
      changed();
    });
    return i;
  };
  const indicatorIds = (): string[] => Object.keys(spec.indicators);

  const operandEditor = (get: () => NcOperand, set: (o: NcOperand) => void, label: string): HTMLElement => {
    const o = get();
    const kind = kindOf(o);
    const typeSel = sel<OperandKind>(
      kind,
      [
        { v: "ind", l: "Indicator" },
        { v: "px", l: "Price" },
        { v: "num", l: "Number" },
      ],
      (t) => set(t === "ind" ? indicatorIds()[0] ?? "close" : t === "px" ? "close" : 0),
      true,
      `${label} type`,
    );
    let valueCtl: HTMLElement;
    if (kind === "ind") valueCtl = sel(o as string, indicatorIds().map((id) => ({ v: id, l: id })), (v) => set(v), false, `${label} indicator`);
    else if (kind === "px") valueCtl = sel(o as string, PRICE_FIELDS.map((p) => ({ v: p, l: `${p} (last closed bar)` })), (v) => set(v), false, `${label} price field`);
    else valueCtl = numIn(o as number, (n) => set(n), { step: "any" }, `${label} number`);
    return h("div", { class: "field" }, h("span", { class: "fl" }, label), h("span", { class: "nc-operand" }, typeSel, valueCtl));
  };

  /** Rename an indicator id everywhere (keeps key order; spec.indicators is an ordered JSON object). */
  const renameIndicator = (old: string, next: string): void => {
    if (!next || next === old || next in spec.indicators) return;
    const entries = Object.entries(spec.indicators).map(([k, v]) => [k === old ? next : k, v] as const);
    spec.indicators = Object.fromEntries(entries);
    const fix = (w: NcWhen): void => {
      for (const it of whenItems(w).items) {
        if (isCondition(it)) {
          if (it.left === old) it.left = next;
          if (it.right === old) it.right = next;
        } else fix(it);
      }
    };
    for (const r of spec.rules) fix(r.when);
  };

  const draw = (): void => {
    const marketsIn = h("input", { type: "text", value: spec.markets.join(", "), placeholder: "BTC, xyz:SILVER", spellcheck: "false" });
    marketsIn.addEventListener("change", () => {
      const list = [...new Set(marketsIn.value.split(/[,\s]+/).map((x) => x.trim()).filter(Boolean))].filter((x) => COIN_RE.test(x)).slice(0, MAX_MARKETS);
      if (list.length) spec.markets = list;
      draw();
    });

    const indRows = Object.entries(spec.indicators).map(([id, ind]) => {
      const idIn = h("input", { type: "text", value: id, "aria-label": "Indicator name", maxlength: 32, spellcheck: "false" });
      idIn.addEventListener("change", () => {
        renameIndicator(id, idIn.value.trim().toLowerCase());
        draw();
      });
      const usesSource = INDICATOR_KINDS.find((k) => k.kind === ind.type)?.usesSource !== false;
      return h(
        "div",
        { class: "nc-row ind" },
        field("Name", idIn),
        field(
          "Indicator",
          sel(ind.type, INDICATOR_KINDS.map((k) => ({ v: k.kind, l: k.label })), (v) => {
            ind.type = v as IndicatorKind;
            if (ind.type === "atr") delete ind.source;
            else ind.source = ind.source ?? "close";
          }, true),
        ),
        usesSource ? field("Of", sel(ind.source ?? "close", SOURCES.map((p) => ({ v: p, l: p })), (v) => { ind.source = v as NcSource; })) : h("div", { class: "small muted" }, "uses high/low/close"),
        field("Period", numIn(ind.period, (n) => { ind.period = Math.trunc(n); }, { min: 1, max: MAX_PERIOD, step: 1 })),
        field("Bars ago (shift)", numIn(ind.shift ?? 0, (n) => { ind.shift = Math.trunc(n); }, { min: 0, max: MAX_SHIFT, step: 1 })),
        button("Remove", { kind: "ghost", onClick: () => { delete spec.indicators[id]; draw(); } }),
      );
    });

    const ruleBlocks = spec.rules.map((rule: NcRule, ri) => {
      const { combine, items } = whenItems(rule.when);
      const setCombine = (c: "all" | "any"): void => {
        rule.when = c === "all" ? { all: items } : { any: items };
      };
      return h(
        "div",
        { class: "nc-rule" },
        h("div", { class: "row between" }, h("b", null, `Rule ${ri + 1}`), button("Remove rule", { kind: "ghost", onClick: () => { spec.rules.splice(ri, 1); draw(); } })),
        field("Match", sel(combine, [{ v: "all", l: "ALL conditions (and)" }, { v: "any", l: "ANY condition (or)" }], (v) => setCombine(v as "all" | "any"))),
        ...items.map((it, ci) =>
          isCondition(it)
            ? h(
                "div",
                { class: "nc-row cond" },
                operandEditor(() => it.left, (o) => { it.left = o; draw(); }, "Left"),
                field("Is", sel(it.op, OPS.map((o) => ({ v: o.op, l: o.label })), (v) => { it.op = v as NcOp; })),
                operandEditor(() => it.right, (o) => { it.right = o; draw(); }, "Right"),
                button("×", { kind: "ghost", title: "Remove condition", onClick: () => { items.splice(ci, 1); draw(); } }),
              )
            : h("p", { class: "small muted" }, "Nested group (kept as is): ", JSON.stringify(it)),
        ),
        h(
          "div",
          { class: "row" },
          button("+ Condition", {
            kind: "ghost",
            onClick: () => {
              const c: NcCondition = { left: "close", op: ">", right: indicatorIds()[0] ?? 0 };
              items.push(c);
              draw();
            },
          }),
          field("Then target weight", numIn(rule.weight, (n) => { rule.weight = n; }, { step: "0.1", min: -PLATFORM_MAX_LEVERAGE, max: PLATFORM_MAX_LEVERAGE })),
        ),
        h(
          "p",
          { class: "small muted" },
          `When this matches, hold ${rule.weight}× of the allocation in EACH market (negative = short). `,
          items.filter(isCondition).map((c) => `${operandLabel(c.left)} ${opLabel(c.op)} ${operandLabel(c.right)}`).join(combine === "all" ? " AND " : " OR ") || "—",
        ),
      );
    });

    mount(
      root,
      h(
        "div",
        { class: "form-grid two-col" },
        field("Markets (1–5, comma separated)", marketsIn, "Hyperliquid perp coins, e.g. BTC, SOL, xyz:SILVER. Must match the strategy's markets."),
        field("Timeframe", sel(spec.timeframe, [{ v: "1d", l: "1 day" }, { v: "4h", l: "4 hours" }, { v: "1h", l: "1 hour" }], (v) => { spec.timeframe = v as NoCodeSpec["timeframe"]; })),
        field("Lookback (bars)", numIn(spec.lookback, (n) => { spec.lookback = Math.trunc(n); }, { min: 50, max: 1000, step: 1 })),
        field("Max leverage", sel(String(spec.max_leverage), Array.from({ length: PLATFORM_MAX_LEVERAGE }, (_, i) => ({ v: String(i + 1), l: `${i + 1}×` })), (v) => { spec.max_leverage = Number(v); })),
        field("Default weight (no rule matches)", numIn(spec.default_weight, (n) => { spec.default_weight = n; }, { step: "0.1" })),
      ),
      h("h3", null, "Indicators"),
      h("p", { class: "small muted" }, "Each indicator is computed on every market's own bars."),
      ...indRows,
      h(
        "div",
        { class: "btns" },
        button("+ Indicator", {
          kind: "ghost",
          onClick: () => {
            let n = Object.keys(spec.indicators).length + 1;
            while (`ind${n}` in spec.indicators) n++;
            spec.indicators[`ind${n}`] = { type: "ema", source: "close", period: 20 };
            draw();
          },
        }),
      ),
      h("h3", null, "Rules"),
      h("p", { class: "small muted" }, "At every bar close, rules are checked in order for each market; the first matching rule sets that market's target weight. Weights apply to each market, so markets × |weight| must stay within max leverage."),
      ...ruleBlocks,
      h(
        "div",
        { class: "btns" },
        button("+ Rule", {
          kind: "ghost",
          onClick: () => {
            const first = indicatorIds()[0];
            spec.rules.push({ when: { all: [{ left: "close", op: ">", right: first ?? 0 }] }, weight: 1 });
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
    spec: () => toServerSpec(spec),
    errors: () => validateSpec(spec),
  };
}
