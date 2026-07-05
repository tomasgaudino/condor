import { useEffect, useMemo, useRef, useState } from "react";

import {
  aggregatorData,
  CB_AMBER,
  CB_BLUE,
  CB_PINK,
  fmtCompact,
  type ChessboardGrid,
} from "@/lib/chessboard";

// Paleta accesible: activos = ámbar sólido; con ajuste = azul PUNTEADO (nunca
// solo matiz); actual en gris fino; diferencia de PnL = rosa translúcido.
const GRAY = "#a3adb8";

interface AggregatorPanelProps {
  grids: ChessboardGrid[];
  /** Draft de ajuste: reemplaza a la grilla replaceId en las curvas "con ajuste". */
  draft: { replaceId: string; low: number; high: number; liquidity: number } | null;
  currentPrice: number;
  quote: string;
  height?: number;
}

function colors() {
  const s = getComputedStyle(document.documentElement);
  return {
    bg: s.getPropertyValue("--chart-bg").trim() || "#0f1525",
    grid: s.getPropertyValue("--chart-grid").trim() || "#1c2541",
    text: s.getPropertyValue("--chart-text").trim() || "#6b7994",
  };
}

const PANELS = [
  { key: "base", title: "Capital en base" },
  { key: "pct", title: "% base agregado" },
  { key: "pnl", title: "PnL vs HOLD" },
] as const;
const WIDTHS = [0.38, 0.27, 0.35];
const AXIS_W = 64; // eje de precio a la izquierda
const TOP = 22;
const BOT = 30;

export function AggregatorPanel({ grids, draft, currentPrice, quote, height = 480 }: AggregatorPanelProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const wrapRef = useRef<HTMLDivElement>(null);
  const [hoverY, setHoverY] = useState<number | null>(null);
  const [width, setWidth] = useState(0);

  const data = useMemo(
    () => aggregatorData(grids, draft, currentPrice),
    [grids, draft, currentPrice],
  );

  // El ancho puede ser 0 en el primer commit (mount por cambio de vista):
  // observamos el contenedor y redibujamos cuando el layout se asienta.
  useEffect(() => {
    const wrap = wrapRef.current;
    if (!wrap) return;
    const ro = new ResizeObserver((entries) => {
      for (const e of entries) setWidth(Math.floor(e.contentRect.width));
    });
    ro.observe(wrap);
    setWidth(wrap.clientWidth);
    return () => ro.disconnect();
  }, []);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas || !data || width <= 0) return;

    const w = width;
    const h = height;
    const dpr = window.devicePixelRatio || 1;
    canvas.width = w * dpr;
    canvas.height = h * dpr;
    canvas.style.width = `${w}px`;
    canvas.style.height = `${h}px`;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    ctx.scale(dpr, dpr);
    const c = colors();
    ctx.fillStyle = c.bg;
    ctx.fillRect(0, 0, w, h);

    const { prices } = data;
    const pLo = prices[0];
    const pHi = prices[prices.length - 1];
    const plotH = h - TOP - BOT;
    const yOf = (p: number) => TOP + ((pHi - p) / (pHi - pLo)) * plotH;

    const innerW = w - AXIS_W;
    const panelX: [number, number][] = [];
    let acc = AXIS_W;
    for (const frac of WIDTHS) {
      const pw = innerW * frac;
      panelX.push([acc + 6, acc + pw - 6]);
      acc += pw;
    }

    // ── Eje de precio + gridlines horizontales compartidas ──
    ctx.font = "10px ui-monospace, monospace";
    ctx.textAlign = "right";
    const nTicks = 8;
    for (let i = 0; i <= nTicks; i++) {
      const p = pLo + ((pHi - pLo) * i) / nTicks;
      const y = yOf(p);
      ctx.strokeStyle = c.grid;
      ctx.beginPath();
      ctx.moveTo(AXIS_W, y);
      ctx.lineTo(w, y);
      ctx.stroke();
      ctx.fillStyle = c.text;
      ctx.fillText(p >= 1000 ? fmtCompact(p) : p.toFixed(2), AXIS_W - 6, y + 3);
    }

    // ── Series por panel ──
    type Series = { vals: number[]; color: string; width: number; dash: number[]; fill?: string };
    const seriesFor = (key: string): { series: Series[]; lo: number; hi: number; label: string } => {
      if (key === "base") {
        const s: Series[] = [
          { vals: data.actBaseVal, color: CB_AMBER, width: 1, dash: [], fill: "rgba(239,159,39,0.4)" },
        ];
        if (data.adjBaseVal) s.push({ vals: data.adjBaseVal, color: CB_BLUE, width: 1.5, dash: [4, 3], fill: "rgba(55,138,221,0.18)" });
        const mx = Math.max(...data.actBaseVal, ...(data.adjBaseVal ?? [0]), 1e-9);
        return { series: s, lo: 0, hi: mx * 1.05, label: `base (${quote})` };
      }
      if (key === "pct") {
        const s: Series[] = [{ vals: data.actPct, color: GRAY, width: 1.2, dash: [] }];
        if (data.adjPct) s.push({ vals: data.adjPct, color: CB_BLUE, width: 2, dash: [4, 3] });
        return { series: s, lo: 0, hi: 100, label: "% base" };
      }
      const s: Series[] = [{ vals: data.actPnl, color: GRAY, width: 1.2, dash: [] }];
      if (data.adjPnl) s.push({ vals: data.adjPnl, color: CB_BLUE, width: 2, dash: [4, 3] });
      const all = [...data.actPnl, ...(data.adjPnl ?? [])];
      const mn = Math.min(...all, 0);
      return { series: s, lo: mn * 1.06 - 1e-9, hi: Math.max(...all, 0) * 1.06 + 1e-9, label: `PnL vs HOLD (${quote})` };
    };

    PANELS.forEach((panel, pi) => {
      const [x0, x1] = panelX[pi];
      const { series, lo, hi, label } = seriesFor(panel.key);
      const xOf = (v: number) => x0 + ((v - lo) / Math.max(hi - lo, 1e-9)) * (x1 - x0);

      // título + label del eje
      ctx.textAlign = "center";
      ctx.fillStyle = c.text;
      ctx.font = "11px sans-serif";
      ctx.fillText(panel.title, (x0 + x1) / 2, 13);
      ctx.font = "9px sans-serif";
      ctx.fillText(label, (x0 + x1) / 2, h - 6);

      // separador entre paneles
      if (pi > 0) {
        ctx.strokeStyle = c.grid;
        ctx.beginPath();
        ctx.moveTo(x0 - 6, TOP);
        ctx.lineTo(x0 - 6, h - BOT);
        ctx.stroke();
      }

      // línea de cero (para PnL) o de borde
      if (panel.key === "pnl" && lo < 0 && hi > 0) {
        ctx.strokeStyle = c.text;
        ctx.globalAlpha = 0.4;
        ctx.beginPath();
        ctx.moveTo(xOf(0), TOP);
        ctx.lineTo(xOf(0), h - BOT);
        ctx.stroke();
        ctx.globalAlpha = 1;
      }

      // relleno rosa entre actual y ajuste en el panel PnL: el COSTO del cambio
      if (panel.key === "pnl" && data.adjPnl) {
        ctx.fillStyle = "rgba(212,83,126,0.16)";
        ctx.beginPath();
        prices.forEach((p, i) => {
          const x = xOf(data.actPnl[i]);
          const y = yOf(p);
          if (i === 0) ctx.moveTo(x, y);
          else ctx.lineTo(x, y);
        });
        for (let i = prices.length - 1; i >= 0; i--) {
          ctx.lineTo(xOf(data.adjPnl[i]), yOf(prices[i]));
        }
        ctx.closePath();
        ctx.fill();
      }

      for (const s of series) {
        if (s.fill) {
          ctx.fillStyle = s.fill;
          ctx.beginPath();
          ctx.moveTo(xOf(Math.max(lo, 0)), yOf(prices[0]));
          prices.forEach((p, i) => ctx.lineTo(xOf(s.vals[i]), yOf(p)));
          ctx.lineTo(xOf(Math.max(lo, 0)), yOf(prices[prices.length - 1]));
          ctx.closePath();
          ctx.fill();
        }
        ctx.strokeStyle = s.color;
        ctx.lineWidth = s.width;
        ctx.setLineDash(s.dash);
        ctx.beginPath();
        prices.forEach((p, i) => {
          const x = xOf(s.vals[i]);
          const y = yOf(p);
          if (i === 0) ctx.moveTo(x, y);
          else ctx.lineTo(x, y);
        });
        ctx.stroke();
        ctx.setLineDash([]);
        ctx.lineWidth = 1;
      }
    });

    // ── Precio actual: línea blanca punteada cruzando los 3 paneles ──
    const yP = yOf(currentPrice);
    ctx.strokeStyle = "#ffffff";
    ctx.setLineDash([5, 4]);
    ctx.beginPath();
    ctx.moveTo(AXIS_W, yP);
    ctx.lineTo(w, yP);
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = "#ffffff";
    ctx.font = "10px ui-monospace, monospace";
    ctx.textAlign = "left";
    ctx.fillText(
      currentPrice >= 1000 ? fmtCompact(currentPrice) : currentPrice.toFixed(2),
      AXIS_W + 4,
      yP - 4,
    );

    // ── Crosshair con lectura de las series ──
    if (hoverY !== null && hoverY >= TOP && hoverY <= h - BOT) {
      const p = pHi - ((hoverY - TOP) / plotH) * (pHi - pLo);
      let idx = 0;
      for (let i = 1; i < prices.length; i++) {
        if (Math.abs(prices[i] - p) < Math.abs(prices[idx] - p)) idx = i;
      }
      ctx.strokeStyle = c.text;
      ctx.globalAlpha = 0.5;
      ctx.setLineDash([2, 3]);
      ctx.beginPath();
      ctx.moveTo(AXIS_W, hoverY);
      ctx.lineTo(w, hoverY);
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.globalAlpha = 1;

      const fmtP = (v: number) => (v >= 1000 ? fmtCompact(v) : v.toFixed(3));
      const parts = [
        `P ${fmtP(prices[idx])}`,
        `base ${fmtCompact(data.actBaseVal[idx])}${data.adjBaseVal ? `→${fmtCompact(data.adjBaseVal[idx])}` : ""}`,
        `%b ${data.actPct[idx].toFixed(0)}${data.adjPct ? `→${data.adjPct[idx].toFixed(0)}` : ""}%`,
        `pnl ${fmtCompact(data.actPnl[idx])}${data.adjPnl ? `→${fmtCompact(data.adjPnl[idx])}` : ""} ${quote}`,
      ];
      ctx.font = "10px ui-monospace, monospace";
      const txt = parts.join("  ·  ");
      const tw = ctx.measureText(txt).width + 12;
      const tx = Math.min(w - tw - 4, AXIS_W + 8);
      const ty = hoverY > h / 2 ? hoverY - 24 : hoverY + 10;
      ctx.fillStyle = "rgba(13,17,23,0.92)";
      ctx.fillRect(tx, ty, tw, 16);
      ctx.strokeStyle = c.grid;
      ctx.strokeRect(tx, ty, tw, 16);
      ctx.fillStyle = "#e2e8f0";
      ctx.textAlign = "left";
      ctx.fillText(txt, tx + 6, ty + 11);
    }
  }, [data, currentPrice, quote, height, hoverY, width]);

  if (!data) {
    return (
      <div className="flex h-40 items-center justify-center rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] text-xs text-[var(--color-text-muted)]">
        Sin grillas para agregar
      </div>
    );
  }

  return (
    <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] overflow-hidden">
      <div className="flex items-center justify-between border-b border-[var(--color-border)] bg-[var(--color-bg)] px-3 py-1.5">
        <p className="text-[10px] text-[var(--color-text-muted)]">
          Agregador de operaciones · curvas de inventario y costo de convexidad
        </p>
        <p className="text-[10px] text-[var(--color-text-muted)]">
          <span style={{ color: CB_AMBER }}>■</span> activos ·{" "}
          <span style={{ color: GRAY }}>—</span> actual ·{" "}
          <span style={{ color: CB_BLUE }}>┄</span> con ajuste ·{" "}
          <span style={{ color: CB_PINK }}>■</span> costo del cambio
        </p>
      </div>
      <div ref={wrapRef} style={{ position: "relative" }}>
        <canvas
          ref={canvasRef}
          onMouseMove={(e) => {
            const r = e.currentTarget.getBoundingClientRect();
            setHoverY(e.clientY - r.top);
          }}
          onMouseLeave={() => setHoverY(null)}
        />
      </div>
    </div>
  );
}
