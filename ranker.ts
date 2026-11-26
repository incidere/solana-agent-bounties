import { createHash } from "node:crypto";
import { readFileSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";

/** Raw protocol record ingested from an offline multi-chain DeFi snapshot. */
export interface RawProtocolRecord {
  id?: string | null;
  slug?: string | null;
  defillamaId?: string | number | null;
  name?: string | null;
  category?: string | null;
  chains?: unknown;
  tvl_usd?: number | string | null;
  tvl_7d_delta?: number | string | null;
  fees_30d_usd?: number | string | null;
  revenue_30d_usd?: number | string | null;
  symbol?: string | null;
  parent_protocol?: string | null;
  governance_links?: unknown;
  description?: string | null;
  mcap?: number | string | null;
  fdv?: number | string | null;
  gecko_id?: string | null;
}

export interface NormalizedProtocol {
  canonical_id: string;
  name: string;
  name_key: string;
  category: string;
  canonical_sector: CanonicalSector;
  chains: string[];
  tvl_usd: number;
  tvl_7d_delta: number;
  fees_30d_usd: number;
  revenue_30d_usd: number;
  symbol: string;
  normalized_symbol: string;
  parent_protocol: string;
  parent_key: string;
  governance_links: string[];
  description: string;
  mcap: number;
  fdv: number;
  gecko_id: string;
}

export type CanonicalSector =
  | "dex"
  | "perps"
  | "lending"
  | "cdp"
  | "yield"
  | "bridge_infra"
  | "infra_oracle"
  | "prediction"
  | "rwa"
  | "launchpad"
  | "defi_general";

export interface ClassificationResult {
  keep: boolean;
  stage: "kept" | "excluded_derivative" | "excluded_live_token" | "excluded_invalid";
  reason: string;
}

export interface DeduplicatedEcosystem extends NormalizedProtocol {
  deduped_children_count: number;
  deduped_children_names: string[];
}

export interface RankedProtocolEntry {
  rank: number;
  id: string;
  name: string;
  category: string;
  canonical_sector: CanonicalSector;
  chains: string[];
  tvl_usd: number;
  effective_tvl_usd: number;
  tvl_7d_delta: number;
  clamped_tvl_7d_delta: number;
  fees_30d_usd: number;
  revenue_30d_usd: number;
  annualized_fees_usd: number;
  annualized_revenue_usd: number;
  capital_efficiency_ratio: number;
  implied_fdv_low_usd: number;
  implied_fdv_mid_usd: number;
  implied_fdv_high_usd: number;
  score: number;
  deduped_children_count: number;
  clamped_flags: string[];
  playbook: string[];
}

export interface BoardOutput {
  schema_version: string;
  summary: {
    input_count: number;
    kept_pre_dedup_count: number;
    excluded_live_token_count: number;
    excluded_derivative_count: number;
    excluded_invalid_count: number;
    deduped_child_count: number;
    ranked_count: number;
    board_row_bounds: [number, number];
    clamped_outlier_count: number;
    input_sha256: string;
  };
  entries: RankedProtocolEntry[];
  exclusion_summary: Record<string, number>;
}

const MAX_BOARD_ROWS = 4000;
const MIN_TARGET_BOARD_ROWS = 100;

const HOMOGLYPH_MAP: Record<string, string> = {
  "\u0410": "A", // Cyrillic A
  "\u0430": "a",
  "\u0412": "B", // Cyrillic Ve
  "\u0421": "C", // Cyrillic Es
  "\u0441": "c",
  "\u0415": "E", // Cyrillic Ie
  "\u0435": "e",
  "\u041d": "H", // Cyrillic En
  "\u0406": "I", // Cyrillic I
  "\u0456": "i",
  "\u041a": "K", // Cyrillic Ka
  "\u043a": "k",
  "\u041c": "M", // Cyrillic Em
  "\u041e": "O", // Cyrillic O
  "\u043e": "o",
  "\u0420": "P", // Cyrillic Er
  "\u0440": "p",
  "\u0422": "T", // Cyrillic Te
  "\u0425": "X", // Cyrillic Kha
  "\u0445": "x",
  "\u0423": "Y", // Cyrillic U
  "\u0391": "A", // Greek Alpha
  "\u0392": "B", // Greek Beta
  "\u0395": "E", // Greek Epsilon
  "\u0396": "Z", // Greek Zeta
  "\u0397": "H", // Greek Eta
  "\u0399": "I", // Greek Iota
  "\u039a": "K", // Greek Kappa
  "\u039c": "M", // Greek Mu
  "\u039d": "N", // Greek Nu
  "\u039f": "O", // Greek Omicron
  "\u03a1": "P", // Greek Rho
  "\u03a4": "T", // Greek Tau
  "\u03a7": "X", // Greek Chi
};

const EMPTY_SYMBOL_PLACEHOLDERS = new Set([
  "",
  "-",
  "--",
  "n/a",
  "na",
  "none",
  "null",
  "nil",
  "tbd",
  "soon",
  "pre",
  "pretoken",
  "pre-token",
  "tokenless",
  "notoken",
  "no-token",
  "points",
  "unknown",
]);

const KNOWN_LIVE_TICKERS = new Set([
  "UNI", "AAVE", "LDO", "MKR", "CRV", "CVX", "SNX", "COMP", "GMX", "GNS",
  "DYDX", "PENDLE", "EIGEN", "ENA", "ETHFI", "JUP", "JTO", "KMNO", "RAY",
  "ORCA", "DRIFT", "PYTH", "W", "ZRO", "ZK", "STRK", "ARB", "OP", "BLUR",
  "TIA", "SUI", "APT", "MORPHO", "USUAL", "ONDO", "RPL", "SWISE", "FXS",
  "BAL", "SUSHI", "1INCH", "CAKE", "RUNE", "INJ", "SEI", "BERA", "HYPE",
  "USDT", "USDC", "DAI", "USDE", "USDD", "USDT0", "FDUSD", "TUSD", "FRAX",
  "PYUSD", "CRVUSD", "GHO", "LUSD", "AERO", "VELO", "CAKE", "YFI", "KNC",
  "BNT", "LRC", "STG", "RDNT", "GRAIL", "DPX", "JOE", "QI", "AVAX", "SOL",
  "ETH", "BTC", "BNB", "MATIC", "POL", "FTM", "SONIC", "TON", "NEAR",
]);

const WRAPPER_ASSET_CORE_RE =
  /^(?:w|st|wst|r|cb|wb|we|ez|rs|puf|uni|m|jito|b|ls|s|x|a|c|y|k|v|n|pt|yt|lp|stk|o|d|i)[-_.]?(?:eth|btc|sol|usdc|usdt|dai|usd|avax|bnb|arb|op|tia|sui|near|matic|ton|ada|xrp)\d*$/i;

const KNOWN_DISGUISED_STABLE_RE = /\b(?:usdt0|usdc\.e|usdbc|axlusdc|lzusdt|usdd|dai\.e|weth\.e)\b/i;

const DERIVATIVE_CATEGORIES: Array<[RegExp, string]> = [
  [/\b(?:liquid[\s-]?restak(?:ed|ing)|restak(?:ed|ing)|lrt)\b/i, "derivative_restaked"],
  [/\b(?:liquid[\s-]?stak(?:ed|ing)|lsd|lst)\b/i, "derivative_liquid_staked"],
  [/\b(?:wrapped|wrapped[\s-]?assets?|wrapped[\s-]?tokens?)\b/i, "derivative_wrapped"],
  [/\b(?:bridged|bridge[\s-]?wrapper|canonical[\s-]?bridge[\s-]?token)\b/i, "derivative_bridged"],
  [/\b(?:lp[\s-]?receipt|vault[\s-]?receipt|receipt[\s-]?token|erc-?4626[\s-]?wrapper)\b/i, "derivative_lp_vault_receipt"],
  [/\b(?:forked[\s-]?single-?asset[\s-]?wrapper|single-?asset[\s-]?wrapper|synthetic[\s-]?wrapper)\b/i, "derivative_forked_wrapper"],
];

const SECTOR_PRIORS: Record<CanonicalSector, { tvlMult: number; feeMult: number; revMult: number }> = {
  dex: { tvlMult: 1.4, feeMult: 16.0, revMult: 38.0 },
  perps: { tvlMult: 2.2, feeMult: 20.0, revMult: 42.0 },
  lending: { tvlMult: 1.1, feeMult: 14.0, revMult: 32.0 },
  cdp: { tvlMult: 1.0, feeMult: 12.0, revMult: 28.0 },
  yield: { tvlMult: 0.75, feeMult: 11.0, revMult: 25.0 },
  bridge_infra: { tvlMult: 1.9, feeMult: 21.0, revMult: 44.0 },
  infra_oracle: { tvlMult: 2.0, feeMult: 22.0, revMult: 45.0 },
  prediction: { tvlMult: 2.4, feeMult: 24.0, revMult: 48.0 },
  rwa: { tvlMult: 1.3, feeMult: 15.0, revMult: 34.0 },
  launchpad: { tvlMult: 1.25, feeMult: 15.0, revMult: 32.0 },
  defi_general: { tvlMult: 1.1, feeMult: 14.0, revMult: 30.0 },
};

/** Normalize Unicode, strip zero-width invisible characters, and collapse whitespace. */
export function normalizeText(value: unknown): string {
  if (typeof value !== "string") {
    return "";
  }
  return value
    .normalize("NFKC")
    .replace(/[\u200B-\u200F\u202A-\u202E\u2060\uFEFF\u00AD]/g, "")
    .replace(/\s+/g, " ")
    .trim();
}

/** Decode common Unicode homoglyphs so lookalike tickers cannot bypass filters. */
export function normalizeHomoglyphs(value: string): string {
  let out = "";
  for (const ch of value.normalize("NFKC")) {
    out += HOMOGLYPH_MAP[ch] ?? ch;
  }
  return out;
}

/** Convert an identifier or protocol name to a deterministic lowercase slug. */
export function canonicalSlug(value: string): string {
  return normalizeHomoglyphs(normalizeText(value))
    .replace(/^parent#/i, "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
}

/** Parse finite numbers cleanly and normalize -0 to 0. */
export function safeNumber(value: unknown, fallback = 0): number {
  if (typeof value === "number") {
    if (!Number.isFinite(value)) return fallback;
    return Object.is(value, -0) ? 0 : value;
  }
  if (typeof value === "string") {
    const cleaned = value.trim().replace(/[$,_]/g, "");
    if (!cleaned) return fallback;
    const parsed = Number(cleaned);
    if (!Number.isFinite(parsed)) return fallback;
    return Object.is(parsed, -0) ? 0 : parsed;
  }
  return fallback;
}

/** Round to fixed decimal places deterministically, normalizing -0 to 0. */
export function roundFixed(value: number, decimals: number): number {
  if (!Number.isFinite(value)) return 0;
  const factor = 10 ** decimals;
  const rounded = Math.round((value + Number.EPSILON) * factor) / factor;
  return Object.is(rounded, -0) || rounded === 0 ? 0 : rounded;
}

export function clamp(value: number, low: number, high: number): number {
  if (value <= low) return low;
  if (value >= high) return high;
  return value;
}

/** Compute deterministic linear-interpolated quantile over a pre-sorted array. */
export function quantileSorted(sorted: number[], q: number): number {
  if (sorted.length === 0) return 0;
  if (sorted.length === 1) return sorted[0];
  const pos = clamp(q, 0, 1) * (sorted.length - 1);
  const lo = Math.floor(pos);
  const hi = Math.ceil(pos);
  if (lo === hi) return sorted[lo];
  const frac = pos - lo;
  return sorted[lo] * (1 - frac) + sorted[hi] * frac;
}

function normalizeStringArray(value: unknown): string[] {
  const items: string[] = [];
  if (Array.isArray(value)) {
    for (const entry of value) {
      const s = normalizeText(typeof entry === "string" ? entry : String(entry ?? ""));
      if (s) items.push(s);
    }
  } else if (typeof value === "string") {
    for (const part of value.split(",")) {
      const s = normalizeText(part);
      if (s) items.push(s);
    }
  }
  return Array.from(new Set(items)).sort((a, b) => (a < b ? -1 : a > b ? 1 : 0));
}

function normalizeSymbolToken(rawSymbol: string): string {
  const cleaned = normalizeHomoglyphs(normalizeText(rawSymbol))
    .replace(/[\s$[\](){}*`"'_]/g, "")
    .trim();
  if (!cleaned) return "";
  const lower = cleaned.toLowerCase();
  if (EMPTY_SYMBOL_PLACEHOLDERS.has(lower)) return "";
  if (lower.startsWith("ticker:")) {
    return cleaned.slice(7).toUpperCase();
  }
  return cleaned;
}

/** Map free-form category labels into canonical scoring/playbook sectors. */
export function mapCanonicalSector(category: string): CanonicalSector {
  const c = normalizeText(category).toLowerCase();
  if (/\b(perp|perpetual|derivatives|futures|options)\b/.test(c)) return "perps";
  if (/\b(dex|amm|swap|liquidity pool|clmm|orderbook)\b/.test(c)) return "dex";
  if (/\b(lend|borrow|money market|credit|undercollateralized)\b/.test(c)) return "lending";
  if (/\b(cdp|stablecoin|minting)\b/.test(c)) return "cdp";
  if (/\b(yield|farm|vault|aggregator|optimizer)\b/.test(c)) return "yield";
  if (/\b(bridge|cross-chain|interop|messaging)\b/.test(c)) return "bridge_infra";
  if (/\b(oracle|infra|mev|intent|rpc|indexer|rollup|sequencer|avs|shared security)\b/.test(c)) return "infra_oracle";
  if (/\b(prediction|forecast|betting)\b/.test(c)) return "prediction";
  if (/\b(rwa|treasury|real world)\b/.test(c)) return "rwa";
  if (/\b(launchpad|ido|auction)\b/.test(c)) return "launchpad";
  return "defi_general";
}

/** Normalize raw protocol fields into canonical deterministic form. */
export function normalizeRecord(raw: RawProtocolRecord, indexFallback: number): NormalizedProtocol {
  const name = normalizeHomoglyphs(normalizeText(raw.name ?? ""));
  const nameKey = canonicalSlug(name);
  const rawParent = normalizeText(raw.parent_protocol ?? "");
  const parentKey = rawParent && !EMPTY_SYMBOL_PLACEHOLDERS.has(rawParent.toLowerCase())
    ? canonicalSlug(rawParent)
    : "";
  const idSeed = normalizeText(
    raw.id ?? raw.slug ?? (raw.defillamaId !== undefined && raw.defillamaId !== null ? String(raw.defillamaId) : "")
  );
  const canonicalId = canonicalSlug(idSeed) || nameKey || `protocol-${indexFallback}`;
  const category = normalizeText(raw.category ?? "DeFi") || "DeFi";
  const symbol = normalizeText(raw.symbol ?? "");
  const normalizedSymbol = normalizeSymbolToken(symbol);
  const description = normalizeHomoglyphs(normalizeText(raw.description ?? ""));

  return {
    canonical_id: canonicalId,
    name,
    name_key: nameKey,
    category,
    canonical_sector: mapCanonicalSector(category),
    chains: normalizeStringArray(raw.chains),
    tvl_usd: Math.max(0, safeNumber(raw.tvl_usd, 0)),
    tvl_7d_delta: safeNumber(raw.tvl_7d_delta, 0),
    fees_30d_usd: Math.max(0, safeNumber(raw.fees_30d_usd, 0)),
    revenue_30d_usd: Math.max(0, safeNumber(raw.revenue_30d_usd, 0)),
    symbol,
    normalized_symbol: normalizedSymbol,
    parent_protocol: rawParent,
    parent_key: parentKey,
    governance_links: normalizeStringArray(raw.governance_links),
    description,
    mcap: Math.max(0, safeNumber(raw.mcap, 0)),
    fdv: Math.max(0, safeNumber(raw.fdv, 0)),
    gecko_id: normalizeText(raw.gecko_id ?? ""),
  };
}

function detectDerivativeReason(p: NormalizedProtocol): string | null {
  const cat = p.category.toLowerCase();
  for (const [re, reason] of DERIVATIVE_CATEGORIES) {
    if (re.test(cat)) return reason;
  }

  const sym = p.normalized_symbol;
  if (sym && (WRAPPER_ASSET_CORE_RE.test(sym) || /^(?:lp|pt|yt)[-_]/i.test(sym))) {
    if (/^(?:ez|rs|puf|we)eth$/i.test(sym)) {
      return "derivative_restaked";
    }
    if (/^(?:(?:st|wst|ls|m|jito|b|r|cb)(?:eth|sol)|(?:st|wst|ls)[a-z]+)$/i.test(sym)) {
      return "derivative_liquid_staked";
    }
    if (/^(?:lp|pt|yt|a|c|y|v|stk)[-_]?/i.test(sym)) {
      return "derivative_lp_vault_receipt";
    }
    return "derivative_wrapped";
  }

  const combined = `${p.name} ${p.description}`;
  if (KNOWN_DISGUISED_STABLE_RE.test(`${p.name} ${p.symbol} ${p.description}`)) {
    return "derivative_bridged";
  }
  if (/^(?:wrapped|staked|liquid staked|restaked|bridged)\s+/i.test(p.name)) {
    const head = p.name.split(/\s+/)[0].toLowerCase();
    if (head === "bridged") return "derivative_bridged";
    if (head === "restaked") return "derivative_restaked";
    if (head === "staked" || head === "liquid") return "derivative_liquid_staked";
    return "derivative_wrapped";
  }

  // Strip negated phrases before checking derivative keywords in prose description.
  const positiveText = combined
    .replace(/\b(?:not|no|never|without|non)[-\s]+(?:a\s+)?(?:wrapped|wrapper|bridged|liquid[-\s]?staked|restaked|derivative|receipt)\b/gi, "")
    .toLowerCase();

  if (/\b(?:liquid[\s-]?restak(?:ed|ing)\s+(?:token|wrapper|receipt|derivative)|lrt\s+wrapper)\b/.test(positiveText)) {
    return "derivative_restaked";
  }
  if (/\b(?:liquid[\s-]?stak(?:ed|ing)\s+(?:token|wrapper|derivative|eth|sol|btc)|lst\s+wrapper)\b/.test(positiveText)) {
    return "derivative_liquid_staked";
  }
  if (/\b(?:lp\s+receipt|vault\s+receipt(?:\s+token)?|erc-?4626\s+(?:wrapper|receipt|share)|deposit\s+receipt\s+token|pendle\s+(?:pt|yt)\s+wrapper)\b/.test(positiveText)) {
    return "derivative_lp_vault_receipt";
  }
  if (/\b(?:forked\s+single-?asset\s+wrapper|single-?asset\s+wrapper|1:1\s+(?:backed|pegged)\s+wrapper)\b/.test(positiveText)) {
    return "derivative_forked_wrapper";
  }
  if (/\b(?:bridged\s+(?:usdc|usdt|dai|eth|btc|stablecoin|token|asset)|canonical\s+bridge\s+(?:wrapper|token))\b/.test(positiveText)) {
    return "derivative_bridged";
  }
  if (/\b(?:wrapped\s+(?:bitcoin|ether|eth|btc|sol|avax|token|asset))\b/.test(positiveText)) {
    return "derivative_wrapped";
  }

  return null;
}

function detectLiveTokenReason(p: NormalizedProtocol): string | null {
  if (p.mcap > 0 || p.fdv > 0 || p.gecko_id.length > 0) {
    return "live_token_market_data";
  }

  const sym = p.normalized_symbol;
  if (sym.length > 0) {
    const compact = sym.replace(/[-_.]/g, "").toUpperCase();
    if (KNOWN_LIVE_TICKERS.has(compact)) {
      return "live_token_known_ticker";
    }
    if (/^[A-Z0-9]{2,14}$/.test(compact)) {
      return "live_token_symbol";
    }
    return "live_token_disguised_symbol";
  }

  for (const link of p.governance_links) {
    const l = link.toLowerCase();
    if (
      /\b(?:snapshot\.org|tally\.xyz|boardroom\.io|coingecko\.com\/en\/coins|coinmarketcap\.com\/currencies)\b/.test(l) ||
      /etherscan\.io\/token\//.test(l)
    ) {
      return "live_token_governance_link";
    }
  }

  const cleanedDesc = p.description
    .replace(
      /\b(?:no|without|prior\s+to|before|pre)[-\s]+(?:a\s+)?(?:live\s+)?(?:governance\s+|utility\s+)?tokens?\b/gi,
      ""
    )
    .replace(/\b(?:tokenless|pre-?token|pre-?tge)\b/gi, "");

  if (
    /\b(?:live\s+(?:governance|utility)\s+token|token\s+(?:is\s+)?live|tge\s+completed|trading\s+under\s+ticker|native\s+token\s+\$[a-z0-9]{2,10})\b/i.test(
      cleanedDesc
    ) ||
    /\(\s*\$[A-Z0-9]{2,10}\s*\)/.test(p.description) ||
    /\bticker\s*[:=]\s*\$?[A-Z0-9]{2,10}\b/i.test(p.description)
  ) {
    return "live_token_description";
  }

  return null;
}

/** Deterministically classify a protocol as kept base protocol or excluded derivative/tokenized. */
export function classifyProtocol(p: NormalizedProtocol): ClassificationResult {
  if (!p.name || !p.name_key) {
    return { keep: false, stage: "excluded_invalid", reason: "invalid_missing_name" };
  }
  const derivReason = detectDerivativeReason(p);
  if (derivReason) {
    return { keep: false, stage: "excluded_derivative", reason: derivReason };
  }
  const liveReason = detectLiveTokenReason(p);
  if (liveReason) {
    return { keep: false, stage: "excluded_live_token", reason: liveReason };
  }
  return { keep: true, stage: "kept", reason: "genuine_pre_token_base" };
}

/** Collapse parent-child ecosystems without parent-child double counting or child leaks from tokenized parents. */
export function deduplicateParentChild(
  kept: NormalizedProtocol[],
  excludedParentKeys: Set<string>,
  exclusionSummary: Record<string, number>
): { ecosystems: DeduplicatedEcosystem[]; dedupedChildCount: number } {
  const groups = new Map<string, NormalizedProtocol[]>();

  for (const item of kept) {
    if (item.parent_key && excludedParentKeys.has(item.parent_key)) {
      exclusionSummary["parent_has_live_token_or_derivative"] =
        (exclusionSummary["parent_has_live_token_or_derivative"] ?? 0) + 1;
      continue;
    }
    const groupKey = item.parent_key || item.name_key;
    const list = groups.get(groupKey);
    if (list) {
      list.push(item);
    } else {
      groups.set(groupKey, [item]);
    }
  }

  const ecosystems: DeduplicatedEcosystem[] = [];
  let dedupedChildCount = 0;

  const sortedGroupKeys = Array.from(groups.keys()).sort((a, b) => (a < b ? -1 : a > b ? 1 : 0));
  for (const groupKey of sortedGroupKeys) {
    const members = groups.get(groupKey)!;
    // Deterministic member ordering: root record first, then highest TVL, shortest name, canonical_id ASC.
    members.sort((a, b) => {
      const aRoot = a.parent_key === "" ? 1 : 0;
      const bRoot = b.parent_key === "" ? 1 : 0;
      if (aRoot !== bRoot) return bRoot - aRoot;
      if (a.tvl_usd !== b.tvl_usd) return b.tvl_usd - a.tvl_usd;
      if (a.name.length !== b.name.length) return a.name.length - b.name.length;
      return a.canonical_id < b.canonical_id ? -1 : a.canonical_id > b.canonical_id ? 1 : 0;
    });

    const primary = members[0];
    const others = members.slice(1);
    dedupedChildCount += others.length;

    const roots = members.filter((m) => m.parent_key === "");
    const children = members.filter((m) => m.parent_key !== "");

    // Prevent parent-child double counting:
    // Root parent entries typically already subsume child TVL/fees; across disjoint single-chain children
    // take the max per chain signature, and merge parent vs children via max() rather than sum().
    const chainMaxTvl = new Map<string, number>();
    const chainMaxFees = new Map<string, number>();
    const chainMaxRev = new Map<string, number>();

    for (const c of children) {
      const sig = c.chains.length === 1 ? `chain:${c.chains[0].toLowerCase()}` : `multi:${groupKey}`;
      chainMaxTvl.set(sig, Math.max(chainMaxTvl.get(sig) ?? 0, c.tvl_usd));
      chainMaxFees.set(sig, Math.max(chainMaxFees.get(sig) ?? 0, c.fees_30d_usd));
      chainMaxRev.set(sig, Math.max(chainMaxRev.get(sig) ?? 0, c.revenue_30d_usd));
    }

    const disjointChildTvl = Array.from(chainMaxTvl.values()).reduce((acc, v) => acc + v, 0);
    const disjointChildFees = Array.from(chainMaxFees.values()).reduce((acc, v) => acc + v, 0);
    const disjointChildRev = Array.from(chainMaxRev.values()).reduce((acc, v) => acc + v, 0);

    const rootTvl = roots.reduce((acc, r) => Math.max(acc, r.tvl_usd), 0);
    const rootFees = roots.reduce((acc, r) => Math.max(acc, r.fees_30d_usd), 0);
    const rootRev = roots.reduce((acc, r) => Math.max(acc, r.revenue_30d_usd), 0);

    const mergedTvl = Math.max(rootTvl, disjointChildTvl);
    const mergedFees = Math.max(rootFees, disjointChildFees);
    const mergedRev = Math.max(rootRev, disjointChildRev);

    const chainUnion = Array.from(
      new Set(members.flatMap((m) => m.chains))
    ).sort((a, b) => (a < b ? -1 : a > b ? 1 : 0));

    const govUnion = Array.from(
      new Set(members.flatMap((m) => m.governance_links))
    ).sort((a, b) => (a < b ? -1 : a > b ? 1 : 0));

    ecosystems.push({
      ...primary,
      chains: chainUnion,
      governance_links: govUnion,
      tvl_usd: mergedTvl,
      fees_30d_usd: mergedFees,
      revenue_30d_usd: mergedRev,
      deduped_children_count: others.length,
      deduped_children_names: others.map((o) => o.name).sort((a, b) => (a < b ? -1 : a > b ? 1 : 0)),
    });
  }

  return { ecosystems, dedupedChildCount };
}

/** Build a structured 3..8 step on-chain interaction playbook tailored to the protocol sector. */
export function buildCategoryPlaybook(p: DeduplicatedEcosystem, clampedFlags: string[]): string[] {
  const chainLabel = p.chains.length > 0 ? p.chains.slice(0, 3).join(", ") : "target mainnet";
  const caution =
    clampedFlags.length > 0
      ? "Verify organic TVL/fee continuity before committing size because snapshot flagged extreme spike/outlier metrics."
      : "Confirm audited contract addresses and current epoch/points rules from official docs before depositing.";

  const commonTail = [
    caution,
    "Track cumulative protocol fees generated vs wallet capital to maintain organic non-sybil activity.",
  ];

  switch (p.canonical_sector) {
    case "dex":
      return [
        `Inspect ${p.name} audited router and pool factory contracts on ${chainLabel}.`,
        "Execute multi-week spot swaps across core liquid pairs during moderate gas windows.",
        "Supply concentrated or balanced LP liquidity within active tick bands where fees accrue organically.",
        "Rebalance range positions periodically instead of rapid enter-exit wash deposits.",
        ...commonTail,
      ];
    case "perps":
      return [
        `Verify ${p.name} margin vault, oracle feed, and liquidation engine parameters on ${chainLabel}.`,
        "Deposit margin collateral and open low-leverage directional or delta-neutral positions.",
        "Generate consistent taker/maker fee volume across multiple weekly epochs.",
        "Contribute liquidity to insurance or market-making vaults only with bounded downside sizing.",
        ...commonTail,
      ];
    case "lending":
      return [
        `Audit ${p.name} reserve risk oracle, LTV caps, and utilization curves on ${chainLabel}.`,
        "Supply blue-chip collateral assets into isolated or core lending pools.",
        "Borrow conservative amounts (<40% LTV) only when net borrow APR or incentive spread is justified.",
        "Monitor health factor across volatile windows to avoid liquidation penalties.",
        ...commonTail,
      ];
    case "cdp":
      return [
        `Review ${p.name} collateralization ratios, stability pool mechanics, and peg stability modules on ${chainLabel}.`,
        "Open a conservatively collateralized vault (>200% CR) using productive collateral.",
        "Deploy minted stable units into approved stability pools or native DEX pairs.",
        ...commonTail,
      ];
    case "yield":
      return [
        `Check ${p.name} strategy contracts, underlying adapters, and withdrawal queue depth on ${chainLabel}.`,
        "Deposit into base native yield strategies while avoiding recursive unbacked leverage loops.",
        "Hold positions across full reward settlement epochs rather than intra-block farming.",
        ...commonTail,
      ];
    case "prediction":
      return [
        `Inspect ${p.name} market resolution oracle and dispute window rules on ${chainLabel}.`,
        "Provide limit-order depth on high-volume active prediction markets.",
        "Settle resolved positions consistently across distinct event categories.",
        ...commonTail,
      ];
    case "bridge_infra":
    case "infra_oracle":
      return [
        `Validate ${p.name} canonical endpoint, message verification thresholds, and relayer security on ${chainLabel}.`,
        "Route genuine cross-chain transfers or intent settlements across non-trivial ticket sizes.",
        "Test native verification or solver-assisted execution across at least two connected networks.",
        ...commonTail,
      ];
    case "rwa":
      return [
        `Confirm ${p.name} custodian attestations, underlying T-bill/credit maturity, and redemption SLA on ${chainLabel}.`,
        "Mint or subscribe to primary RWA instruments through verified compliance rails where permitted.",
        "Deploy yield-bearing RWA balances into whitelisted DeFi collateral venues.",
        ...commonTail,
      ];
    default:
      return [
        `Review ${p.name} smart-contract audits, multisig timelock, and fee schedule on ${chainLabel}.`,
        "Interact with core protocol primitives using staggered weekly transactions and durable positions.",
        "Hold net liquidity through full snapshot epochs to qualify as organic ecosystem participation.",
        ...commonTail,
      ];
  }
}

/** Score, clamp wash-TVL outliers, estimate implied launch FDV bands, and produce final ranked board entries. */
export function scoreAndRank(ecosystems: DeduplicatedEcosystem[]): {
  entries: RankedProtocolEntry[];
  clampedOutlierCount: number;
} {
  if (ecosystems.length === 0) {
    return { entries: [], clampedOutlierCount: 0 };
  }

  const allTvlsSorted = ecosystems.map((e) => e.tvl_usd).sort((a, b) => a - b);
  const globalP95Tvl = quantileSorted(allTvlsSorted, 0.95);
  const globalP99Tvl = quantileSorted(allTvlsSorted, 0.99);

  // Per-sector TVL and fee-yield quantiles.
  const sectorTvls = new Map<CanonicalSector, number[]>();
  const sectorFeeYields = new Map<CanonicalSector, number[]>();
  const sectorRevMultRatios = new Map<CanonicalSector, number[]>();

  for (const e of ecosystems) {
    const tList = sectorTvls.get(e.canonical_sector) ?? [];
    tList.push(e.tvl_usd);
    sectorTvls.set(e.canonical_sector, tList);

    if (e.tvl_usd >= 10_000) {
      const fy = (e.fees_30d_usd * 12) / e.tvl_usd;
      const fList = sectorFeeYields.get(e.canonical_sector) ?? [];
      fList.push(fy);
      sectorFeeYields.set(e.canonical_sector, fList);
    }

    if (e.revenue_30d_usd > 0 && e.fees_30d_usd > 0) {
      const rList = sectorRevMultRatios.get(e.canonical_sector) ?? [];
      rList.push(clamp(e.revenue_30d_usd / e.fees_30d_usd, 0, 1));
      sectorRevMultRatios.set(e.canonical_sector, rList);
    }
  }

  for (const list of sectorTvls.values()) list.sort((a, b) => a - b);
  for (const list of sectorFeeYields.values()) list.sort((a, b) => a - b);
  for (const list of sectorRevMultRatios.values()) list.sort((a, b) => a - b);

  interface ProvisionalRow {
    eco: DeduplicatedEcosystem;
    effectiveTvl: number;
    clampedDelta: number;
    annFees: number;
    annRev: number;
    capEffRatio: number;
    sizeScore: number;
    capEffScore: number;
    momentumScore: number;
    fdvLow: number;
    fdvMid: number;
    fdvHigh: number;
    clampedFlags: string[];
  }

  const provisional: ProvisionalRow[] = [];
  let clampedOutlierCount = 0;

  for (const eco of ecosystems) {
    const flags: string[] = [];
    const sTvls = sectorTvls.get(eco.canonical_sector) ?? allTvlsSorted;
    const sP95Tvl = sTvls.length >= 5 ? quantileSorted(sTvls, 0.95) : globalP95Tvl;
    const sP95FeeYieldList = sectorFeeYields.get(eco.canonical_sector) ?? [];
    const sP95FeeYield =
      sP95FeeYieldList.length >= 4
        ? clamp(quantileSorted(sP95FeeYieldList, 0.95), 0.15, 2.5)
        : 1.5;

    let clampedDelta = eco.tvl_7d_delta;
    if (clampedDelta < -0.85 || clampedDelta > 2.0) {
      flags.push("delta_spike_clamped");
      clampedDelta = clamp(clampedDelta, -0.85, 2.0);
    }

    let rawAnnFees = eco.fees_30d_usd * 12;
    let rawAnnRev = eco.revenue_30d_usd * 12;

    if (rawAnnFees > 0 && rawAnnRev > rawAnnFees) {
      flags.push("revenue_exceeds_fees_clamped");
      rawAnnRev = rawAnnFees;
    }

    let effectiveTvl = eco.tvl_usd;
    const rawFeeYield = effectiveTvl > 0 ? rawAnnFees / effectiveTvl : 0;

    // Wash-TVL spike guard: sudden >200% delta with zero fees OR enormous TVL with negligible (<0.1%) fee generation.
    const washSpike =
      (eco.tvl_7d_delta > 2.0 && eco.fees_30d_usd <= 0 && effectiveTvl > 100_000) ||
      (effectiveTvl > Math.max(sP95Tvl * 1.5, 5_000_000) && rawFeeYield < 0.001);

    if (washSpike) {
      flags.push("wash_tvl_clamped");
      const capTvl = Math.max(100_000, sP95Tvl > 0 ? sP95Tvl : globalP95Tvl);
      effectiveTvl = Math.min(effectiveTvl, capTvl);
      clampedDelta = Math.min(clampedDelta, 0.35);
    } else if (globalP99Tvl > 0 && effectiveTvl > globalP99Tvl * 1.5) {
      flags.push("extreme_tvl_p99_clamped");
      effectiveTvl = globalP99Tvl * 1.25;
    }

    // Wash-fee anomaly guard against micro-TVL inflating fee multiples.
    let effectiveAnnFees = rawAnnFees;
    const maxPlausibleFees = Math.max(50_000, effectiveTvl * Math.max(sP95FeeYield, 2.0));
    if (effectiveAnnFees > maxPlausibleFees) {
      flags.push("wash_fee_multiple_clamped");
      effectiveAnnFees = maxPlausibleFees;
      rawAnnRev = Math.min(rawAnnRev, effectiveAnnFees);
    }

    if (flags.length > 0) clampedOutlierCount += 1;

    const prior = SECTOR_PRIORS[eco.canonical_sector];
    const tvlImpliedFdv = effectiveTvl * prior.tvlMult;
    const flowImpliedFdv =
      rawAnnRev > 0
        ? 0.6 * (rawAnnRev * prior.revMult) + 0.4 * (effectiveAnnFees * prior.feeMult)
        : effectiveAnnFees * prior.feeMult;

    let impliedMid = 0;
    if (flowImpliedFdv > 0 && effectiveTvl >= 10_000) {
      const cappedFlowFdv = tvlImpliedFdv > 0 ? Math.min(flowImpliedFdv, tvlImpliedFdv * 6.0) : flowImpliedFdv;
      impliedMid = 0.45 * tvlImpliedFdv + 0.55 * cappedFlowFdv;
    } else if (tvlImpliedFdv > 0) {
      impliedMid = tvlImpliedFdv * 0.7;
    } else {
      impliedMid = flowImpliedFdv * 0.5;
    }

    const fdvMid = roundFixed(Math.max(0, impliedMid), 2);
    const fdvLow = roundFixed(fdvMid * 0.65, 2);
    const fdvHigh = roundFixed(fdvMid * 1.65, 2);

    const capEffRatio = effectiveTvl > 0 ? effectiveAnnFees / effectiveTvl : 0;
    const sizeScore = clamp((Math.log10(1 + effectiveTvl) - 4) / 5, 0, 1);

    const boundedYield = effectiveAnnFees / Math.max(effectiveTvl, 25_000);
    const yieldScore = Math.log1p(boundedYield * 10) / Math.log1p(5.0);
    const captureRatio = effectiveAnnFees > 0 ? clamp(rawAnnRev / effectiveAnnFees, 0, 1) : 0;
    const plausibility = effectiveTvl >= 10_000 ? 1.0 : effectiveTvl / 10_000;
    const capEffScore = clamp((0.8 * yieldScore + 0.2 * captureRatio) * plausibility, 0, 1);
    const momentumScore = clamp((clampedDelta + 0.5) / 2.5, 0, 1);

    provisional.push({
      eco,
      effectiveTvl,
      clampedDelta,
      annFees: effectiveAnnFees,
      annRev: rawAnnRev,
      capEffRatio,
      sizeScore,
      capEffScore,
      momentumScore,
      fdvLow,
      fdvMid,
      fdvHigh,
      clampedFlags: flags.sort((a, b) => (a < b ? -1 : a > b ? 1 : 0)),
    });
  }

  // Compute deterministic sector-relative rank with Bayesian shrinkage n/(n+5) so tiny categories do not distort ranks.
  const sectorBuckets = new Map<CanonicalSector, ProvisionalRow[]>();
  for (const row of provisional) {
    const b = sectorBuckets.get(row.eco.canonical_sector) ?? [];
    b.push(row);
    sectorBuckets.set(row.eco.canonical_sector, b);
  }
  const sectorPercentiles = new Map<string, number>();
  for (const bucket of sectorBuckets.values()) {
    bucket.sort((a, b) => {
      const aBase = 0.55 * a.sizeScore + 0.45 * a.capEffScore;
      const bBase = 0.55 * b.sizeScore + 0.45 * b.capEffScore;
      if (aBase !== bBase) return aBase - bBase;
      return a.eco.canonical_id < b.eco.canonical_id ? -1 : a.eco.canonical_id > b.eco.canonical_id ? 1 : 0;
    });
    const n = bucket.length;
    const weight = n / (n + 5);
    for (let i = 0; i < n; i++) {
      const pct = n === 1 ? 0.5 : i / (n - 1);
      sectorPercentiles.set(bucket[i].eco.canonical_id, weight * pct + (1 - weight) * 0.5);
    }
  }

  const scored = provisional.map((row) => {
    const sectorPct = sectorPercentiles.get(row.eco.canonical_id) ?? 0.5;
    const composite =
      100 *
      (0.42 * row.sizeScore +
        0.33 * row.capEffScore +
        0.15 * sectorPct +
        0.10 * row.momentumScore);
    return {
      row,
      score: roundFixed(composite, 6),
    };
  });

  scored.sort((a, b) => {
    if (b.score !== a.score) return b.score - a.score;
    if (b.row.fdvMid !== a.row.fdvMid) return b.row.fdvMid - a.row.fdvMid;
    if (b.row.effectiveTvl !== a.row.effectiveTvl) return b.row.effectiveTvl - a.row.effectiveTvl;
    return a.row.eco.canonical_id < b.row.eco.canonical_id
      ? -1
      : a.row.eco.canonical_id > b.row.eco.canonical_id
      ? 1
      : 0;
  });

  const capped = scored.slice(0, MAX_BOARD_ROWS);
  const entries: RankedProtocolEntry[] = capped.map((item, idx) => {
    const { row, score } = item;
    return {
      rank: idx + 1,
      id: row.eco.canonical_id,
      name: row.eco.name,
      category: row.eco.category,
      canonical_sector: row.eco.canonical_sector,
      chains: row.eco.chains,
      tvl_usd: roundFixed(row.eco.tvl_usd, 2),
      effective_tvl_usd: roundFixed(row.effectiveTvl, 2),
      tvl_7d_delta: roundFixed(row.eco.tvl_7d_delta, 6),
      clamped_tvl_7d_delta: roundFixed(row.clampedDelta, 6),
      fees_30d_usd: roundFixed(row.eco.fees_30d_usd, 2),
      revenue_30d_usd: roundFixed(row.eco.revenue_30d_usd, 2),
      annualized_fees_usd: roundFixed(row.annFees, 2),
      annualized_revenue_usd: roundFixed(row.annRev, 2),
      capital_efficiency_ratio: roundFixed(row.capEffRatio, 6),
      implied_fdv_low_usd: row.fdvLow,
      implied_fdv_mid_usd: row.fdvMid,
      implied_fdv_high_usd: row.fdvHigh,
      score,
      deduped_children_count: row.eco.deduped_children_count,
      clamped_flags: row.clampedFlags,
      playbook: buildCategoryPlaybook(row.eco, row.clampedFlags),
    };
  });

  return { entries, clampedOutlierCount };
}

/** End-to-end deterministic pipeline from raw protocol records to serialized board payload. */
export function runPipeline(rawRecords: RawProtocolRecord[]): BoardOutput {
  const normalized = rawRecords.map((r, i) => normalizeRecord(r, i));

  // Canonicalize input order before classification/hash so input array permutation produces byte-identical output.
  const ordered = [...normalized].sort((a, b) => {
    if (a.canonical_id !== b.canonical_id) return a.canonical_id < b.canonical_id ? -1 : 1;
    if (a.name_key !== b.name_key) return a.name_key < b.name_key ? -1 : 1;
    return a.tvl_usd - b.tvl_usd;
  });

  const inputDigest = createHash("sha256").update(JSON.stringify(ordered)).digest("hex");

  const kept: NormalizedProtocol[] = [];
  const excludedParentKeys = new Set<string>();
  const exclusionSummary: Record<string, number> = {};
  let excludedLiveTokenCount = 0;
  let excludedDerivativeCount = 0;
  let excludedInvalidCount = 0;

  for (const item of ordered) {
    const cls = classifyProtocol(item);
    if (cls.keep) {
      kept.push(item);
    } else {
      exclusionSummary[cls.reason] = (exclusionSummary[cls.reason] ?? 0) + 1;
      if (item.name_key && item.parent_key === "") {
        excludedParentKeys.add(item.name_key);
      }
      if (cls.stage === "excluded_live_token") excludedLiveTokenCount += 1;
      else if (cls.stage === "excluded_derivative") excludedDerivativeCount += 1;
      else excludedInvalidCount += 1;
    }
  }

  const { ecosystems, dedupedChildCount } = deduplicateParentChild(
    kept,
    excludedParentKeys,
    exclusionSummary
  );

  // Sort exclusion summary keys deterministically.
  const sortedExclusionSummary: Record<string, number> = {};
  for (const key of Object.keys(exclusionSummary).sort()) {
    sortedExclusionSummary[key] = exclusionSummary[key];
  }

  const { entries, clampedOutlierCount } = scoreAndRank(ecosystems);

  return {
    schema_version: "1.0.0",
    summary: {
      input_count: rawRecords.length,
      kept_pre_dedup_count: kept.length,
      excluded_live_token_count: excludedLiveTokenCount,
      excluded_derivative_count: excludedDerivativeCount,
      excluded_invalid_count: excludedInvalidCount,
      deduped_child_count: dedupedChildCount,
      ranked_count: entries.length,
      board_row_bounds: [MIN_TARGET_BOARD_ROWS, MAX_BOARD_ROWS],
      clamped_outlier_count: clampedOutlierCount,
      input_sha256: inputDigest,
    },
    entries,
    exclusion_summary: sortedExclusionSummary,
  };
}

/** Serialize board output deterministically with fixed formatting and trailing LF. */
export function serializeBoard(board: BoardOutput): string {
  return `${JSON.stringify(board, null, 2)}\n`;
}

/** Generate a deterministic offline snapshot of N protocols (with tokens, wrappers, children, wash-TVL, and organic base protocols). */
export function generateSyntheticSnapshot(count: number): RawProtocolRecord[] {
  const sectors = ["Dexes", "Perpetuals", "Lending", "CDP", "Yield", "Oracle", "Prediction", "RWA"];
  const chainsPool = ["Solana", "Arbitrum", "Base", "Ethereum", "Sui", "HyperEVM"];
  const records: RawProtocolRecord[] = [];
  let seed = 0x5f3759df;
  const nextRand = (): number => {
    seed = (seed * 1664525 + 1013904223) >>> 0;
    return seed / 0x100000000;
  };

  for (let i = 0; i < count; i++) {
    const sector = sectors[i % sectors.length];
    const chain = chainsPool[i % chainsPool.length];
    const bucket = i % 10;

    if (bucket === 0 || bucket === 1) {
      // Live governance token protocol
      records.push({
        id: `live-${i}`,
        name: `LiveProto ${i}`,
        category: sector,
        chains: [chain],
        tvl_usd: Math.round(500_000 + nextRand() * 80_000_000),
        tvl_7d_delta: roundFixed((nextRand() - 0.4) * 0.4, 4),
        fees_30d_usd: Math.round(10_000 + nextRand() * 400_000),
        revenue_30d_usd: Math.round(3_000 + nextRand() * 120_000),
        symbol: `LIVE${i}`,
        parent_protocol: "",
        governance_links: ["https://snapshot.org/#/liveproto.eth"],
        description: `Live governance protocol with active token.`,
      });
    } else if (bucket === 2) {
      // Derivative wrapper
      records.push({
        id: `deriv-${i}`,
        name: i % 2 === 0 ? `Wrapped SOL Vault ${i}` : `Liquid Staked ETH ${i}`,
        category: i % 2 === 0 ? "Wrapped" : "Liquid Staking",
        chains: [chain],
        tvl_usd: Math.round(2_000_000 + nextRand() * 50_000_000),
        tvl_7d_delta: 0.02,
        fees_30d_usd: 12000,
        revenue_30d_usd: 4000,
        symbol: i % 2 === 0 ? "wSOL" : "stETH",
        parent_protocol: "",
        governance_links: [],
        description: "Wrapper receipt token tracking underlying staked asset.",
      });
    } else if (bucket === 3 && i >= 10) {
      // Child deployment of an earlier genuine parent
      const parentIdx = i - (i % 10) + 4;
      records.push({
        id: `base-${parentIdx}-${chain.toLowerCase()}`,
        name: `FrontierBase ${parentIdx} ${chain}`,
        category: sectors[parentIdx % sectors.length],
        chains: [chain],
        tvl_usd: Math.round(200_000 + nextRand() * 4_000_000),
        tvl_7d_delta: 0.08,
        fees_30d_usd: Math.round(4_000 + nextRand() * 40_000),
        revenue_30d_usd: Math.round(1_000 + nextRand() * 12_000),
        symbol: "-",
        parent_protocol: `parent#frontierbase-${parentIdx}`,
        governance_links: [],
        description: "Pre-token multi-chain deployment.",
      });
    } else {
      // Genuine pre-token base protocol (with periodic wash-TVL outliers)
      const isWash = i % 97 === 0;
      const tvl = isWash ? 950_000_000 : Math.round(50_000 + (nextRand() ** 2) * 150_000_000);
      const delta = isWash ? 14.5 : roundFixed((nextRand() - 0.35) * 0.9, 4);
      const fees = isWash ? 120 : Math.round(tvl * (0.004 + nextRand() * 0.03) / 12);
      const rev = Math.round(fees * (0.2 + nextRand() * 0.35));
      records.push({
        id: `frontierbase-${i}`,
        name: `FrontierBase ${i}`,
        category: sector,
        chains: [chain, chainsPool[(i + 1) % chainsPool.length]],
        tvl_usd: tvl,
        tvl_7d_delta: delta,
        fees_30d_usd: fees,
        revenue_30d_usd: rev,
        symbol: "",
        parent_protocol: "",
        governance_links: [],
        description: "Genuine tokenless base protocol prior to TGE.",
      });
    }
  }
  return records;
}

function extractRecords(parsed: unknown): RawProtocolRecord[] {
  if (Array.isArray(parsed)) return parsed as RawProtocolRecord[];
  if (parsed && typeof parsed === "object") {
    const obj = parsed as Record<string, unknown>;
    if (Array.isArray(obj.protocols)) return obj.protocols as RawProtocolRecord[];
    if (Array.isArray(obj.records)) return obj.records as RawProtocolRecord[];
    if (Array.isArray(obj.rows)) return obj.rows as RawProtocolRecord[];
  }
  throw new Error("Input JSON must be an array or an object containing protocols/records/rows.");
}

function runCli(argv: string[]): void {
  let inputPath = "";
  let outputPath = "board.json";
  let syntheticCount = 0;

  for (let i = 0; i < argv.length; i++) {
    const arg = argv[i];
    if (arg === "--input" || arg === "-i") {
      inputPath = argv[++i] ?? "";
    } else if (arg === "--output" || arg === "-o") {
      outputPath = argv[++i] ?? "board.json";
    } else if (arg === "--generate-synthetic") {
      syntheticCount = Number(argv[++i] ?? "4200");
    } else if (!arg.startsWith("-")) {
      if (!inputPath && syntheticCount === 0) inputPath = arg;
      else outputPath = arg;
    }
  }

  let rawRecords: RawProtocolRecord[];
  if (syntheticCount > 0) {
    rawRecords = generateSyntheticSnapshot(syntheticCount);
  } else if (inputPath) {
    const content = readFileSync(resolve(inputPath), "utf8");
    rawRecords = extractRecords(JSON.parse(content));
  } else {
    throw new Error(
      "Usage: node --experimental-strip-types ranker.ts [--input snapshot.json | --generate-synthetic 4200] [--output board.json]"
    );
  }

  const board = runPipeline(rawRecords);
  const serialized = serializeBoard(board);
  const absOut = resolve(outputPath);
  writeFileSync(absOut, serialized, "utf8");

  const sha256 = createHash("sha256").update(serialized).digest("hex");
  process.stdout.write(
    `${JSON.stringify({
      ok: true,
      output_path: absOut,
      ranked_count: board.summary.ranked_count,
      input_count: board.summary.input_count,
      excluded_live_token_count: board.summary.excluded_live_token_count,
      excluded_derivative_count: board.summary.excluded_derivative_count,
      deduped_child_count: board.summary.deduped_child_count,
      clamped_outlier_count: board.summary.clamped_outlier_count,
      output_sha256: sha256,
    })}\n`
  );
}

const isEntrypoint =
  process.argv[1] !== undefined && resolve(process.argv[1]) === resolve(fileURLToPath(import.meta.url));
if (isEntrypoint) {
  runCli(process.argv.slice(2));
}
