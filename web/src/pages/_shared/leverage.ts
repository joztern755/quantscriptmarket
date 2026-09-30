// Leverage bounds for the subscribe wizard and the dashboard "Edit subscription" dialog (owner decision, 30 Sep 2026):
// there is NO per-user allocation cap, NO platform total cap and NO platform/launch leverage cap. A subscription's max
// leverage is bounded only by min(strategy MAX_LEVERAGE, the market's max leverage from Hyperliquid `meta`) — the
// lowest across the strategy's markets. `/v1/public/config` may still carry platform_max_leverage (e.g. 50) and
// max_user_leverage_x100 = null; they are not UI limits (platform_max_leverage is only a last-resort fallback when
// neither the strategy nor Hyperliquid tells us anything). The backend re-checks (422 details.max_x100).
import { h, note } from "../../core/ui.js";
import { hlInfo } from "../../core/hl.js";
import { isRec } from "./util.js";

/** Above this, the UI shows a stronger liquidation warning (not a cap). */
export const HIGH_LEVERAGE_X = 5;

const metaCache = new Map<string, Promise<Map<string, number>>>();

/** "xyz:SILVER" → "xyz"; "BTC" → "" (validator-operated perps). */
export function dexOf(coin: string): string {
  const i = coin.indexOf(":");
  return i > 0 ? coin.slice(0, i) : "";
}

/** coin → maxLeverage from Hyperliquid `meta` for one dex (cached per page load). Builder-dex universes may list
 *  names with or without the "dex:" prefix; both are indexed with the prefix. */
function dexMeta(dex: string): Promise<Map<string, number>> {
  let p = metaCache.get(dex);
  if (!p) {
    p = hlInfo<unknown>(dex ? { type: "meta", dex } : { type: "meta" }).then((raw) => {
      const out = new Map<string, number>();
      const uni = isRec(raw) && Array.isArray(raw.universe) ? raw.universe : [];
      for (const u of uni) {
        if (!isRec(u) || typeof u.name !== "string") continue;
        const lev = Number(u.maxLeverage);
        if (!Number.isFinite(lev) || lev < 1) continue;
        const name = dex && !u.name.includes(":") ? `${dex}:${u.name}` : u.name;
        out.set(name, Math.floor(lev));
      }
      return out;
    });
    p.catch(() => metaCache.delete(dex));
    metaCache.set(dex, p);
  }
  return p;
}

/** Lowest Hyperliquid max leverage across `markets`, or null when any market is unknown / Hyperliquid unreachable. */
export async function marketMaxLeverage(markets: string[]): Promise<number | null> {
  if (!markets.length) return null;
  try {
    let lo = Infinity;
    for (const coin of markets) {
      const v = (await dexMeta(dexOf(coin))).get(coin);
      if (typeof v !== "number") return null;
      lo = Math.min(lo, v);
    }
    return Number.isFinite(lo) ? lo : null;
  } catch {
    return null;
  }
}

/** min(strategy MAX_LEVERAGE, market max leverage); whichever is known. `fallback` (config platform_max_leverage)
 *  only when neither is known. Always ≥ 1, integer. */
export function leverageBound(strategyMax: number | null | undefined, marketMax: number | null | undefined, fallback: number): number {
  const known = [strategyMax, marketMax].filter((x): x is number => typeof x === "number" && Number.isFinite(x) && x >= 1);
  const m = known.length ? Math.min(...known) : fallback;
  return Math.max(1, Math.floor(Number.isFinite(m) ? m : 1));
}

/** Hint under the leverage field: where the bound comes from. */
export function leverageHint(strategyMax: number | null | undefined, marketMax: number | null | undefined, bound: number): string {
  const parts: string[] = [];
  if (typeof strategyMax === "number" && strategyMax >= 1) parts.push(`strategy maximum ${Math.floor(strategyMax)}×`);
  if (typeof marketMax === "number" && marketMax >= 1) parts.push(`Hyperliquid market maximum ${marketMax}×`);
  return parts.length ? `Up to ${bound}× (${parts.join(", ")}).` : `Up to ${bound}×.`;
}

/** Stronger warning for leverage above HIGH_LEVERAGE_X (shown in addition to the standard loss warning). */
export function highLeverageWarning(lev: number): HTMLElement | null {
  if (!(lev > HIGH_LEVERAGE_X)) return null;
  const move = Math.max(1, Math.floor(100 / lev));
  return note(
    h(
      "span",
      { class: "high-lev" },
      h("b", null, `High leverage (${lev}×). `),
      `At ${lev}× a price move of roughly ${move}% against a position can wipe out its margin and trigger liquidation — before the strategy gets a chance to exit. Gaps, funding and fees make it happen sooner. Only choose this if you accept losing the whole allocation quickly.`,
    ),
    "bad",
  );
}
