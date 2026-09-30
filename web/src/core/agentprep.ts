// Agent preparation (migrations/0016 — executor-side key generation).
//
// POST /v1/agents only files a REQUEST: the api cannot generate or seal an agent key. The aijalon EXECUTOR generates
// the key, seals it, proves the sealed blob re-opens to the address and KMS-signs
//     aijalon-agent-v2|{user_id}|{agent_address}
// (usually within a minute: every tick + the attest-agents job). The browser polls GET /v1/agents/{id} until the row
// carries the address AND an attestation that verifies against the PINNED public key (core/attest.ts). Nothing here
// trusts the server's word: the address is only accepted together with a valid attestation for exactly this user.

import { verifyAgentAttestation } from "./attest.js";

/** GET /v1/agents/{id} (backend app/api/routers/agents.py AgentDetailOut). */
export interface AgentDetail {
  agent: { id: string; master_address: string; agent_address: string | null; status: string };
  user_id: string;
  ready: boolean;
  failed?: boolean;
  attestation: { signature_b64: string; key_version?: string; attested_at?: string } | null;
}

export type AgentPrepResult =
  | { kind: "ready"; agentAddress: string; signatureB64: string }
  /** the executor refused this request (ops were paged): create a new agent */
  | { kind: "failed" }
  /** 404, or the request was replaced / revoked (another tab, rotation) */
  | { kind: "gone" }
  /** the server returned an address whose attestation does NOT verify, or data for another agent/user: fail closed */
  | { kind: "invalid"; reason: string }
  | { kind: "timeout" }
  | { kind: "cancelled" };

const ADDR = /^0x[0-9a-fA-F]{40}$/;
const TERMINAL = new Set(["revoked", "rotated", "expired"]);

export interface WaitOptions {
  agentId: string;
  userId: string;
  /** the master wallet the request was made for (lower-case); a response for another master is refused */
  master?: string;
  /** GET /v1/agents/{id}; resolve null on 404 */
  fetchAgent: (agentId: string) => Promise<AgentDetail | null>;
  timeoutMs?: number;
  intervalMs?: number;
  isCurrent?: () => boolean;
  onWaiting?: (polls: number) => void;
  sleep?: (ms: number) => Promise<void>;
  now?: () => number;
  /** tests only; production uses the key pinned in app-config.json */
  publicKeySpkiB64?: string;
}

/** Poll until the executor has generated AND attested the agent key (or a terminal outcome). Never throws for the
 *  expected outcomes; network errors from fetchAgent propagate to the caller. */
export async function waitForAgentReady(o: WaitOptions): Promise<AgentPrepResult> {
  const now = o.now ?? (() => Date.now());
  const sleep = o.sleep ?? ((ms: number) => new Promise<void>((res) => setTimeout(res, ms)));
  const deadline = now() + (o.timeoutMs ?? 180_000);
  const interval = Math.max(250, o.intervalMs ?? 4000);
  const uid = o.userId.toLowerCase();
  let polls = 0;
  for (;;) {
    if (o.isCurrent && !o.isCurrent()) return { kind: "cancelled" };
    const r = await o.fetchAgent(o.agentId);
    polls++;
    if (r === null) return { kind: "gone" };
    if (String(r.agent?.id ?? "").toLowerCase() !== o.agentId.toLowerCase() || String(r.user_id ?? "").toLowerCase() !== uid) {
      return { kind: "invalid", reason: "the server returned a different agent" };
    }
    if (o.master && String(r.agent.master_address ?? "").toLowerCase() !== o.master.toLowerCase()) {
      return { kind: "invalid", reason: "the agent belongs to a different wallet" };
    }
    if (r.failed) return { kind: "failed" };
    if (TERMINAL.has(String(r.agent.status))) return { kind: "gone" };
    const addr = r.agent.agent_address;
    const sig = r.attestation?.signature_b64;
    if (addr && sig) {
      if (!ADDR.test(addr)) return { kind: "invalid", reason: "malformed agent address" };
      const agentAddress = addr.toLowerCase();
      const ok = await verifyAgentAttestation({ userId: uid, agentAddress, signatureB64: sig, publicKeySpkiB64: o.publicKeySpkiB64 });
      if (!ok) return { kind: "invalid", reason: "the agent address is not attested by aijalon's executor" };
      return { kind: "ready", agentAddress, signatureB64: sig };
    }
    if (now() + interval > deadline) return { kind: "timeout" };
    o.onWaiting?.(polls);
    await sleep(interval);
  }
}
