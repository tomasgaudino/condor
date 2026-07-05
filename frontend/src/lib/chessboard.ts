import type { CandleData } from "@/lib/api";

// ── Paleta accesible (daltonismo rojo-verde) ─────────────────────────────
// LONG/BUY = azul, SHORT/SELL = ámbar, rebates = rosa. El color nunca es el
// único canal: sólido = cobrado, rayado = abierto; derecha = ingreso,
// izquierda = riesgo.
export const CB_BLUE = "#378ADD"; // LONG
export const CB_AMBER = "#EF9F27"; // SHORT
export const CB_PINK = "#D4537E"; // rebates
export const CB_GREEN_TOT = "#2EA043"; // totalizador ingreso
export const CB_RED_TOT = "#E24B4A"; // totalizador IL

// ── Tipos (espejo del payload de /chessboard/state) ──────────────────────

export interface ChessboardFill {
  ts: number; // epoch ms
  price: number;
  base: number;
  qvol: number;
  side: "BUY" | "SELL" | string;
  matched: number;
  open: number;
  fee_quote: number;
}

export interface FifoMatch {
  side: "LONG" | "SHORT" | string;
  level: number;
  spread: number;
}

export interface OpenLot {
  side: "BUY" | "SELL" | string;
  price: number;
  base: number;
}

export interface FifoResult {
  realized: number;
  fees: number;
  be_buy: number | null;
  be_sell: number | null;
  be_general: number | null;
  open_long_base: number;
  open_short_base: number;
  matched_quote: number;
  open_quote: number;
  t0: number | null;
  matches: FifoMatch[];
  open_lots: OpenLot[];
}

export interface GridTelemetrySide {
  state?: string;
  n_levels_real?: number;
  position_quote?: number;
  realized_pnl_quote?: number;
  fees_quote?: number;
  break_even?: number | null;
  relaunches?: number;
  taker_volume_quote?: number;
  retry_in_s?: number;
}

export interface GridTelemetry {
  inv_pct?: number;
  inv_band?: [number, number];
  long?: GridTelemetrySide;
  short?: GridTelemetrySide;
  [k: string]: unknown;
}

export interface ChessboardGrid {
  id: string;
  bot_name: string;
  connector: string;
  trading_pair: string;
  low: number;
  high: number;
  n_levels: number;
  liquidity: number;
  inv_floor: number;
  inv_ceiling: number;
  realized: number;
  unrealized: number;
  pnl: number;
  volume: number;
  deployed_at: string | null;
  telemetry: GridTelemetry | null;
  alerts: string[];
  fifo: FifoResult;
  fills: ChessboardFill[];
}

export interface ChessboardState {
  server_online: boolean;
  error_hint?: string;
  grids: ChessboardGrid[];
  prices: Record<string, number>;
  bots?: { bot_name: string; grids: number }[];
}

// ── Binning compartido ────────────────────────────────────────────────────

export interface PriceBins {
  edges: number[]; // n+1
  centers: number[]; // n
  step: number;
}

export function makeBins(lo: number, hi: number, n: number): PriceBins {
  const edges: number[] = [];
  const step = (hi - lo) / n;
  for (let i = 0; i <= n; i++) edges.push(lo + step * i);
  const centers = edges.slice(0, -1).map((e) => e + step / 2);
  return { edges, centers, step };
}

function binIndex(bins: PriceBins, price: number): number {
  const n = bins.centers.length;
  const i = Math.floor((price - bins.edges[0]) / bins.step);
  return Math.max(0, Math.min(n - 1, i));
}

// ── Modo 1: volumen del mercado por precio + inventario esperado ─────────

export interface VolumeProfile {
  volumes: number[]; // por bin, en base asset
  poc: number; // índice del Point of Control (-1 si vacío)
  max: number;
}

/** Volume-by-price clásico: el volumen de cada vela va al bin de su precio
 * típico (H+L+C)/3. Corre sobre las velas visibles → reacciona al zoom. */
export function volumeProfile(candles: CandleData[], bins: PriceBins): VolumeProfile {
  const volumes = new Array(bins.centers.length).fill(0);
  for (const c of candles) {
    const typical = (c.high + c.low + c.close) / 3;
    volumes[binIndex(bins, typical)] += c.volume;
  }
  let poc = -1;
  let max = 0;
  volumes.forEach((v, i) => {
    if (v > max) {
      max = v;
      poc = i;
    }
  });
  return { volumes, poc, max };
}

/** Fracción del capital de una grilla en BASE a un precio dado.
 * Debajo del rango → 100% base (techo); arriba → 0% (piso); adentro, lineal.
 * (Espejo de _grid_base_fraction de la routine, con techo=1/piso=0 fijos.) */
export function gridBaseFraction(price: number, low: number, high: number): number {
  if (high <= low) return 1;
  if (price <= low) return 1;
  if (price >= high) return 0;
  return 1 - (price - low) / (high - low);
}

export interface InventoryProfile {
  baseVals: number[]; // capital en base (unidades quote) por bin
  quoteVals: number[];
  pctBase: number[]; // 0-100 por bin
  max: number; // máximo base+quote (para escalar)
}

/** Cómo DEBERÍA fluctuar el inventario: composición base/quote proyectada del
 * capital agregado de las grillas, por nivel de precio. */
export function inventoryProfile(grids: ChessboardGrid[], bins: PriceBins): InventoryProfile {
  const n = bins.centers.length;
  const baseVals = new Array(n).fill(0);
  const quoteVals = new Array(n).fill(0);
  for (let i = 0; i < n; i++) {
    const p = bins.centers[i];
    for (const g of grids) {
      const fb = gridBaseFraction(p, g.low, g.high);
      baseVals[i] += g.liquidity * fb;
      quoteVals[i] += g.liquidity * (1 - fb);
    }
  }
  const pctBase = baseVals.map((b, i) => {
    const t = b + quoteVals[i];
    return t > 0 ? (b / t) * 100 : 0;
  });
  const max = Math.max(...baseVals.map((b, i) => b + quoteVals[i]), 0);
  return { baseVals, quoteVals, pctBase, max };
}

// ── Modo 2: volumen generado por grilla ──────────────────────────────────

export interface GridVolumeByPrice {
  gridId: string;
  buyMatched: number[]; // quote por bin
  buyOpen: number[];
  sellMatched: number[];
  sellOpen: number[];
  max: number;
}

/** Volumen propio de una grilla por precio, partido BUY/SELL (espejo) y
 * matcheado/abierto (sólido/rayado) — del FIFO ya aplicado en el backend. */
export function gridVolumeByPrice(grid: ChessboardGrid, bins: PriceBins): GridVolumeByPrice {
  const n = bins.centers.length;
  const mk = () => new Array(n).fill(0);
  const out: GridVolumeByPrice = {
    gridId: grid.id,
    buyMatched: mk(),
    buyOpen: mk(),
    sellMatched: mk(),
    sellOpen: mk(),
    max: 0,
  };
  for (const f of grid.fills) {
    const i = binIndex(bins, f.price);
    const m = f.matched * f.price;
    const o = f.open * f.price;
    if (f.side === "BUY") {
      out.buyMatched[i] += m;
      out.buyOpen[i] += o;
    } else {
      out.sellMatched[i] += m;
      out.sellOpen[i] += o;
    }
  }
  for (let i = 0; i < n; i++) {
    out.max = Math.max(out.max, out.buyMatched[i] + out.buyOpen[i], out.sellMatched[i] + out.sellOpen[i]);
  }
  return out;
}

export interface VolumeOverTimePoint {
  time: number; // epoch s (bucket)
  buy: number; // quote
  sell: number;
}

/** Volumen por grilla en función del tiempo, bucketeado al intervalo dado. */
export function gridVolumeOverTime(
  grid: ChessboardGrid,
  bucketSeconds: number,
): VolumeOverTimePoint[] {
  const buckets = new Map<number, { buy: number; sell: number }>();
  for (const f of grid.fills) {
    const t = Math.floor(f.ts / 1000 / bucketSeconds) * bucketSeconds;
    const b = buckets.get(t) ?? { buy: 0, sell: 0 };
    if (f.side === "BUY") b.buy += f.qvol;
    else b.sell += f.qvol;
    buckets.set(t, b);
  }
  return [...buckets.entries()]
    .sort((a, b) => a[0] - b[0])
    .map(([time, v]) => ({ time, ...v }));
}

// ── Modo 3: IL vs spreads capturados + rebates, por nivel ────────────────

export interface LevelPnl {
  spreadLong: number[]; // quote por bin (≥0, derecha)
  spreadShort: number[];
  rebates: number[];
  openLong: number[]; // PnL con signo (der=ganando, izq=perdiendo)
  openShort: number[];
  incomeTotal: number; // Σ spreads + rebates + abiertos en ganancia
  ilTotal: number; // Σ |abiertos en pérdida|
  net: number; // income - il = PnL vs HOLD
  max: number; // máximo |valor| por bin (para escalar el eje)
}

/** La identidad contable con resolución por nivel:
 * PnL vs HOLD = Σ spread capturado + Σ rebates + PnL del abierto.
 * (Espejo de _build_level_pnl_figure de la routine, solo los datos.) */
export function levelPnl(
  grid: ChessboardGrid,
  currentPrice: number,
  bins: PriceBins,
  rebatePct = 0.015,
): LevelPnl {
  const n = bins.centers.length;
  const mk = () => new Array(n).fill(0);
  const spreadLong = mk();
  const spreadShort = mk();
  const rebates = mk();
  const openLong = mk();
  const openShort = mk();

  for (const m of grid.fifo.matches) {
    const arr = m.side === "LONG" ? spreadLong : spreadShort;
    arr[binIndex(bins, m.level)] += m.spread;
  }
  for (const f of grid.fills) {
    rebates[binIndex(bins, f.price)] += (f.qvol * rebatePct) / 100;
  }
  for (const l of grid.fifo.open_lots) {
    const pnl =
      l.side === "BUY"
        ? (currentPrice - l.price) * l.base
        : (l.price - currentPrice) * l.base;
    const arr = l.side === "BUY" ? openLong : openShort;
    arr[binIndex(bins, l.price)] += pnl;
  }

  let incomeTotal = 0;
  let ilTotal = 0;
  let max = 0;
  for (let i = 0; i < n; i++) {
    incomeTotal +=
      spreadLong[i] + spreadShort[i] + rebates[i] +
      Math.max(0, openLong[i]) + Math.max(0, openShort[i]);
    ilTotal += -(Math.min(0, openLong[i]) + Math.min(0, openShort[i]));
    const right = spreadLong[i] + spreadShort[i] + rebates[i] +
      Math.max(0, openLong[i]) + Math.max(0, openShort[i]);
    const left = -(Math.min(0, openLong[i]) + Math.min(0, openShort[i]));
    max = Math.max(max, right, left);
  }
  return {
    spreadLong,
    spreadShort,
    rebates,
    openLong,
    openShort,
    incomeTotal,
    ilTotal,
    net: incomeTotal - ilTotal,
    max,
  };
}

// ── Canvas helpers (texturas accesibles) ─────────────────────────────────

const patternCache = new Map<string, CanvasPattern>();

/** Patrón rayado diagonal (accesible: textura para "abierto"). dir "\\" para
 * LONG, "/" para SHORT — distinguible incluso en blanco y negro. */
export function hatchPattern(
  ctx: CanvasRenderingContext2D,
  color: string,
  dir: "/" | "\\",
): CanvasPattern | string {
  const key = `${color}${dir}`;
  const cached = patternCache.get(key);
  if (cached) return cached;
  const size = 6;
  const off = document.createElement("canvas");
  off.width = size;
  off.height = size;
  const c = off.getContext("2d");
  if (!c) return color;
  c.strokeStyle = color;
  c.lineWidth = 1.4;
  c.beginPath();
  if (dir === "\\") {
    c.moveTo(-1, -1);
    c.lineTo(size + 1, size + 1);
    c.moveTo(-1, size - 1);
    c.lineTo(1, size + 1);
    c.moveTo(size - 1, -1);
    c.lineTo(size + 1, 1);
  } else {
    c.moveTo(-1, size + 1);
    c.lineTo(size + 1, -1);
    c.moveTo(-1, 1);
    c.lineTo(1, -1);
    c.moveTo(size - 1, size + 1);
    c.lineTo(size + 1, size - 1);
  }
  c.stroke();
  const pattern = ctx.createPattern(off, "repeat");
  if (!pattern) return color;
  patternCache.set(key, pattern);
  return pattern;
}

/** Formato compacto de números para labels del canvas. */
export function fmtCompact(v: number, dec = 1): string {
  const a = Math.abs(v);
  if (a >= 1_000_000) return `${(v / 1_000_000).toFixed(dec)}M`;
  if (a >= 1_000) return `${(v / 1_000).toFixed(dec)}k`;
  return v.toFixed(a >= 100 ? 0 : dec);
}

// ── Agregador de operaciones (matemática portada de chessboard_lite_lab) ──
//
// Cada grilla lite es un rectángulo de densidad: 100% base en su low → 100%
// quote en su high (lineal). El PnL vs HOLD es el costo de convexidad:
// dNAV = base(p)·dp → PnL(P) = ∫[p0→P] (base(p) − base(p0)) dp (trapezoidal).

export interface AggSpec {
  low: number;
  high: number;
  liquidity: number;
}

export interface AggregatorData {
  prices: number[];
  /** Capital en base (quote) de los bots activos, por precio. */
  actBaseVal: number[];
  /** Ídem con el ajuste aplicado (null si no hay draft). */
  adjBaseVal: number[] | null;
  actPct: number[];
  adjPct: number[] | null;
  actPnl: number[];
  adjPnl: number[] | null;
}

function baseValProfile(prices: number[], specs: AggSpec[]): number[] {
  return prices.map((p) =>
    specs.reduce((acc, s) => {
      const frac = Math.min(1, Math.max(0, (s.high - p) / Math.max(s.high - s.low, 1e-9)));
      return acc + s.liquidity * frac;
    }, 0),
  );
}

function pnlVsHold(prices: number[], baseUnits: number[], p0: number): number[] {
  let i0 = 0;
  for (let i = 1; i < prices.length; i++) {
    if (Math.abs(prices[i] - p0) < Math.abs(prices[i0] - p0)) i0 = i;
  }
  const diff = baseUnits.map((b) => b - baseUnits[i0]);
  const pnl = new Array(prices.length).fill(0);
  for (let i = i0 + 1; i < prices.length; i++) {
    pnl[i] = pnl[i - 1] + 0.5 * (diff[i] + diff[i - 1]) * (prices[i] - prices[i - 1]);
  }
  for (let i = i0 - 1; i >= 0; i--) {
    pnl[i] = pnl[i + 1] - 0.5 * (diff[i] + diff[i + 1]) * (prices[i + 1] - prices[i]);
  }
  return pnl;
}

/**
 * Series del agregador: bots activos vs "con ajuste" (draft reemplaza a la
 * grilla replaceId; con replaceId vacío el draft es aditivo, como en la lab).
 */
export function aggregatorData(
  grids: { id: string; low: number; high: number; liquidity: number }[],
  draft: { replaceId: string; low: number; high: number; liquidity: number } | null,
  currentPrice: number,
  nPts = 240,
): AggregatorData | null {
  if (!grids.length && !draft) return null;
  const act: AggSpec[] = grids.map((g) => ({ low: g.low, high: g.high, liquidity: g.liquidity }));
  const adj: AggSpec[] | null = draft
    ? [
        ...grids.filter((g) => g.id !== draft.replaceId).map((g) => ({ low: g.low, high: g.high, liquidity: g.liquidity })),
        { low: draft.low, high: draft.high, liquidity: draft.liquidity },
      ]
    : null;

  const all = [...act, ...(adj ?? [])];
  if (!all.length) return null;
  const lo = Math.min(...all.map((s) => s.low), currentPrice) * 0.985;
  const hi = Math.max(...all.map((s) => s.high), currentPrice) * 1.015;
  const prices: number[] = [];
  for (let i = 0; i <= nPts; i++) prices.push(lo + ((hi - lo) * i) / nPts);

  const cap = (specs: AggSpec[]) => specs.reduce((a, s) => a + s.liquidity, 0);
  const mk = (specs: AggSpec[]) => {
    const baseVal = baseValProfile(prices, specs);
    const units = baseVal.map((v, i) => v / prices[i]);
    const total = cap(specs);
    return {
      baseVal,
      pct: baseVal.map((v) => (total > 0 ? (v / total) * 100 : 0)),
      pnl: pnlVsHold(prices, units, currentPrice),
    };
  };

  const a = mk(act);
  const d = adj ? mk(adj) : null;
  return {
    prices,
    actBaseVal: a.baseVal,
    adjBaseVal: d?.baseVal ?? null,
    actPct: a.pct,
    adjPct: d?.pct ?? null,
    actPnl: a.pnl,
    adjPnl: d?.pnl ?? null,
  };
}

// ── Contrato del cuadro de impacto (Fase A live-ops) ──

export type ImpactTag = "green" | "yellow" | "red";

export interface ImpactItem {
  name: string;
  current: number | string | null;
  proposed: number | string | null;
  changed: boolean;
  tag: ImpactTag | null;
  rule?: string | null;
  reason?: string | null;
}

export interface ImpactRebalance {
  side: "BUY" | "SELL";
  base_amount: number;
  quote_value: number;
  from_inv_pct: number;
  to_inv_pct: number;
  il_realized_quote: number;
  est_fee_quote: number;
  payback_days: number | null;
  tag: ImpactTag | null;
  rule?: string | null;
  reason?: string | null;
}

export interface ImpactOrphan {
  open_quote: number;
  be_buy: number | null;
  be_sell: number | null;
  be_inside_new_range: boolean;
  tag: ImpactTag | null;
  rule?: string | null;
  reason?: string | null;
}

export interface ChessboardImpact {
  grid_id: string;
  rules_version: string;
  verdict: ImpactTag;
  params: ImpactItem[];
  derived: ImpactItem[];
  rebalance: ImpactRebalance | null;
  orphan: ImpactOrphan;
  economics: { old: Record<string, number>; new: Record<string, number> };
}
