// Full-address display with copy + compare (SECURITY H1 / address poisoning). Every signing dialog shows the WHOLE
// EIP-55 checksummed address (never 0x1234…abcd), grouped for reading aloud, with a Copy button and a Compare box
// where the user pastes what their wallet / hardware-wallet screen shows. The comparison is on all 40 hex characters.

import { isAddress, toChecksumAddress } from "./keccak.js";
import { h, toast } from "./ui.js";

/** "0xAbCd…" → "0xAbCd 1234 5678 …" (groups of 4 after 0x). Throws on a malformed address. */
export function groupedChecksum(address: string): string {
  const c = toChecksumAddress(address);
  return "0x " + (c.slice(2).match(/.{1,4}/g) ?? []).join(" ");
}

/** Case-insensitive, whitespace-tolerant comparison of two addresses (all 40 hex characters). */
export function sameAddress(a: string, b: string): boolean {
  const n = (s: string) => s.replace(/\s+/g, "").toLowerCase();
  const x = n(a);
  const y = n(b);
  return isAddress(x) && isAddress(y) && x === y;
}

export function addressCheck(address: string, opts: { label?: string; hint?: string } = {}): HTMLElement {
  if (!isAddress(address)) return h("p", { class: "status err" }, `Invalid address — do not sign.`);
  const checksum = toChecksumAddress(address);
  const result = h("p", { class: "small", "aria-live": "polite" });
  const input = h("input", {
    type: "text",
    class: "mono",
    spellcheck: "false",
    autocomplete: "off",
    "aria-label": `Paste the ${opts.label ?? "address"} your wallet shows to compare`,
    placeholder: "Paste the address your wallet shows to compare",
  });
  input.addEventListener("input", () => {
    const v = input.value.trim();
    if (!v) {
      result.textContent = "";
      result.className = "small";
      return;
    }
    if (sameAddress(v, address)) {
      result.textContent = "Match: all 40 characters are identical.";
      result.className = "small status ok";
    } else {
      result.textContent = "DOES NOT MATCH — do not sign. Cancel and contact support.";
      result.className = "small status err";
    }
  });
  const copy = h("button", {
    type: "button",
    class: "btn sm",
    onclick: async () => {
      try {
        await navigator.clipboard.writeText(checksum);
        toast("Address copied", "good", 1800);
      } catch {
        toast("Copy failed — select the address and copy it manually", "warn");
      }
    },
  }, "Copy");
  return h(
    "div",
    { class: "addr-check stack", dataset: { address: address.toLowerCase() } },
    opts.label ? h("div", { class: "small muted" }, opts.label) : null,
    h("code", { class: "mono break addr-full", title: checksum }, groupedChecksum(address)),
    h("div", { class: "btns" }, copy),
    input,
    result,
    h("p", { class: "small muted" }, opts.hint ?? "Check EVERY character against your wallet screen before approving — not just the first and last few."),
  );
}
