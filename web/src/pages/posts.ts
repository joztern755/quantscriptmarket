// #/posts (list, optional ?strategy=slug) and #/posts/:id (read; purchase paid posts from the fee balance).
import type { PageContext } from "../core/router.js";
import { h, mount, skeleton, errorState, emptyState, note, button, confirmDialog, toast } from "../core/ui.js";
import { api, newIdempotencyKey } from "../core/api.js";
import { fmtUsd, fmtDate } from "../core/format.js";
import type { Post } from "./_shared/types.js";
import { renderMarkdown } from "./_shared/markdown.js";
import { ensurePageCss, listOf, isAbortError, errCode, pageHead, replaceQuery } from "./_shared/util.js";

export const title = "Posts";

export async function render(root: HTMLElement, ctx: PageContext): Promise<void> {
  ensurePageCss();
  const id = ctx.params.id;
  if (id) return renderOne(root, ctx, id);
  return renderList(root, ctx);
}

async function renderList(root: HTMLElement, ctx: PageContext): Promise<void> {
  let filter = ctx.query.get("strategy") ?? "";
  let price: "" | "free" | "paid" = (ctx.query.get("price") as "free" | "paid") ?? "";
  if (price !== "free" && price !== "paid") price = "";
  const list = h("div", { class: "stack" }, skeleton(6));
  const priceSel = h(
    "select",
    { id: "p-price", onchange: () => { price = priceSel.value as typeof price; void load(); } },
    h("option", { value: "" }, "Free and paid"),
    h("option", { value: "free" }, "Free only"),
    h("option", { value: "paid" }, "Paid only"),
  );
  priceSel.value = price;
  mount(
    root,
    h(
      "div",
      { class: "stack" },
      pageHead("Posts", "Research & updates", "Notes from strategy creators. Paid posts are charged once to your fee balance. Posts are opinions and education, not investment advice."),
      h(
        "div",
        { class: "filters" },
        h("div", { class: "field" }, h("label", { for: "p-price" }, "Show"), priceSel),
        filter
          ? h("div", { class: "row small" }, "Strategy: ", h("span", { class: "chip" }, filter), h("button", { class: "btn ghost sm", type: "button", onclick: () => { filter = ""; void load(); } }, "Clear"))
          : null,
      ),
      list,
    ),
  );

  let seq = 0;
  const load = async (): Promise<void> => {
    const my = ++seq;
    replaceQuery("/posts", { strategy: filter || null, price: price || null });
    mount(list, skeleton(6));
    try {
      const q = filter ? `?strategy=${encodeURIComponent(filter)}` : "";
      const res = await api.get<unknown>(`/public/posts${q}`, { signal: ctx.signal, auth: !!ctx.user });
      if (!ctx.isCurrent() || my !== seq) return;
      const posts = listOf<Post>(res, "posts").filter((p) => (price === "free" ? p.price_micro === 0 : price === "paid" ? p.price_micro > 0 : true));
      if (!posts.length) {
        mount(list, emptyState("No posts yet", filter ? "This strategy has no posts yet." : "Creators haven't published anything yet."));
        return;
      }
      mount(list, ...posts.map(postItem));
    } catch (err) {
      if (isAbortError(err) || !ctx.isCurrent() || my !== seq) return;
      mount(list, errorState(err, () => void load()));
    }
  };
  await load();
}

function postItem(p: Post): HTMLElement {
  return h(
    "article",
    { class: "post-item" },
    h("a", { class: "title", href: `#/posts/${encodeURIComponent(p.id)}` }, p.title),
    h(
      "div",
      { class: "row small muted" },
      p.price_micro > 0 ? h("span", { class: "pill warn" }, p.purchased ? "Purchased" : fmtUsd(p.price_micro)) : h("span", { class: "pill good" }, "Free"),
      p.strategy_slug ? h("a", { href: `#/s/${encodeURIComponent(p.strategy_slug)}` }, p.strategy_name ?? p.strategy_slug) : null,
      p.creator_name ? h("span", null, p.creator_name) : null,
      p.published_at ? h("span", null, fmtDate(p.published_at)) : null,
    ),
    p.excerpt ? h("p", { class: "small" }, p.excerpt) : null,
  );
}

async function renderOne(root: HTMLElement, ctx: PageContext, id: string): Promise<void> {
  if (!/^[A-Za-z0-9_-]{1,64}$/.test(id)) {
    mount(root, emptyState("Post not found", undefined, h("a", { class: "btn", href: "#/posts" }, "All posts")));
    return;
  }
  // One idempotency key per page view so a retried purchase can never double-charge.
  const purchaseKey = newIdempotencyKey();
  const load = async (): Promise<void> => {
    mount(root, skeleton(10));
    try {
      const p = await api.get<Post>(`/public/posts/${encodeURIComponent(id)}`, { signal: ctx.signal, auth: !!ctx.user });
      if (!ctx.isCurrent()) return;
      ctx.setTitle(p.title);
      draw(p);
    } catch (err) {
      if (isAbortError(err) || !ctx.isCurrent()) return;
      if (errCode(err) === "not_found") {
        mount(root, emptyState("Post not found", undefined, h("a", { class: "btn", href: "#/posts" }, "All posts")));
        return;
      }
      mount(root, errorState(err, () => void load()));
    }
  };

  const draw = (p: Post): void => {
    const hasBody = typeof p.body === "string" && p.body.length > 0;
    const locked = p.price_micro > 0 && !hasBody;
    mount(
      root,
      h(
        "article",
        { class: "stack" },
        h("a", { href: "#/posts", class: "small" }, "← All posts"),
        h(
          "div",
          { class: "stack tight" },
          h("div", { class: "eyebrow" }, p.price_micro > 0 ? `Paid post · ${fmtUsd(p.price_micro)}` : "Free post"),
          h("h1", { class: "page-title" }, p.title),
          h(
            "div",
            { class: "row small muted" },
            p.creator_name ? h("span", null, p.creator_name) : null,
            p.strategy_slug ? h("a", { href: `#/s/${encodeURIComponent(p.strategy_slug)}` }, p.strategy_name ?? p.strategy_slug) : null,
            p.published_at ? h("span", null, fmtDate(p.published_at)) : null,
          ),
        ),
        locked
          ? h(
              "div",
              { class: "panel stack" },
              p.excerpt ? h("div", { class: "prose" }, renderMarkdown(p.excerpt)) : null,
              h("p", null, `This post costs ${fmtUsd(p.price_micro)}, charged once to your prepaid fee balance. You keep access afterwards.`),
              ctx.user
                ? h(
                    "div",
                    { class: "btns" },
                    button(`Buy for ${fmtUsd(p.price_micro)}`, {
                      kind: "primary",
                      onClick: async () => {
                        const ok = await confirmDialog({
                          title: "Buy this post?",
                          message: `${fmtUsd(p.price_micro)} will be deducted from your fee balance. Purchases are final.`,
                          confirmLabel: `Pay ${fmtUsd(p.price_micro)}`,
                        });
                        if (!ok) return;
                        try {
                          await api.post(`/posts/${encodeURIComponent(p.id)}/purchase`, {}, { signal: ctx.signal, idempotencyKey: purchaseKey });
                          toast("Purchased. Enjoy the read.", "good");
                          await load();
                        } catch (err) {
                          if (errCode(err) === "insufficient_balance") {
                            toast("Not enough fee balance. Top up in your dashboard.", "warn");
                            ctx.navigate("/dashboard/balance");
                            return;
                          }
                          throw err;
                        }
                      },
                    }),
                    h("a", { class: "btn", href: "#/dashboard/balance" }, "Top up fee balance"),
                  )
                : h("a", { class: "btn primary", href: `#/signin?next=${encodeURIComponent("/posts/" + p.id)}` }, "Sign in to buy"),
            )
          : h("div", { class: "prose" }, renderMarkdown(p.body ?? p.excerpt ?? "")),
        note("Posts reflect the author's views and are not investment advice. Trading perpetual futures can lose all allocated funds.", "info"),
      ),
    );
  };
  await load();
}
