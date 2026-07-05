import { useCallback, useEffect, useRef, useState } from "react";

import type { CandleData } from "@/lib/api";
import {
  CB_AMBER,
  CB_BLUE,
  CB_GREEN_TOT,
  CB_PINK,
  CB_RED_TOT,
  type ChessboardGrid,
  fmtCompact,
  gridVolumeByPrice,
  gridVolumeOverTime,
  hatchPattern,
  inventoryProfile,
  levelPnl,
  makeBins,
  volumeProfile,
} from "@/lib/chessboard";

export type ChessboardView = "market" | "activity" | "levels";

interface ChessboardChartProps {
  candles: CandleData[];
  grids: ChessboardGrid[]; // grillas del par graficado
  selectedGrid: ChessboardGrid | null; // para activity/levels
  currentPrice: number;
  view: ChessboardView;
  interval: string;
  tradingPair: string;
  rebatePct?: number;
  height?: number;
  /** Draft de ajuste de la grilla seleccionada: banda punteada editable. */
  draft?: { low: number; high: number } | null;
  /** Con draft activo, el drag de min/max/banda entera reporta acá. */
  onDraftChange?: (d: { low: number; high: number }) => void;
}

// Tonos distintos por grilla para el sombreado de bandas (el id va rotulado,
// el color no es el único canal).
const BAND_COLORS: [number, number, number][] = [
  [56, 189, 248], // sky
  [167, 139, 250], // violet
  [52, 211, 153], // emerald
  [244, 114, 182], // pink
  [250, 204, 21], // amber
  [96, 165, 250], // blue
];

const SIDE_W = 280;
const BOTTOM_H = 96;

const INTERVAL_SECONDS: Record<string, number> = {
  "1m": 60,
  "5m": 300,
  "15m": 900,
  "1h": 3600,
  "4h": 14400,
};

function chartColors() {
  const s = getComputedStyle(document.documentElement);
  return {
    bg: s.getPropertyValue("--chart-bg").trim() || "#0f1525",
    grid: s.getPropertyValue("--chart-grid").trim() || "#1c2541",
    text: s.getPropertyValue("--chart-text").trim() || "#6b7994",
    up: s.getPropertyValue("--chart-up").trim() || "#22c55e",
    down: s.getPropertyValue("--chart-down").trim() || "#ef4444",
  };
}

function isoToSec(iso: string | null): number | null {
  if (!iso) return null;
  const t = Date.parse(iso);
  return Number.isNaN(t) ? null : Math.floor(t / 1000);
}

/** Prepara un canvas para DPR y devuelve su contexto + tamaño CSS. */
function setupCanvas(canvas: HTMLCanvasElement) {
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth;
  const h = canvas.clientHeight;
  canvas.width = w * dpr;
  canvas.height = h * dpr;
  const ctx = canvas.getContext("2d");
  if (!ctx) return null;
  ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, w, h);
  return { ctx, w, h };
}

export function ChessboardChart({
  candles,
  grids,
  selectedGrid,
  currentPrice,
  view,
  interval,
  tradingPair,
  rebatePct = 0.015,
  height = 560,
  draft = null,
  onDraftChange,
}: ChessboardChartProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const bandCanvasRef = useRef<HTMLCanvasElement>(null);
  const sideCanvasRef = useRef<HTMLCanvasElement>(null);
  const bottomCanvasRef = useRef<HTMLCanvasElement>(null);
  const chartRef = useRef<import("lightweight-charts").IChartApi | null>(null);
  const seriesRef = useRef<import("lightweight-charts").ISeriesApi<"Candlestick"> | null>(null);
  const rangeSeriesRef = useRef<import("lightweight-charts").ISeriesApi<"Line"> | null>(null);
  const priceLineRef = useRef<import("lightweight-charts").IPriceLine | null>(null);
  const modRef = useRef<typeof import("lightweight-charts") | null>(null);
  const initializedRef = useRef(false);
  const rafRef = useRef(0);
  const [chartReady, setChartReady] = useState(false);
  const [fullscreen, setFullscreen] = useState(false);

  // Los draws leen SIEMPRE los props más recientes vía ref (sin resuscribir).
  const propsRef = useRef({ candles, grids, selectedGrid, currentPrice, view, rebatePct, interval, draft, onDraftChange });
  propsRef.current = { candles, grids, selectedGrid, currentPrice, view, rebatePct, interval, draft, onDraftChange };
  // Drag en curso sobre el draft: qué agarraste y (para "move") el offset.
  const dragRef = useRef<{ edge: "low" | "high" | "move"; grabPrice: number } | null>(null);

  const quote = tradingPair.includes("-") ? tradingPair.split("-")[1] : "quote";

  // ── Dibujo: bandas de grilla sobre el chart ──────────────────────────
  const drawBands = useCallback(() => {
    const canvas = bandCanvasRef.current;
    const chart = chartRef.current;
    const series = seriesRef.current;
    if (!canvas || !chart || !series) return;
    const setup = setupCanvas(canvas);
    if (!setup) return;
    const { ctx, w } = setup;
    const { grids: gs } = propsRef.current;
    const ts = chart.timeScale();

    gs.forEach((g, i) => {
      const [r, gg, b] = BAND_COLORS[i % BAND_COLORS.length];
      const yTop = series.priceToCoordinate(g.high);
      const yBot = series.priceToCoordinate(g.low);
      if (yTop === null || yBot === null) return;

      // El sombreado es un RECTÁNGULO que arranca en el deploy del bot, no una
      // sombra infinita. timeToCoordinate solo mapea tiempos que existen como
      // vela → snapear el deploy a la primera vela >= deploy.
      let x0 = 0;
      const dep = isoToSec(g.deployed_at);
      const cs = propsRef.current.candles;
      if (dep !== null && cs.length) {
        const snap = cs.find((c) => (c.timestamp > 1e12 ? c.timestamp / 1000 : c.timestamp) >= dep);
        if (snap) {
          const t = snap.timestamp > 1e12 ? Math.floor(snap.timestamp / 1000) : snap.timestamp;
          const x = ts.timeToCoordinate(t as import("lightweight-charts").UTCTimestamp);
          if (x !== null) x0 = Math.max(0, x);
        } else {
          // Deploy posterior a la última vela cargada → nace en el borde derecho.
          x0 = w;
        }
      }

      ctx.fillStyle = `rgba(${r},${gg},${b},0.10)`;
      ctx.fillRect(x0, yTop, w - x0, yBot - yTop);
      ctx.strokeStyle = `rgba(${r},${gg},${b},0.75)`;
      ctx.lineWidth = 1;
      ctx.setLineDash([4, 3]);
      for (const y of [yTop, yBot]) {
        ctx.beginPath();
        ctx.moveTo(x0, y);
        ctx.lineTo(w, y);
        ctx.stroke();
      }
      ctx.setLineDash([]);
      // Marcador de deploy + rótulo del id (posición + texto, no solo color).
      if (x0 > 0) {
        ctx.beginPath();
        ctx.moveTo(x0, yTop);
        ctx.lineTo(x0, yBot);
        ctx.strokeStyle = `rgba(${r},${gg},${b},0.9)`;
        ctx.stroke();
      }
      ctx.fillStyle = `rgba(${r},${gg},${b},1)`;
      ctx.font = "10px ui-monospace, monospace";
      ctx.textAlign = "left";
      ctx.fillText(`${g.id}`, x0 + 4, yTop + 12);
    });

    // ── Draft de ajuste: banda azul punteada con manijas (encima de todo) ──
    const { draft: d } = propsRef.current;
    if (d) {
      const yT = series.priceToCoordinate(d.high);
      const yB = series.priceToCoordinate(d.low);
      if (yT !== null && yB !== null) {
        ctx.fillStyle = "rgba(55,138,221,0.10)";
        ctx.fillRect(0, yT, w, yB - yT);
        ctx.strokeStyle = "#378ADD";
        ctx.lineWidth = 2;
        ctx.setLineDash([7, 5]);
        for (const y of [yT, yB]) {
          ctx.beginPath();
          ctx.moveTo(0, y);
          ctx.lineTo(w, y);
          ctx.stroke();
        }
        ctx.setLineDash([]);
        // Manijas: rectángulos sólidos al centro de cada borde (agarrables).
        ctx.fillStyle = "#378ADD";
        for (const y of [yT, yB]) ctx.fillRect(w / 2 - 18, y - 3, 36, 6);
        ctx.font = "10px ui-monospace, monospace";
        ctx.textAlign = "right";
        const fmt = (v: number) => (v >= 1000 ? v.toLocaleString(undefined, { maximumFractionDigits: 0 }) : v.toFixed(2));
        ctx.fillText(`ajuste max ${fmt(d.high)}`, w - 8, yT - 5);
        ctx.fillText(`ajuste min ${fmt(d.low)}`, w - 8, yB + 13);
      }
    }
  }, []);

  // ── Dibujo: panel lateral (comparte eje de precio con el chart) ──────
  const drawSide = useCallback(() => {
    const canvas = sideCanvasRef.current;
    const chart = chartRef.current;
    const series = seriesRef.current;
    if (!canvas || !chart || !series) return;
    const setup = setupCanvas(canvas);
    if (!setup) return;
    const { ctx, w, h } = setup;
    const { candles: cs, grids: gs, selectedGrid: sel, currentPrice: px, view: v, rebatePct: rb } =
      propsRef.current;
    const colors = chartColors();

    const y = (price: number) => series.priceToCoordinate(price);

    // Línea del precio actual, cruzando el panel.
    const drawPriceLine = () => {
      const yp = y(px);
      if (yp === null) return;
      ctx.strokeStyle = "#facc15";
      ctx.setLineDash([4, 3]);
      ctx.beginPath();
      ctx.moveTo(0, yp);
      ctx.lineTo(w, yp);
      ctx.stroke();
      ctx.setLineDash([]);
    };

    const label = (text: string, x: number, yy: number, color: string, align: CanvasTextAlign = "left") => {
      ctx.fillStyle = color;
      ctx.font = "9px ui-monospace, monospace";
      ctx.textAlign = align;
      ctx.fillText(text, x, yy);
    };

    if (v === "market") {
      // Volumen del mercado por precio + inventario esperado (curva blanca).
      if (!gs.length || !cs.length) return;
      // Bins sobre el rango visible del precio ∪ rangos de grillas.
      const range = chart.timeScale().getVisibleRange();
      const visible = range
        ? cs.filter((c) => {
            const t = c.timestamp > 1e12 ? c.timestamp / 1000 : c.timestamp;
            return t >= (range.from as number) && t <= (range.to as number);
          })
        : cs;
      if (!visible.length) return;
      let lo = Math.min(...visible.map((c) => c.low), ...gs.map((g) => g.low));
      let hi = Math.max(...visible.map((c) => c.high), ...gs.map((g) => g.high));
      const pad = (hi - lo) * 0.01;
      lo -= pad;
      hi += pad;
      const bins = makeBins(lo, hi, 50);
      const vp = volumeProfile(visible, bins);
      const inv = inventoryProfile(gs, bins);

      for (let i = 0; i < bins.centers.length; i++) {
        const y0 = y(bins.edges[i + 1]);
        const y1 = y(bins.edges[i]);
        if (y0 === null || y1 === null) continue;
        const bh = Math.max(1, y1 - y0 - 1);
        // Volumen (gris-celeste, POC en ámbar): desde el borde izquierdo.
        if (vp.max > 0 && vp.volumes[i] > 0) {
          const bw = (vp.volumes[i] / vp.max) * (w - 46);
          ctx.fillStyle = i === vp.poc ? "rgba(250,204,21,0.9)" : "rgba(124,196,255,0.35)";
          ctx.fillRect(0, y0, bw, bh);
          if (i === vp.poc) label("POC", bw + 3, y0 + bh / 2 + 3, "#facc15");
        }
      }
      // Curva de inventario esperado: % base → ancho del panel [0,100%].
      ctx.strokeStyle = "#e2e8f0";
      ctx.lineWidth = 1.6;
      ctx.beginPath();
      let started = false;
      for (let i = 0; i < bins.centers.length; i++) {
        const yy = y(bins.centers[i]);
        if (yy === null) continue;
        const x = (inv.pctBase[i] / 100) * (w - 8) + 4;
        if (!started) {
          ctx.moveTo(x, yy);
          started = true;
        } else ctx.lineTo(x, yy);
      }
      ctx.stroke();
      // Banda dura de inventario (piso/techo %) como guías verticales.
      const band = sel ?? gs[0];
      if (band) {
        for (const [pct, name] of [
          [band.inv_floor, "piso"],
          [band.inv_ceiling, "techo"],
        ] as const) {
          const x = (Number(pct) / 100) * (w - 8) + 4;
          ctx.strokeStyle = "rgba(251,191,36,0.6)";
          ctx.setLineDash([3, 3]);
          ctx.beginPath();
          ctx.moveTo(x, 0);
          ctx.lineTo(x, h - 14);
          ctx.stroke();
          ctx.setLineDash([]);
          label(`${name} ${Number(pct).toFixed(0)}%`, x + 2, h - 16, "#fbbf24");
        }
      }
      drawPriceLine();
      label("vol mercado ░ · inventario esperado —— (0→100% base)", 4, h - 4, colors.text);
    } else if (v === "activity") {
      // Volumen generado por la grilla, en espejo: SELL ◄ | ► BUY,
      // matcheado sólido / abierto rayado.
      if (!sel) {
        label("elegí una grilla", 8, 16, colors.text);
        return;
      }
      const refs = [
        sel.low,
        sel.high,
        ...sel.fills.map((f) => f.price),
      ];
      const lo = Math.min(...refs) * 0.999;
      const hi = Math.max(...refs) * 1.001;
      const nb = Math.min(40, Math.max(10, sel.n_levels || 20));
      const bins = makeBins(lo, hi, nb);
      const gv = gridVolumeByPrice(sel, bins);
      const mid = w / 2;
      const half = mid - 8;
      const totH = 34; // franja inferior para totalizadores

      ctx.strokeStyle = colors.grid;
      ctx.beginPath();
      ctx.moveTo(mid, 0);
      ctx.lineTo(mid, h - totH - 14);
      ctx.stroke();

      for (let i = 0; i < bins.centers.length; i++) {
        const y0 = y(bins.edges[i + 1]);
        const y1 = y(bins.edges[i]);
        if (y0 === null || y1 === null) continue;
        const bh = Math.max(1, y1 - y0 - 1);
        if (gv.max <= 0) continue;
        const sc = half / gv.max;
        // BUY (derecha, azul): matcheado sólido + abierto rayado.
        const bm = gv.buyMatched[i] * sc;
        const bo = gv.buyOpen[i] * sc;
        if (bm > 0) {
          ctx.fillStyle = CB_BLUE;
          ctx.fillRect(mid + 1, y0, bm, bh);
        }
        if (bo > 0) {
          ctx.fillStyle = hatchPattern(ctx, CB_BLUE, "\\");
          ctx.fillRect(mid + 1 + bm, y0, bo, bh);
        }
        // SELL (izquierda, ámbar).
        const sm = gv.sellMatched[i] * sc;
        const so = gv.sellOpen[i] * sc;
        if (sm > 0) {
          ctx.fillStyle = CB_AMBER;
          ctx.fillRect(mid - 1 - sm, y0, sm, bh);
        }
        if (so > 0) {
          ctx.fillStyle = hatchPattern(ctx, CB_AMBER, "/");
          ctx.fillRect(mid - 1 - sm - so, y0, so, bh);
        }
      }
      drawPriceLine();

      // Totalizadores en espejo, misma gramática que las barras: SELL a la
      // izquierda / BUY a la derecha, sólido = matcheado, rayado = abierto.
      const sum = (a: number[]) => a.reduce((x, y) => x + y, 0);
      const totSellM = sum(gv.sellMatched);
      const totSellO = sum(gv.sellOpen);
      const totBuyM = sum(gv.buyMatched);
      const totBuyO = sum(gv.buyOpen);
      const maxTot = Math.max(totSellM + totSellO, totBuyM + totBuyO);
      const tsc = maxTot > 0 ? (half - 44) / maxTot : 0;
      const ty = h - totH - 8;

      // Σ SELL (ámbar, hacia la izquierda): sólido pegado al eje, rayado después.
      let xs = mid - 1;
      if (totSellM > 0) {
        ctx.fillStyle = CB_AMBER;
        ctx.fillRect(xs - totSellM * tsc, ty, totSellM * tsc, 10);
        xs -= totSellM * tsc;
      }
      if (totSellO > 0) {
        ctx.fillStyle = hatchPattern(ctx, CB_AMBER, "/");
        ctx.fillRect(xs - totSellO * tsc, ty, totSellO * tsc, 10);
        xs -= totSellO * tsc;
      }
      label(`Σ ${fmtCompact(totSellM + totSellO)}`, xs - 3, ty + 8, CB_AMBER, "right");

      // Σ BUY (azul, hacia la derecha).
      let xb = mid + 1;
      if (totBuyM > 0) {
        ctx.fillStyle = CB_BLUE;
        ctx.fillRect(xb, ty + 12, totBuyM * tsc, 10);
        xb += totBuyM * tsc;
      }
      if (totBuyO > 0) {
        ctx.fillStyle = hatchPattern(ctx, CB_BLUE, "\\");
        ctx.fillRect(xb, ty + 12, totBuyO * tsc, 10);
        xb += totBuyO * tsc;
      }
      label(`Σ ${fmtCompact(totBuyM + totBuyO)}`, xb + 3, ty + 20, CB_BLUE);

      const totM = totSellM + totBuyM;
      const totO = totSellO + totBuyO;
      const mPct = totM + totO > 0 ? (totM / (totM + totO)) * 100 : 0;
      label(
        `matcheado ${fmtCompact(totM)} (${mPct.toFixed(0)}%) · abierto ${fmtCompact(totO)} ${quote}`,
        4,
        h - 14,
        colors.text,
      );
      label(`◄ SELL (ámbar) · BUY (azul) ► · sólido=matcheado · rayado=abierto`, 4, h - 4, colors.text);
    } else {
      // "levels": IL vs spreads capturados + rebates, por nivel.
      if (!sel) {
        label("elegí una grilla", 8, 16, colors.text);
        return;
      }
      const refs = [
        sel.low,
        sel.high,
        ...sel.fifo.matches.map((m) => m.level),
        ...sel.fifo.open_lots.map((l) => l.price),
      ];
      const lo = Math.min(...refs) * 0.999;
      const hi = Math.max(...refs) * 1.001;
      const nb = Math.min(40, Math.max(10, sel.n_levels || 20));
      const bins = makeBins(lo, hi, nb);
      const lp = levelPnl(sel, propsRef.current.currentPrice, bins, rb);

      const totH = 34; // franja inferior para totalizadores
      const zero = w * 0.42; // eje 0: izquierda = IL, derecha = ingreso
      const sc = lp.max > 0 ? Math.min((w - zero - 6) / lp.max, (zero - 6) / lp.max) : 0;

      ctx.strokeStyle = "#484f58";
      ctx.beginPath();
      ctx.moveTo(zero, 0);
      ctx.lineTo(zero, h - totH - 14);
      ctx.stroke();

      for (let i = 0; i < bins.centers.length; i++) {
        const y0 = y(bins.edges[i + 1]);
        const y1 = y(bins.edges[i]);
        if (y0 === null || y1 === null) continue;
        const bh = Math.max(1, y1 - y0 - 1);
        // Cobrado (sólido, derecha): spread LONG azul + SHORT ámbar + rebates rosa.
        let x = zero + 1;
        for (const [val, color] of [
          [lp.spreadLong[i], CB_BLUE],
          [lp.spreadShort[i], CB_AMBER],
          [lp.rebates[i], CB_PINK],
        ] as const) {
          const bw = Number(val) * sc;
          if (bw > 0.3) {
            ctx.fillStyle = String(color);
            ctx.fillRect(x, y0, bw, bh);
            x += bw;
          }
        }
        // Abierto (rayado, ±): der = ganando, izq = perdiendo.
        let xNeg = zero - 1;
        for (const [val, color, dir] of [
          [lp.openLong[i], CB_BLUE, "\\"],
          [lp.openShort[i], CB_AMBER, "/"],
        ] as const) {
          const vv = Number(val);
          if (Math.abs(vv) * sc < 0.3) continue;
          ctx.fillStyle = hatchPattern(ctx, String(color), dir as "/" | "\\");
          if (vv >= 0) {
            ctx.fillRect(x, y0, vv * sc, bh);
            x += vv * sc;
          } else {
            const bw = -vv * sc;
            ctx.fillRect(xNeg - bw, y0, bw, bh);
            xNeg -= bw;
          }
        }
      }
      drawPriceLine();

      // Totalizadores en la misma escala X: verde = Σ ingreso, rojo = Σ IL.
      const ty = h - totH - 10;
      const tsc = Math.max(lp.incomeTotal, lp.ilTotal) > 0
        ? Math.min((w - zero - 40) / Math.max(lp.incomeTotal, 1e-9), (zero - 40) / Math.max(lp.ilTotal, 1e-9))
        : 0;
      ctx.fillStyle = CB_RED_TOT;
      ctx.fillRect(zero - lp.ilTotal * tsc, ty, lp.ilTotal * tsc, 10);
      label(`−${fmtCompact(lp.ilTotal)}`, zero - lp.ilTotal * tsc - 3, ty + 8, CB_RED_TOT, "right");
      ctx.fillStyle = CB_GREEN_TOT;
      ctx.fillRect(zero + 1, ty + 12, lp.incomeTotal * tsc, 10);
      label(`+${fmtCompact(lp.incomeTotal)}`, zero + lp.incomeTotal * tsc + 3, ty + 20, CB_GREEN_TOT);
      label(
        `neto ${lp.net >= 0 ? "+" : ""}${fmtCompact(lp.net)} ${quote} vs HOLD`,
        4,
        h - 4,
        lp.net >= 0 ? CB_GREEN_TOT : CB_RED_TOT,
      );
    }
  }, [quote]);

  // ── Dibujo: panel inferior (volumen por grilla en el tiempo) ─────────
  const drawBottom = useCallback(() => {
    const canvas = bottomCanvasRef.current;
    const chart = chartRef.current;
    if (!canvas || !chart) return;
    const setup = setupCanvas(canvas);
    if (!setup) return;
    const { ctx, w, h } = setup;
    const { selectedGrid: sel, view: v, interval: iv } = propsRef.current;
    if (v !== "activity" || !sel) return;
    const colors = chartColors();
    const ts = chart.timeScale();

    const range = ts.getVisibleRange();
    if (!range) return;
    // Bucket FIJO = intervalo de las velas → los fills caen en el mismo
    // casillero temporal que su vela y el panel no "re-bucketea" al zoomear.
    const bucket = INTERVAL_SECONDS[iv] ?? 300;
    const pts = gridVolumeOverTime(sel, bucket);
    if (!pts.length) return;
    const maxV = Math.max(...pts.map((p) => Math.max(p.buy, p.sell)), 1e-9);
    const mid = h / 2;

    ctx.strokeStyle = colors.grid;
    ctx.beginPath();
    ctx.moveTo(0, mid);
    ctx.lineTo(w, mid);
    ctx.stroke();

    // Ancho de barra en px: distancia entre dos buckets consecutivos, medida
    // sobre un bucket DENTRO del rango visible (fuera devuelve null).
    let bw = 3;
    const tRef = Math.ceil((range.from as number) / bucket) * bucket + bucket;
    const xr0 = ts.timeToCoordinate(tRef as import("lightweight-charts").UTCTimestamp);
    const xr1 = ts.timeToCoordinate((tRef + bucket) as import("lightweight-charts").UTCTimestamp);
    if (xr0 !== null && xr1 !== null) bw = Math.max(1, Math.abs(xr1 - xr0) - 1);

    for (const p of pts) {
      const x = ts.timeToCoordinate(p.time as import("lightweight-charts").UTCTimestamp);
      if (x === null) continue;
      // timeToCoordinate devuelve el CENTRO de la vela → barra centrada,
      // alineada 1:1 con su vela de arriba.
      const xl = x - bw / 2;
      // BUY hacia arriba (azul), SELL hacia abajo (ámbar): posición + color.
      if (p.buy > 0) {
        const bh = (p.buy / maxV) * (mid - 12);
        ctx.fillStyle = CB_BLUE;
        ctx.fillRect(xl, mid - bh, bw, bh);
      }
      if (p.sell > 0) {
        const bh = (p.sell / maxV) * (mid - 12);
        ctx.fillStyle = CB_AMBER;
        ctx.fillRect(xl, mid + 1, bw, bh);
      }
    }
    ctx.fillStyle = colors.text;
    ctx.font = "9px ui-monospace, monospace";
    ctx.textAlign = "left";
    ctx.fillText(`vol ${sel.id} en el tiempo · ▲ BUY (azul) · ▼ SELL (ámbar) · max ${fmtCompact(maxV)} ${quote}`, 4, 10);
  }, [quote]);

  const scheduleDraw = useCallback(() => {
    cancelAnimationFrame(rafRef.current);
    rafRef.current = requestAnimationFrame(() => {
      drawBands();
      drawSide();
      drawBottom();
    });
  }, [drawBands, drawSide, drawBottom]);

  // ── Init del chart ────────────────────────────────────────────────────
  useEffect(() => {
    let cancelled = false;
    import("lightweight-charts").then((mod) => {
      if (cancelled || !containerRef.current) return;
      modRef.current = mod;
      const colors = chartColors();
      const chart = mod.createChart(containerRef.current, {
        autoSize: true,
        layout: {
          background: { type: mod.ColorType.Solid, color: colors.bg },
          textColor: colors.text,
        },
        grid: {
          vertLines: { color: colors.grid },
          horzLines: { color: colors.grid },
        },
        crosshair: { mode: mod.CrosshairMode.Normal },
        timeScale: { timeVisible: true, secondsVisible: false },
        rightPriceScale: { borderVisible: false },
        localization: {
          priceFormatter: (p: number) =>
            Math.abs(p) >= 1000 ? p.toFixed(0) : Math.abs(p) >= 1 ? p.toFixed(4) : p.toPrecision(6),
        },
      });
      chartRef.current = chart;
      const series = chart.addSeries(mod.CandlestickSeries, {
        upColor: colors.up,
        downColor: colors.down,
        wickUpColor: colors.up,
        wickDownColor: colors.down,
        borderVisible: false,
      });
      seriesRef.current = series;
      // Serie invisible con los extremos de las grillas: el autoscale del eje
      // de precio solo mira series, y las bandas son un canvas que no ve — sin
      // esto, una grilla más ancha que las velas queda cortada sin remedio.
      rangeSeriesRef.current = chart.addSeries(mod.LineSeries, {
        color: "rgba(0,0,0,0)",
        lineWidth: 1,
        priceLineVisible: false,
        lastValueVisible: false,
        crosshairMarkerVisible: false,
      });
      chart.timeScale().subscribeVisibleLogicalRangeChange(scheduleDraw);
      chart.subscribeCrosshairMove(scheduleDraw);
      setChartReady(true);
    });
    return () => {
      cancelled = true;
      cancelAnimationFrame(rafRef.current);
      if (chartRef.current) {
        chartRef.current.remove();
        chartRef.current = null;
        seriesRef.current = null;
        rangeSeriesRef.current = null;
        priceLineRef.current = null;
      }
      setChartReady(false);
    };
  }, [scheduleDraw]);

  // ── Datos de velas ────────────────────────────────────────────────────
  useEffect(() => {
    if (!chartReady || !seriesRef.current || !candles.length) return;
    const mapped = candles.map((c) => ({
      time: (c.timestamp > 1e12 ? Math.floor(c.timestamp / 1000) : c.timestamp) as import("lightweight-charts").UTCTimestamp,
      open: c.open,
      high: c.high,
      low: c.low,
      close: c.close,
    }));
    seriesRef.current.setData(mapped);
    if (!initializedRef.current) {
      chartRef.current?.timeScale().fitContent();
      initializedRef.current = true;
    }
    scheduleDraw();
  }, [candles, chartReady, scheduleDraw]);

  useEffect(() => {
    initializedRef.current = false;
  }, [tradingPair, interval]);

  // ── Línea de precio actual ────────────────────────────────────────────
  useEffect(() => {
    const series = seriesRef.current;
    const mod = modRef.current;
    if (!series || !mod || !chartReady || !currentPrice) return;
    if (priceLineRef.current) series.removePriceLine(priceLineRef.current);
    priceLineRef.current = series.createPriceLine({
      price: currentPrice,
      color: "#facc15",
      lineWidth: 1,
      lineStyle: mod.LineStyle.Dashed,
      axisLabelVisible: true,
      title: "precio",
    });
    scheduleDraw();
  }, [currentPrice, chartReady, scheduleDraw]);

  // Extremos de las grillas → serie invisible, para que el autoscale los incluya.
  useEffect(() => {
    const rs = rangeSeriesRef.current;
    if (!rs || !chartReady) return;
    if (!grids.length || candles.length < 2) {
      rs.setData([]);
      return;
    }
    const toSec = (t: number) => (t > 1e12 ? Math.floor(t / 1000) : t);
    const t0 = toSec(candles[0].timestamp);
    const t1 = toSec(candles[candles.length - 1].timestamp);
    const lo = Math.min(...grids.map((g) => g.low));
    const hi = Math.max(...grids.map((g) => g.high));
    type TS = import("lightweight-charts").UTCTimestamp;
    rs.setData([
      { time: t0 as TS, value: lo },
      { time: t1 as TS, value: hi },
    ]);
  }, [grids, candles, chartReady]);

  // Redibujar cuando cambian datos/vista/grilla/draft.
  useEffect(() => {
    if (chartReady) scheduleDraw();
  }, [grids, selectedGrid, view, chartReady, draft, scheduleDraw]);

  // ── Drag del draft: manijas en min/max, banda entera desde el medio ────
  const draftHitTest = useCallback((y: number): "low" | "high" | "move" | null => {
    const s = seriesRef.current;
    const d = propsRef.current.draft;
    if (!s || !d) return null;
    const yT = s.priceToCoordinate(d.high);
    const yB = s.priceToCoordinate(d.low);
    if (yT === null || yB === null) return null;
    if (Math.abs(y - yT) < 8) return "high";
    if (Math.abs(y - yB) < 8) return "low";
    if (y > yT && y < yB) return "move";
    return null;
  }, []);

  const overlayY = (e: React.MouseEvent<HTMLDivElement>) =>
    e.clientY - e.currentTarget.getBoundingClientRect().top;

  const onDraftMouseDown = useCallback((e: React.MouseEvent<HTMLDivElement>) => {
    const s = seriesRef.current;
    const y = overlayY(e);
    const edge = draftHitTest(y);
    const p = s?.coordinateToPrice(y);
    if (edge && p != null) dragRef.current = { edge, grabPrice: p as number };
  }, [draftHitTest]);

  const onDraftMouseMove = useCallback((e: React.MouseEvent<HTMLDivElement>) => {
    const s = seriesRef.current;
    const d = propsRef.current.draft;
    const cb = propsRef.current.onDraftChange;
    const el = e.currentTarget;
    const y = overlayY(e);
    if (!s || !d || !cb) return;
    const drag = dragRef.current;
    if (!drag) {
      const hit = draftHitTest(y);
      el.style.cursor = hit === "move" ? "grab" : hit ? "ns-resize" : "default";
      return;
    }
    const p = s.coordinateToPrice(y);
    if (p == null) return;
    const price = p as number;
    if (drag.edge === "high") cb({ low: d.low, high: Math.max(price, d.low * 1.001) });
    else if (drag.edge === "low") cb({ low: Math.min(price, d.high * 0.999), high: d.high });
    else {
      const delta = price - drag.grabPrice;
      dragRef.current = { edge: "move", grabPrice: price };
      cb({ low: d.low + delta, high: d.high + delta });
    }
  }, [draftHitTest]);

  const endDraftDrag = useCallback(() => {
    dragRef.current = null;
  }, []);

  // ── Fullscreen ────────────────────────────────────────────────────────
  useEffect(() => {
    if (!fullscreen) return;
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape") setFullscreen(false);
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [fullscreen]);

  useEffect(() => {
    const timer = setTimeout(() => {
      chartRef.current?.timeScale().fitContent();
      scheduleDraw();
    }, 60);
    return () => clearTimeout(timer);
  }, [fullscreen, scheduleDraw]);

  const showBottom = view === "activity";

  return (
    <div
      className={
        fullscreen
          ? "fixed inset-0 z-50 flex flex-col bg-[var(--color-bg)] p-2"
          : "flex flex-col rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] overflow-hidden"
      }
    >
      <div className="flex items-center justify-between border-b border-[var(--color-border)] bg-[var(--color-bg)] px-3 py-1.5">
        <p className="text-[10px] text-[var(--color-text-muted)]">
          {tradingPair} · {interval} · {grids.length} grilla(s)
        </p>
        <button
          onClick={() => setFullscreen((f) => !f)}
          className="p-0.5 rounded hover:bg-[var(--color-surface-hover)] text-[var(--color-text-muted)] hover:text-[var(--color-text)]"
          title={fullscreen ? "Salir (Esc)" : "Fullscreen"}
        >
          <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            {fullscreen ? (
              <>
                <polyline points="4 14 10 14 10 20" /><polyline points="20 10 14 10 14 4" />
                <line x1="14" y1="10" x2="21" y2="3" /><line x1="3" y1="21" x2="10" y2="14" />
              </>
            ) : (
              <>
                <polyline points="15 3 21 3 21 9" /><polyline points="9 21 3 21 3 15" />
                <line x1="21" y1="3" x2="14" y2="10" /><line x1="3" y1="21" x2="10" y2="14" />
              </>
            )}
          </svg>
        </button>
      </div>

      <div className="flex" style={{ flex: fullscreen ? 1 : undefined, minHeight: 0 }}>
        {/* Chart + overlay de bandas */}
        <div style={{ position: "relative", flex: 1, minWidth: 0 }}>
          <div ref={containerRef} style={{ height: fullscreen ? "100%" : height, width: "100%" }} />
          <canvas
            ref={bandCanvasRef}
            style={{ position: "absolute", inset: 0, width: "100%", height: "100%", pointerEvents: "none", zIndex: 3 }}
          />
          {/* Overlay de edición: mientras hay draft, captura el mouse (el pan
              del chart queda pausado — la edición es un modo explícito). */}
          {draft && onDraftChange && (
            <div
              style={{ position: "absolute", inset: 0, zIndex: 4 }}
              onMouseDown={onDraftMouseDown}
              onMouseMove={onDraftMouseMove}
              onMouseUp={endDraftDrag}
              onMouseLeave={endDraftDrag}
            />
          )}
        </div>
        {/* Panel lateral sincronizado al eje de precio */}
        <div
          className="border-l border-[var(--color-border)]"
          style={{ width: SIDE_W, background: "var(--chart-bg, #0f1525)", position: "relative" }}
        >
          <canvas
            ref={sideCanvasRef}
            style={{ width: "100%", height: fullscreen ? "100%" : height, display: "block" }}
          />
        </div>
      </div>

      {/* Panel inferior: volumen de la grilla en el tiempo (modo actividad) */}
      {showBottom && (
        <div className="border-t border-[var(--color-border)]" style={{ background: "var(--chart-bg, #0f1525)" }}>
          <canvas ref={bottomCanvasRef} style={{ width: "100%", height: BOTTOM_H, display: "block" }} />
        </div>
      )}
    </div>
  );
}
