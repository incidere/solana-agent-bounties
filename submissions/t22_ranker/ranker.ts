/**
 * Offline Protocol Snapshot Derivative Stripper & Implied-FDV Ranker
 * Standalone Zero-Dependency Node 22+ TypeScript CLI (`ranker.ts`)
 * Built strictly with Node.js built-ins (`node:crypto`, `node:fs`, `node:path`, `node:url`)
 * Deterministic byte-for-byte reproducible execution.
 */

import * as fs from 'node:fs';
import * as path from 'node:path';
import * as crypto from 'node:crypto';

export interface ProtocolRecord {
  id?: string;
  name: string;
  category: string;
  chains: string[];
  tvl_usd: number;
  tvl_7d_delta?: number;
  fees_30d_usd?: number;
  revenue_30d_usd?: number;
  symbol?: string | null;
  parent_protocol?: string | null;
  governance_links?: string[];
  description?: string;
}

export interface ClassificationResult {
  allowed: boolean;
  reason?: string;
  isTokenized: boolean;
  isDerivative: boolean;
  cleanName: string;
}

export interface RankedEntry {
  rank: number;
  name: string;
  category: string;
  chains: string[];
  tvl_usd: number;
  clamped_tvl_7d_delta: number;
  annualized_fees_usd: number;
  annualized_revenue_usd: number;
  capital_efficiency_score: number;
  implied_fdv_low_usd: number;
  implied_fdv_mid_usd: number;
  implied_fdv_high_usd: number;
  interaction_playbook: string[];
}

export interface RankerStats {
  totalInput: number;
  tokenizedExcluded: number;
  derivativeExcluded: number;
  deduplicatedCount: number;
  outliersClamped: number;
  totalOutput: number;
  outputSha256: string;
}

// Homoglyph substitution map for Cyrillic / Greek / Latin confusables
const HOMOGLYPH_MAP: Record<string, string> = {
  // Cyrillic to Latin
  'А': 'A', 'а': 'a', 'В': 'B', 'С': 'C', 'с': 'c', 'Е': 'E', 'е': 'e',
  'Н': 'H', 'І': 'I', 'і': 'i', 'Ј': 'J', 'ј': 'j', 'К': 'K', 'к': 'k',
  'М': 'M', 'О': 'O', 'о': 'o', 'Р': 'P', 'р': 'p', 'Т': 'T', 'Х': 'X',
  'х': 'x', 'У': 'Y', 'у': 'y', 'Ѕ': 'S', 'ѕ': 's',
  // Greek to Latin
  'Α': 'A', 'Β': 'B', 'Γ': 'G', 'Δ': 'D', 'Ε': 'E', 'Ζ': 'Z', 'Η': 'H',
  'Θ': 'TH', 'Ι': 'I', 'Κ': 'K', 'Λ': 'L', 'Μ': 'M', 'Ν': 'N', 'Ξ': 'X',
  'Ο': 'O', 'Π': 'P', 'Ρ': 'P', 'Σ': 'S', 'Τ': 'T', 'Υ': 'Y', 'Φ': 'F',
  'Χ': 'X', 'Ψ': 'PS', 'Ω': 'O'
};

export function normalizeHomoglyphs(text: string): string {
  if (!text) return '';
  // 1. Unicode NFKC normalization
  let normalized = text.normalize('NFKC');
  // 2. Strip invisible zero-width and control characters
  normalized = normalized.replace(/[\u200B-\u200D\uFEFF\u00A0\u2000-\u200A\u2028\u2029]/g, '');
  // 3. Map known homoglyphs
  let result = '';
  for (const ch of normalized) {
    result += HOMOGLYPH_MAP[ch] || ch;
  }
  return result;
}

// Derivative keywords pattern
const DERIVATIVE_REGEX = /\b(wrapped|liquid[- ]staked|liquid[- ]staking|restaked|restaking|bridged|receipt[- ]token|vault[- ]receipt|lp[- ]receipt|forked[- ]wrapper)\b/i;

// Tokenless protection phrases (negated phrases where token is NOT yet live)
const TOKENLESS_PROTECTION_REGEX = /\b(no\s+(governance\s+|utility\s+)?token(\s+yet)?|tokenless|pre-token|untokenized|points\s+program|airdrop\s+expected|token\s+not\s+(yet\s+)?launched|no\s+native\s+token)\b/i;

// Active token indicator phrases
const ACTIVE_TOKEN_REGEX = /\b(governance token|utility token|native token|reward token|token holder|tokenomics|circulating supply|staked token|traded on|binance listing|coinbase listing|uniswap pool|aerodrome pool)\b/i;

// Disguised ticker bracket patterns: [$TICKER], (TICKER), ticker: TICKER
const DISGUISED_TICKER_REGEX = /\[\$?([A-Z0-9]{2,10})\]|\(\$([A-Z0-9]{2,10})\)|\bticker:\s*([A-Z0-9]{2,10})\b|\bsymbol:\s*([A-Z0-9]{2,10})\b/i;

export function classifyProtocol(record: ProtocolRecord): ClassificationResult {
  const cleanName = normalizeHomoglyphs(record.name || '');
  const cleanCategory = (record.category || '').toLowerCase().trim();
  const cleanDesc = normalizeHomoglyphs(record.description || '');
  const cleanSymbol = normalizeHomoglyphs(record.symbol || '').trim();

  // 1. Check Derivative Wrappers
  if (DERIVATIVE_REGEX.test(cleanName) || DERIVATIVE_REGEX.test(cleanDesc) || DERIVATIVE_REGEX.test(cleanCategory)) {
    return { allowed: false, reason: 'Derivative wrapper protocol', isTokenized: false, isDerivative: true, cleanName };
  }

  // Check known wrapped/derivative symbol patterns (e.g. stETH, wstETH, cbETH, weETH, ezETH, bridged stablecoins USDT0, USDC.e)
  if (cleanSymbol && /^(st[A-Z]+|w[A-Z]+|r[A-Z]+|cb[A-Z]+|ez[A-Z]+|we[A-Z]+|USDT0|USDC\.e|USDD)$/i.test(cleanSymbol)) {
    return { allowed: false, reason: 'Derivative token symbol pattern', isTokenized: false, isDerivative: true, cleanName };
  }

  // 2. Check Live Token Status
  // If symbol is explicitly provided and not a placeholder
  if (cleanSymbol && !['NONE', 'N/A', '-', 'NO', 'NULL', ''].includes(cleanSymbol.toUpperCase())) {
    // If symbol is explicitly TOKENLESS or POINTS, allow
    if (!['TOKENLESS', 'POINTS'].includes(cleanSymbol.toUpperCase())) {
      return { allowed: false, reason: `Active token symbol: ${cleanSymbol}`, isTokenized: true, isDerivative: false, cleanName };
    }
  }

  // Check for disguised ticker in name or description
  const tickerMatch = cleanName.match(DISGUISED_TICKER_REGEX) || cleanDesc.match(DISGUISED_TICKER_REGEX);
  if (tickerMatch) {
    const foundTicker = (tickerMatch[1] || tickerMatch[2] || tickerMatch[3] || tickerMatch[4]).toUpperCase();
    if (!['NONE', 'USDC', 'USDT', 'ETH', 'SOL', 'BTC'].includes(foundTicker)) {
      return { allowed: false, reason: `Disguised token ticker detected: ${foundTicker}`, isTokenized: true, isDerivative: false, cleanName };
    }
  }

  // Check Governance Links for active token governance
  if (record.governance_links && record.governance_links.length > 0) {
    for (const link of record.governance_links) {
      const lower = link.toLowerCase();
      if (lower.includes('snapshot.org') || lower.includes('tally.xyz') || lower.includes('coingecko.com/en/coins/') || lower.includes('coinmarketcap.com/currencies/')) {
        return { allowed: false, reason: `Live token governance/market link: ${link}`, isTokenized: true, isDerivative: false, cleanName };
      }
    }
  }

  // Check description for affirmative active token phrases (unless protected by tokenless phrasing)
  if (cleanDesc) {
    const isProtected = TOKENLESS_PROTECTION_REGEX.test(cleanDesc);
    if (!isProtected && ACTIVE_TOKEN_REGEX.test(cleanDesc)) {
      return { allowed: false, reason: 'Active token affirmative phrasing in description', isTokenized: true, isDerivative: false, cleanName };
    }
  }

  return { allowed: true, isTokenized: false, isDerivative: false, cleanName };
}

export function deduplicateParentChild(records: ProtocolRecord[]): { deduplicated: ProtocolRecord[]; excludedChildrenCount: number } {
  const groups: Map<string, ProtocolRecord[]> = new Map();
  const independent: ProtocolRecord[] = [];

  for (const r of records) {
    const parentSlug = r.parent_protocol ? normalizeHomoglyphs(r.parent_protocol).toLowerCase().trim() : null;
    if (parentSlug) {
      if (!groups.has(parentSlug)) {
        groups.set(parentSlug, []);
      }
      groups.get(parentSlug)!.push(r);
    } else {
      independent.push(r);
    }
  }

  const result: ProtocolRecord[] = [...independent];
  let excludedChildrenCount = 0;

  for (const [parentSlug, group] of groups.entries()) {
    // If any member in the parent group has a live token or derivative, exclude whole family
    const hasTokenized = group.some(item => classifyProtocol(item).isTokenized);
    const hasDerivative = group.some(item => classifyProtocol(item).isDerivative);

    if (hasTokenized || hasDerivative) {
      excludedChildrenCount += group.length;
      continue;
    }

    // Merge group into single representative parent protocol
    const allChains = Array.from(new Set(group.flatMap(g => g.chains || []))).sort();
    const maxTvl = Math.max(...group.map(g => g.tvl_usd || 0));
    const totalFees = group.reduce((acc, g) => acc + (g.fees_30d_usd || 0), 0);
    const totalRev = group.reduce((acc, g) => acc + (g.revenue_30d_usd || 0), 0);
    const primary = group[0];

    const merged: ProtocolRecord = {
      ...primary,
      name: primary.parent_protocol || primary.name,
      chains: allChains,
      tvl_usd: maxTvl,
      fees_30d_usd: totalFees,
      revenue_30d_usd: totalRev,
      parent_protocol: null
    };

    result.push(merged);
    excludedChildrenCount += (group.length - 1);
  }

  return { deduplicated: result, excludedChildrenCount };
}

export function buildCategoryPlaybook(category: string, name: string): string[] {
  const cat = (category || '').toLowerCase();
  if (cat.includes('dex') || cat.includes('swap') || cat.includes('amm')) {
    return [
      `1. Connect web3 wallet to official verified app domain for ${name}.`,
      `2. Execute a spot swap across high-liquidity base pairs (e.g. USDC/SOL or ETH/USDC).`,
      `3. Deposit liquidity into standard concentrated or multi-asset pool within active range.`,
      `4. Maintain continuous LP position for at least 14 days to establish authentic LP tenure.`,
      `5. Harvest and compound accrued LP fees periodically without rapid deposit/withdraw cycles.`
    ];
  } else if (cat.includes('lending') || cat.includes('borrow') || cat.includes('money market')) {
    return [
      `1. Connect verified wallet and deposit collateral asset into primary lending market.`,
      `2. Borrow modest amount of secondary asset (conservative LTV <= 50% to prevent liquidation).`,
      `3. Maintain health factor >= 1.5 across consecutive market epochs.`,
      `4. Repay borrowed principal and accrued interest cleanly prior to withdrawing collateral.`,
      `5. Participate in on-chain parameter voting or community proposals if available.`
    ];
  } else if (cat.includes('perp') || cat.includes('derivative') || cat.includes('margin')) {
    return [
      `1. Deposit USDC margin into verified account vault.`,
      `2. Open hedged or directional perpetual futures position with conservative leverage (<= 3x).`,
      `3. Keep position open across multiple funding rate settlement periods (>= 24h).`,
      `4. Close position in profit/loss and withdraw settled margin to original funding address.`
    ];
  } else if (cat.includes('yield') || cat.includes('vault') || cat.includes('asset management')) {
    return [
      `1. Review vault underlying smart contract audit and performance history.`,
      `2. Deposit target asset into non-synthetic autocompounding strategy vault.`,
      `3. Hold vault shares across minimum 30-day epoch cycle.`,
      `4. Redeem shares and claim underlying yield cleanly.`
    ];
  } else if (cat.includes('cdp') || cat.includes('stablecoin')) {
    return [
      `1. Open collateralized debt position with verified underlying assets.`,
      `2. Mint decentralized stablecoin maintaining minimum 160% collateral ratio.`,
      `3. Deploy minted stablecoin into ecosystem liquidity or stability pool.`,
      `4. Burn stablecoin to extinguish debt and retrieve collateral.`
    ];
  } else {
    return [
      `1. Connect wallet and perform genuine primary protocol interaction on testnet/mainnet.`,
      `2. Provide utility volume and test edge case transaction workflows.`,
      `3. Maintain sustained interaction history across multiple distinct weeks.`
    ];
  }
}

export function scoreAndRank(records: ProtocolRecord[]): { ranked: RankedEntry[]; clampedCount: number } {
  let clampedCount = 0;

  const scored = records.map(record => {
    // 1. Clamping tvl_7d_delta to [-0.85, +2.00]
    let rawDelta = record.tvl_7d_delta !== undefined ? record.tvl_7d_delta : 0.0;
    let clampedDelta = rawDelta;
    if (rawDelta < -0.85) {
      clampedDelta = -0.85;
      clampedCount++;
    } else if (rawDelta > 2.00) {
      clampedDelta = 2.00;
      clampedCount++;
    }

    // 2. Wash-TVL Spikes penalty
    const tvl = Math.max(record.tvl_usd || 0, 0);
    const fees30d = Math.max(record.fees_30d_usd || 0, 0);
    let rev30d = Math.max(record.revenue_30d_usd || 0, 0);
    if (rev30d > fees30d) {
      rev30d = fees30d; // Revenue cannot logically exceed protocol gross fees
    }

    const annualizedFees = fees30d * 12;
    const annualizedRev = rev30d * 12;

    let washPenalty = 1.0;
    if (tvl > 10_000_000 && (annualizedFees / tvl < 0.001) && clampedDelta > 1.2) {
      washPenalty = 0.35;
      clampedCount++;
    }

    // 3. Implied FDV multi-band estimation based on sector multiples
    const cat = (record.category || '').toLowerCase();
    let tvlMultipliers = [0.15, 0.40, 1.00];
    let feeMultipliers = [8, 16, 32];

    if (cat.includes('dex') || cat.includes('swap')) {
      tvlMultipliers = [0.20, 0.50, 1.20];
      feeMultipliers = [10, 20, 40];
    } else if (cat.includes('lending')) {
      tvlMultipliers = [0.15, 0.35, 0.90];
      feeMultipliers = [8, 15, 30];
    } else if (cat.includes('perp')) {
      tvlMultipliers = [0.25, 0.60, 1.50];
      feeMultipliers = [12, 24, 45];
    } else if (cat.includes('yield')) {
      tvlMultipliers = [0.10, 0.25, 0.60];
      feeMultipliers = [6, 12, 25];
    }

    const baselineTvl = tvl * washPenalty;
    const fdvLow = Math.round(baselineTvl * tvlMultipliers[0] + annualizedFees * feeMultipliers[0]);
    const fdvMid = Math.round(baselineTvl * tvlMultipliers[1] + annualizedFees * feeMultipliers[1]);
    const fdvHigh = Math.round(baselineTvl * tvlMultipliers[2] + annualizedFees * feeMultipliers[2]);

    // 4. Capital Efficiency Score
    const feeYield = (annualizedFees / Math.max(tvl, 50_000));
    const momentum = 1.0 + 0.25 * clampedDelta;
    const capitalEfficiency = parseFloat((feeYield * momentum * washPenalty * 100).toFixed(4));

    const playbook = buildCategoryPlaybook(record.category, record.name);

    return {
      name: record.name,
      category: record.category || 'Other',
      chains: (record.chains || []).slice().sort(),
      tvl_usd: tvl,
      clamped_tvl_7d_delta: parseFloat(clampedDelta.toFixed(4)),
      annualized_fees_usd: annualizedFees,
      annualized_revenue_usd: annualizedRev,
      capital_efficiency_score: capitalEfficiency,
      implied_fdv_low_usd: Math.max(fdvLow, 100_000),
      implied_fdv_mid_usd: Math.max(fdvMid, 250_000),
      implied_fdv_high_usd: Math.max(fdvHigh, 500_000),
      interaction_playbook: playbook
    };
  });

  // Sort deterministically:
  // Primary: capital_efficiency_score descending
  // Secondary: tvl_usd descending
  // Tertiary: name alphabetical ascending
  scored.sort((a, b) => {
    if (b.capital_efficiency_score !== a.capital_efficiency_score) {
      return b.capital_efficiency_score - a.capital_efficiency_score;
    }
    if (b.tvl_usd !== a.tvl_usd) {
      return b.tvl_usd - a.tvl_usd;
    }
    return a.name.localeCompare(b.name);
  });

  const ranked: RankedEntry[] = scored.map((item, idx) => ({
    rank: idx + 1,
    ...item
  }));

  return { ranked, clampedCount };
}

export function processSnapshot(records: ProtocolRecord[]): { ranked: RankedEntry[]; stats: RankerStats; jsonString: string } {
  let tokenizedExcluded = 0;
  let derivativeExcluded = 0;
  const filtered: ProtocolRecord[] = [];

  for (const record of records) {
    const classification = classifyProtocol(record);
    if (!classification.allowed) {
      if (classification.isTokenized) {
        tokenizedExcluded++;
      } else if (classification.isDerivative) {
        derivativeExcluded++;
      }
      continue;
    }
    filtered.push({
      ...record,
      name: classification.cleanName
    });
  }

  const { deduplicated, excludedChildrenCount } = deduplicateParentChild(filtered);
  const { ranked, clampedCount } = scoreAndRank(deduplicated);

  // Deterministic JSON serialization (2 spaces, trailing newline)
  const jsonString = JSON.stringify(ranked, null, 2) + '\n';
  const outputSha256 = crypto.createHash('sha256').update(jsonString, 'utf-8').digest('hex');

  const stats: RankerStats = {
    totalInput: records.length,
    tokenizedExcluded,
    derivativeExcluded,
    deduplicatedCount: excludedChildrenCount,
    outliersClamped: clampedCount,
    totalOutput: ranked.length,
    outputSha256
  };

  return { ranked, stats, jsonString };
}

// CLI entry point
export function runCli(args: string[]): void {
  const inputFileIdx = args.indexOf('--input');
  const outputFileIdx = args.indexOf('--output');
  const synthIdx = args.indexOf('--generate-synthetic');

  if (synthIdx !== -1) {
    const count = parseInt(args[synthIdx + 1] || '4200', 10);
    const syntheticRecords: ProtocolRecord[] = [];
    const categories = ['DEXes', 'Lending', 'Perpetuals', 'Yield', 'CDP', 'Derivatives'];
    const chainSets = [['Solana'], ['Ethereum'], ['Base', 'Ethereum'], ['Solana', 'Base'], ['Arbitrum']];

    for (let i = 0; i < count; i++) {
      const isTokenized = (i % 5 === 0);
      const isDerivative = (i % 10 === 0);
      const isParent = (i % 15 === 0);
      const hasDelta = (i % 20 === 0) ? 3.5 : (i % 7 === 0 ? -0.95 : 0.15);

      syntheticRecords.push({
        name: isDerivative ? `Wrapped Lido Yield ${i}` : (isTokenized ? `Tokenized Protocol ${i}` : `Protocol Alpha ${i}`),
        category: categories[i % categories.length],
        chains: chainSets[i % chainSets.length],
        tvl_usd: 500_000 + (i * 1234) % 10_000_000,
        tvl_7d_delta: hasDelta,
        fees_30d_usd: 15_000 + (i * 100) % 200_000,
        revenue_30d_usd: 5_000 + (i * 50) % 100_000,
        symbol: isTokenized ? `TOK${i}` : null,
        parent_protocol: isParent ? `ParentEcosystem_${Math.floor(i / 15)}` : null,
        description: isDerivative ? 'Liquid-staked derivative receipt token' : (isTokenized ? 'Traded on Uniswap with active token' : 'No governance token yet, untokenized points program')
      });
    }

    const { stats, jsonString } = processSnapshot(syntheticRecords);
    const outFile = outputFileIdx !== -1 ? args[outputFileIdx + 1] : 'board.json';
    fs.writeFileSync(outFile, jsonString, 'utf-8');
    console.log(`Synthetic run completed (${count} records) -> ${outFile}`);
    console.log(`SHA-256: ${stats.outputSha256}`);
    console.log(`Stats:`, JSON.stringify(stats, null, 2));
    return;
  }

  const inputFile = inputFileIdx !== -1 ? args[inputFileIdx + 1] : 'snapshot.json';
  const outputFile = outputFileIdx !== -1 ? args[outputFileIdx + 1] : 'board.json';

  if (!fs.existsSync(inputFile)) {
    console.error(`Input file not found: ${inputFile}`);
    process.exit(1);
  }

  const raw = fs.readFileSync(inputFile, 'utf-8');
  const records: ProtocolRecord[] = JSON.parse(raw);
  const { stats, jsonString } = processSnapshot(records);
  fs.writeFileSync(outputFile, jsonString, 'utf-8');

  console.log(`Successfully processed ${records.length} records -> ${outputFile}`);
  console.log(`SHA-256: ${stats.outputSha256}`);
  console.log(`Stats:`, JSON.stringify(stats, null, 2));
}

// Run CLI when executed directly
if (process.argv[1] && (process.argv[1].endsWith('ranker.ts') || process.argv[1].endsWith('ranker.js'))) {
  runCli(process.argv.slice(2));
}
