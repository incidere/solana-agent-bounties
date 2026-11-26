import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import {
  classifyProtocol,
  generateSyntheticSnapshot,
  normalizeRecord,
  runPipeline,
  serializeBoard,
  type RawProtocolRecord,
} from "./ranker.ts";

const REPO_DIR = resolve(fileURLToPath(new URL(".", import.meta.url)));
const RANKER_PATH = join(REPO_DIR, "ranker.ts");

function baseRecord(overrides: Partial<RawProtocolRecord> = {}): RawProtocolRecord {
  return {
    id: "hyper-alpha",
    name: "HyperAlpha",
    category: "Dexes",
    chains: ["Solana", "Base"],
    tvl_usd: 25_000_000,
    tvl_7d_delta: 0.14,
    fees_30d_usd: 320_000,
    revenue_30d_usd: 96_000,
    symbol: "",
    parent_protocol: "",
    governance_links: [],
    description: "Genuine tokenless spot liquidity protocol prior to TGE.",
    ...overrides,
  };
}

interface ClassifierFixture {
  label: string;
  record: RawProtocolRecord;
  expectedKeep: boolean;
  expectedReasonIncludes?: string;
}

const CLASSIFIER_FIXTURES: ClassifierFixture[] = [
  // Liquid-staking & restaking derivatives
  { label: "F01 category Liquid Staking", record: baseRecord({ name: "StakePool X", category: "Liquid Staking", symbol: "" }), expectedKeep: false, expectedReasonIncludes: "derivative_liquid_staked" },
  { label: "F02 category Liquid Restaking", record: baseRecord({ name: "RestakeVault", category: "Liquid Restaking", symbol: "" }), expectedKeep: false, expectedReasonIncludes: "derivative_restaked" },
  { label: "F03 symbol stETH", record: baseRecord({ name: "Lido Staked ETH", symbol: "stETH" }), expectedKeep: false, expectedReasonIncludes: "derivative_liquid_staked" },
  { label: "F04 symbol wstETH", record: baseRecord({ name: "Wrapped stETH", symbol: "wstETH" }), expectedKeep: false, expectedReasonIncludes: "derivative_liquid_staked" },
  { label: "F05 symbol jitoSOL", record: baseRecord({ name: "Jito Staked SOL", symbol: "jitoSOL" }), expectedKeep: false, expectedReasonIncludes: "derivative_liquid_staked" },
  { label: "F06 symbol ezETH", record: baseRecord({ name: "Renzo Restaked ETH", symbol: "ezETH" }), expectedKeep: false, expectedReasonIncludes: "derivative_restaked" },
  { label: "F07 symbol rsETH", record: baseRecord({ name: "Kelp Restaked", symbol: "rsETH" }), expectedKeep: false, expectedReasonIncludes: "derivative_restaked" },
  { label: "F08 name prefix Liquid Staked", record: baseRecord({ name: "Liquid Staked SUI", category: "Yield", symbol: "" }), expectedKeep: false, expectedReasonIncludes: "derivative_liquid_staked" },
  { label: "F09 description LRT wrapper", record: baseRecord({ name: "EtherNest", description: "Autonomous LRT wrapper vault tracking staked ETH." }), expectedKeep: false, expectedReasonIncludes: "derivative_restaked" },

  // Wrapped, bridged, receipt & forked single-asset wrappers
  { label: "F10 symbol WBTC", record: baseRecord({ name: "BitWrap", symbol: "WBTC" }), expectedKeep: false, expectedReasonIncludes: "derivative_wrapped" },
  { label: "F11 category Wrapped", record: baseRecord({ name: "AssetWrap", category: "Wrapped Assets", symbol: "" }), expectedKeep: false, expectedReasonIncludes: "derivative" },
  { label: "F12 name Wrapped Bitcoin", record: baseRecord({ name: "Wrapped Bitcoin Bridge", symbol: "" }), expectedKeep: false, expectedReasonIncludes: "derivative_wrapped" },
  { label: "F13 name Bridged USDC", record: baseRecord({ name: "Bridged USDC Arbitrum", category: "DeFi", symbol: "" }), expectedKeep: false, expectedReasonIncludes: "derivative_bridged" },
  { label: "F14 blank symbol disguised USDT0", record: baseRecord({ name: "Tether Omnichain USDT0", symbol: "", description: "Canonical USDT0 bridged stablecoin" }), expectedKeep: false, expectedReasonIncludes: "derivative_bridged" },
  { label: "F15 blank symbol disguised USDD", record: baseRecord({ name: "Decentralized USDD", symbol: "", description: "USDD stablecoin reserve" }), expectedKeep: false, expectedReasonIncludes: "derivative_bridged" },
  { label: "F16 blank symbol USDC.e", record: baseRecord({ name: "Base USDC.e", symbol: "", description: "Bridged USDC.e token" }), expectedKeep: false, expectedReasonIncludes: "derivative_bridged" },
  { label: "F17 LP receipt token", record: baseRecord({ name: "Aero Receipt", category: "DEX", description: "ERC-4626 wrapper sharing underlying LP receipt positions." }), expectedKeep: false, expectedReasonIncludes: "derivative_lp_vault_receipt" },
  { label: "F18 Pendle PT wrapper", record: baseRecord({ name: "Principal Token", symbol: "pt-eETH", category: "Yield" }), expectedKeep: false, expectedReasonIncludes: "derivative_lp_vault_receipt" },
  { label: "F19 Pendle YT wrapper", record: baseRecord({ name: "Yield Token", symbol: "yt-weETH", category: "Yield" }), expectedKeep: false, expectedReasonIncludes: "derivative_lp_vault_receipt" },
  { label: "F20 Vault receipt symbol aUSDC", record: baseRecord({ name: "Pool Receipt", symbol: "aUSDC" }), expectedKeep: false, expectedReasonIncludes: "derivative" },
  { label: "F21 Forked single-asset wrapper", record: baseRecord({ name: "CloneBTC", description: "Forked single-asset wrapper backed 1:1 by BTC." }), expectedKeep: false, expectedReasonIncludes: "derivative_forked_wrapper" },

  // Live token & disguised ticker stripping
  { label: "F22 explicit known ticker AAVE", record: baseRecord({ name: "Aave V3", symbol: "AAVE" }), expectedKeep: false, expectedReasonIncludes: "live_token_known_ticker" },
  { label: "F23 zero-width disguised ticker U\\u200bNI", record: baseRecord({ name: "UniClone", symbol: "U\u200bNI" }), expectedKeep: false, expectedReasonIncludes: "live_token_known_ticker" },
  { label: "F24 spaced disguised ticker L D O", record: baseRecord({ name: "LidProto", symbol: "L D O" }), expectedKeep: false, expectedReasonIncludes: "live_token_known_ticker" },
  { label: "F25 Cyrillic homoglyph ticker ААVЕ", record: baseRecord({ name: "Homoglyph", symbol: "\u0410\u0410V\u0415" }), expectedKeep: false, expectedReasonIncludes: "live_token_known_ticker" },
  { label: "F26 cashtag bracketed ticker [$GMX]", record: baseRecord({ name: "PerpFork", symbol: "[$GMX]" }), expectedKeep: false, expectedReasonIncludes: "live_token_known_ticker" },
  { label: "F27 prefixed ticker:MORPHO", record: baseRecord({ name: "LendX", symbol: "ticker:MORPHO" }), expectedKeep: false, expectedReasonIncludes: "live_token_known_ticker" },
  { label: "F28 non-empty custom token", record: baseRecord({ name: "CustomTokenDex", symbol: "CTDX" }), expectedKeep: false, expectedReasonIncludes: "live_token_symbol" },
  { label: "F29 Snapshot governance link with empty symbol", record: baseRecord({ name: "GovDao", symbol: "", governance_links: ["https://snapshot.org/#/govdao.eth"] }), expectedKeep: false, expectedReasonIncludes: "live_token_governance_link" },
  { label: "F30 CoinGecko link with empty symbol", record: baseRecord({ name: "ListedDex", symbol: "", governance_links: ["https://www.coingecko.com/en/coins/listeddex"] }), expectedKeep: false, expectedReasonIncludes: "live_token_governance_link" },
  { label: "F31 nonzero mcap with empty symbol", record: baseRecord({ name: "TrackedCoin", symbol: "", mcap: 42_000_000 }), expectedKeep: false, expectedReasonIncludes: "live_token_market_data" },
  { label: "F32 description confirms TGE completed", record: baseRecord({ name: "PostTgeApp", symbol: "", description: "Decentralized perpetuals after TGE completed with staking." }), expectedKeep: false, expectedReasonIncludes: "live_token_description" },
  { label: "F33 invalid blank name", record: baseRecord({ name: "   ", symbol: "" }), expectedKeep: false, expectedReasonIncludes: "invalid_missing_name" },

  // Genuine pre-token base protocols preserved
  { label: "F34 clean pre-token DEX", record: baseRecord({ name: "ZeroXBase", category: "Dexes", symbol: "" }), expectedKeep: true },
  { label: "F35 dash placeholder symbol", record: baseRecord({ name: "PerpEngine", category: "Perpetuals", symbol: "-" }), expectedKeep: true },
  { label: "F36 TBD placeholder symbol", record: baseRecord({ name: "LendCore", category: "Lending", symbol: "TBD" }), expectedKeep: true },
  { label: "F37 pre-token placeholder symbol", record: baseRecord({ name: "YieldMatrix", category: "Yield", symbol: "pre-token" }), expectedKeep: true },
  { label: "F38 negated live-token phrase in description", record: baseRecord({ name: "BaseOrderbook", category: "DEX", symbol: "", description: "Orderbook DEX with no governance token yet and not a wrapped asset." }), expectedKeep: true },
  { label: "F39 prediction market without token", record: baseRecord({ name: "OracleOdds", category: "Prediction", symbol: "points" }), expectedKeep: true },
  { label: "F40 RWA protocol prior to TGE", record: baseRecord({ name: "TreasuryRail", category: "RWA", symbol: "none", description: "Tokenized T-bill rail without a live governance token." }), expectedKeep: true },
];

for (const fx of CLASSIFIER_FIXTURES) {
  test(`classifier: ${fx.label}`, () => {
    const res = classifyProtocol(normalizeRecord(fx.record, 0));
    assert.equal(res.keep, fx.expectedKeep, `${fx.label} -> ${res.reason}`);
    if (fx.expectedReasonIncludes) {
      assert.ok(
        res.reason.includes(fx.expectedReasonIncludes),
        `Expected reason containing '${fx.expectedReasonIncludes}', got '${res.reason}'`
      );
    }
  });
}

test("F41 parent-child deduplication avoids TVL/fee double counting", () => {
  const board = runPipeline([
    baseRecord({
      id: "alpha-parent",
      name: "AlphaEco",
      chains: ["Arbitrum", "Base"],
      tvl_usd: 40_000_000,
      fees_30d_usd: 500_000,
      revenue_30d_usd: 150_000,
      parent_protocol: "",
    }),
    baseRecord({
      id: "alpha-arb",
      name: "AlphaEco Arbitrum",
      chains: ["Arbitrum"],
      tvl_usd: 25_000_000,
      fees_30d_usd: 300_000,
      revenue_30d_usd: 90_000,
      parent_protocol: "parent#alphaeco",
    }),
    baseRecord({
      id: "alpha-base",
      name: "AlphaEco Base",
      chains: ["Base"],
      tvl_usd: 15_000_000,
      fees_30d_usd: 200_000,
      revenue_30d_usd: 60_000,
      parent_protocol: "AlphaEco",
    }),
  ]);
  assert.equal(board.entries.length, 1);
  assert.equal(board.summary.deduped_child_count, 2);
  assert.equal(board.entries[0].tvl_usd, 40_000_000);
  assert.equal(board.entries[0].fees_30d_usd, 500_000);
  assert.equal(board.entries[0].deduped_children_count, 2);
});

test("F42 excludes child modules when parent protocol has live token", () => {
  const board = runPipeline([
    baseRecord({ id: "lido-root", name: "Lido", symbol: "LDO", tvl_usd: 30_000_000_000 }),
    baseRecord({
      id: "lido-child",
      name: "Lido Module Beta",
      category: "Yield",
      symbol: "",
      parent_protocol: "Lido",
      tvl_usd: 12_000_000,
    }),
    baseRecord({ id: "real-base", name: "RealBase", category: "Lending", tvl_usd: 12_000_000 }),
  ]);
  assert.equal(board.entries.length, 1);
  assert.equal(board.entries[0].name, "RealBase");
  assert.equal(board.exclusion_summary["parent_has_live_token_or_derivative"], 1);
});

test("F43 wash-TVL spike and delta explosion are clamped below organic earner", () => {
  const board = runPipeline([
    baseRecord({
      id: "wash-spike",
      name: "WashPump",
      category: "Dexes",
      tvl_usd: 600_000_000,
      tvl_7d_delta: 25.0,
      fees_30d_usd: 0,
      revenue_30d_usd: 0,
    }),
    baseRecord({
      id: "organic-pro",
      name: "OrganicDex",
      category: "Dexes",
      tvl_usd: 45_000_000,
      tvl_7d_delta: 0.25,
      fees_30d_usd: 900_000,
      revenue_30d_usd: 300_000,
    }),
    baseRecord({ id: "peer-1", name: "Peer1", category: "Dexes", tvl_usd: 8_000_000, fees_30d_usd: 80_000, revenue_30d_usd: 20_000 }),
    baseRecord({ id: "peer-2", name: "Peer2", category: "Dexes", tvl_usd: 6_000_000, fees_30d_usd: 60_000, revenue_30d_usd: 15_000 }),
    baseRecord({ id: "peer-3", name: "Peer3", category: "Dexes", tvl_usd: 4_000_000, fees_30d_usd: 40_000, revenue_30d_usd: 10_000 }),
    baseRecord({ id: "peer-4", name: "Peer4", category: "Dexes", tvl_usd: 2_000_000, fees_30d_usd: 20_000, revenue_30d_usd: 5_000 }),
  ]);
  const washRow = board.entries.find((e) => e.id === "wash-spike")!;
  assert.ok(washRow.clamped_flags.includes("wash_tvl_clamped"));
  assert.ok(washRow.clamped_flags.includes("delta_spike_clamped"));
  assert.ok(washRow.clamped_tvl_7d_delta <= 2.0);
  assert.ok(washRow.effective_tvl_usd < 600_000_000);
  assert.equal(board.entries[0].id, "organic-pro");
});

test("F44 micro-TVL protocol cannot dominate via fake high fee ratio", () => {
  const board = runPipeline([
    baseRecord({
      id: "dust-wash",
      name: "DustProtocol",
      category: "Lending",
      tvl_usd: 1_680,
      fees_30d_usd: 95_000,
      revenue_30d_usd: 95_000,
    }),
    baseRecord({
      id: "solid-lender",
      name: "SolidLend",
      category: "Lending",
      tvl_usd: 55_000_000,
      fees_30d_usd: 420_000,
      revenue_30d_usd: 120_000,
    }),
  ]);
  assert.equal(board.entries[0].id, "solid-lender");
  const dust = board.entries.find((e) => e.id === "dust-wash")!;
  assert.ok(dust.clamped_flags.includes("wash_fee_multiple_clamped"));
});

test("F45 implied FDV bands, revenue<=fees clamp, and 3..8 playbook bounds", () => {
  const categories = ["Dexes", "Perpetuals", "Lending", "CDP", "Yield", "Prediction", "Oracle", "RWA", "General"];
  const rows = categories.map((cat, idx) =>
    baseRecord({
      id: `cat-${idx}`,
      name: `Proto-${cat}`,
      category: cat,
      tvl_usd: 12_000_000 + idx * 1_000_000,
      fees_30d_usd: 100_000,
      revenue_30d_usd: idx === 0 ? 250_000 : 30_000,
      tvl_7d_delta: -0,
    })
  );
  const board = runPipeline(rows);
  assert.equal(board.entries.length, categories.length);
  for (const entry of board.entries) {
    assert.ok(entry.implied_fdv_low_usd <= entry.implied_fdv_mid_usd);
    assert.ok(entry.implied_fdv_mid_usd <= entry.implied_fdv_high_usd);
    assert.ok(entry.playbook.length >= 3 && entry.playbook.length <= 8);
    assert.ok(!Object.is(entry.tvl_7d_delta, -0));
  }
  const anomalyRow = board.entries.find((e) => e.id === "cat-0")!;
  assert.ok(anomalyRow.clamped_flags.includes("revenue_exceeds_fees_clamped"));
  assert.ok(anomalyRow.annualized_revenue_usd <= anomalyRow.annualized_fees_usd);
});

test("F46 input permutation invariance and byte-for-byte identical serialization", () => {
  const synthetic = generateSyntheticSnapshot(420);
  const reversed = [...synthetic].reverse();
  const b1 = serializeBoard(runPipeline(synthetic));
  const b2 = serializeBoard(runPipeline(reversed));
  const h1 = createHash("sha256").update(b1).digest("hex");
  const h2 = createHash("sha256").update(b2).digest("hex");
  assert.equal(h1, h2);
  assert.equal(b1, b2);
});

test("F47 CLI generates 4,200+ snapshot yielding 100..4000 rows and byte-identical SHA-256 across two subprocess runs", () => {
  const dir = mkdtempSync(join(tmpdir(), "ranker-test-"));
  try {
    const out1 = join(dir, "board_run1.json");
    const out2 = join(dir, "board_run2.json");

    const stdout1 = execFileSync(
      process.execPath,
      ["--experimental-strip-types", RANKER_PATH, "--generate-synthetic", "4200", "--output", out1],
      { encoding: "utf8" }
    );
    const stdout2 = execFileSync(
      process.execPath,
      ["--experimental-strip-types", RANKER_PATH, "--generate-synthetic", "4200", "--output", out2],
      { encoding: "utf8" }
    );

    const meta1 = JSON.parse(stdout1.trim()) as { ranked_count: number; output_sha256: string };
    const meta2 = JSON.parse(stdout2.trim()) as { ranked_count: number; output_sha256: string };
    assert.ok(meta1.ranked_count >= 100 && meta1.ranked_count <= 4000, `ranked_count=${meta1.ranked_count}`);
    assert.equal(meta1.output_sha256, meta2.output_sha256);

    const buf1 = readFileSync(out1);
    const buf2 = readFileSync(out2);
    assert.equal(Buffer.compare(buf1, buf2), 0);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});
