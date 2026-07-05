import { useEffect, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";

import { AggregatorPanel } from "@/components/chessboard/AggregatorPanel";
import { ChessboardChart, type ChessboardView } from "@/components/chessboard/ChessboardChart";
import { ImpactModal } from "@/components/chessboard/ImpactModal";
import { useCandleStore } from "@/hooks/useCandleStore";
import { useServer } from "@/hooks/useServer";
import { api } from "@/lib/api";
import { CB_AMBER, CB_BLUE, type ChessboardGrid, type ChessboardImpact, fmtCompact } from "@/lib/chessboard";

type PageView = ChessboardView | "aggregator";

const VIEWS: { key: PageView; label: string; hint: string }[] = [
  { key: "market", label: "Mercado + inventario", hint: "vol. del mercado por precio + cómo debería fluctuar el inventario" },
  { key: "activity", label: "Actividad por grilla", hint: "volumen generado por la grilla, por precio y en el tiempo" },
  { key: "levels", label: "IL vs spreads", hint: "spread capturado + rebates vs IL del abierto, por nivel" },
  { key: "aggregator", label: "Agregador", hint: "capital en base, % base agregado y PnL vs HOLD del conjunto" },
];

const INTERVALS = ["1m", "5m", "15m", "1h", "4h"];

function sideStateBadge(state?: string) {
  if (!state) return null;
  const ok = state === "ACTIVE";
  return (
    <span
      className={`rounded px-1 py-0.5 text-[9px] font-semibold ${
        ok ? "bg-emerald-500/15 text-emerald-400" : "bg-amber-500/15 text-amber-500"
      }`}
    >
      {state}
    </span>
  );
}

export function Chessboard() {
  const { server } = useServer();
  const [mode, setMode] = useState<"dashboard" | "lab">("dashboard");
  const [view, setView] = useState<PageView>("market");
  const [interval, setInterval_] = useState("5m");
  const [selectedGridId, setSelectedGridId] = useState<string | null>(null);
  const [selectedPairKey, setSelectedPairKey] = useState<string | null>(null);

  const { data: state, isLoading, isError, error } = useQuery({
    queryKey: ["chessboard-state", server],
    queryFn: () => api.getChessboardState(server!),
    enabled: !!server,
    refetchInterval: 15000,
  });

  const allGrids = useMemo(() => state?.grids ?? [], [state]);

  // Pares disponibles (connector|pair) con su cantidad de grillas.
  const pairs = useMemo(() => {
    const counts = new Map<string, number>();
    for (const g of allGrids) {
      const k = `${g.connector}|${g.trading_pair}`;
      counts.set(k, (counts.get(k) ?? 0) + 1);
    }
    return [...counts.entries()].sort((a, b) => b[1] - a[1]);
  }, [allGrids]);

  // Par graficado: el elegido, o el dominante como default.
  const { pair, connector, grids } = useMemo(() => {
    if (!pairs.length) return { pair: "", connector: "", grids: [] as ChessboardGrid[] };
    const key = selectedPairKey && pairs.some(([k]) => k === selectedPairKey)
      ? selectedPairKey
      : pairs[0][0];
    const [conn, p] = key.split("|");
    return {
      pair: p,
      connector: conn,
      grids: allGrids.filter((g) => g.connector === conn && g.trading_pair === p),
    };
  }, [pairs, selectedPairKey, allGrids]);

  const selectedGrid = useMemo(
    () => grids.find((g) => g.id === selectedGridId) ?? grids[0] ?? null,
    [grids, selectedGridId],
  );

  // Elegir una grilla desde el strip: cambia también el par graficado.
  const pickGrid = (g: ChessboardGrid) => {
    setSelectedPairKey(`${g.connector}|${g.trading_pair}`);
    setSelectedGridId(g.id);
  };

  // ── Live ops: draft de ajuste + cuadro de impacto ──
  const [draft, setDraft] = useState<{ low: number; high: number } | null>(null);
  const [rebalanceWanted, setRebalanceWanted] = useState(true);
  const [impact, setImpact] = useState<ChessboardImpact | null>(null);
  const [impactBusy, setImpactBusy] = useState(false);
  const [savingAdj, setSavingAdj] = useState(false);
  const [adjMsg, setAdjMsg] = useState<string | null>(null);

  // Cambiar de grilla/par descarta el draft en curso.
  useEffect(() => {
    setDraft(null);
    setImpact(null);
  }, [selectedGridId, selectedPairKey]);

  const evalImpact = async () => {
    if (!server || !selectedGrid || !draft) return;
    setImpactBusy(true);
    setAdjMsg(null);
    try {
      const res = await api.getChessboardImpact(server, selectedGrid.id, {
        draft: { min_price: draft.low, max_price: draft.high },
        rebalance: rebalanceWanted,
      });
      setImpact(res);
    } catch (e) {
      setAdjMsg(`Error evaluando impacto: ${e instanceof Error ? e.message : e}`);
    } finally {
      setImpactBusy(false);
    }
  };

  const confirmAdjustment = async (note: string) => {
    if (!server || !selectedGrid || !impact || !draft) return;
    setSavingAdj(true);
    try {
      await api.postChessboardAdjustment(server, selectedGrid.id, {
        impact,
        draft: { min_price: draft.low, max_price: draft.high, rebalance: rebalanceWanted },
        accepted: true,
        contradicted: impact.verdict === "red",
        note: note || undefined,
      });
      setAdjMsg(
        "✓ Decisión registrada en el journal. El apply automático llega con el controller Fase B — por ahora, redeployá desde la lab con el rango nuevo.",
      );
      setImpact(null);
      setDraft(null);
    } catch (e) {
      setAdjMsg(`Error registrando el ajuste: ${e instanceof Error ? e.message : e}`);
    } finally {
      setSavingAdj(false);
    }
  };

  // Velas: store en vivo (WS con backfill) + un fetch REST inicial de profundidad.
  const { candles, mergeCandles } = useCandleStore(server, connector, pair, interval);
  const { data: restCandles } = useQuery({
    queryKey: ["chessboard-candles", server, connector, pair, interval],
    queryFn: () => api.getCandles(server!, connector, pair, interval, 1500),
    enabled: !!server && !!connector && !!pair,
    staleTime: 60_000,
  });
  useEffect(() => {
    if (restCandles?.length) mergeCandles(restCandles);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [restCandles]);

  const currentPrice =
    (pair && state?.prices?.[pair]) ||
    (candles.length ? candles[candles.length - 1].close : 0);

  const alerts = useMemo(
    () => (state?.grids ?? []).flatMap((g) => g.alerts),
    [state],
  );

  const totals = useMemo(() => {
    const gs = state?.grids ?? [];
    return {
      bots: new Set(gs.map((g) => g.bot_name)).size,
      grids: gs.length,
      liq: gs.reduce((a, g) => a + g.liquidity, 0),
      vol: gs.reduce((a, g) => a + g.volume, 0),
      pnl: gs.reduce((a, g) => a + g.pnl, 0),
    };
  }, [state]);

  const quote = pair.includes("-") ? pair.split("-")[1] : "";

  if (!server) {
    return <p className="text-sm text-[var(--color-text-muted)]">Elegí un server para ver el tablero.</p>;
  }

  return (
    <div className="flex flex-col gap-4">
      {/* Header: título + toggle de modo */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-lg font-bold">Chessboard</h1>
          <p className="text-xs text-[var(--color-text-muted)]">
            {mode === "dashboard"
              ? "Monitoreo en vivo de las grillas chessboard_lite"
              : "Lab: dimensionamiento interactivo (próximamente)"}
          </p>
        </div>
        <div className="flex rounded-md border border-[var(--color-border)] overflow-hidden">
          {(["dashboard", "lab"] as const).map((m) => (
            <button
              key={m}
              onClick={() => setMode(m)}
              className={`px-4 py-1.5 text-xs font-medium transition-colors ${
                mode === m
                  ? "bg-[var(--color-primary)]/20 text-[var(--color-primary)]"
                  : "text-[var(--color-text-muted)] hover:bg-[var(--color-surface-hover)]"
              }`}
            >
              {m === "dashboard" ? "Dashboard" : "Lab"}
            </button>
          ))}
        </div>
      </div>

      {mode === "lab" ? (
        <div className="rounded-lg border border-dashed border-[var(--color-border)] p-10 text-center text-sm text-[var(--color-text-muted)]">
          El modo Lab (sliders de rango/niveles/TP con recálculo instantáneo del edge) viene en la Fase 2.
          Por ahora usá la routine <code>chessboard_lite_lab</code> desde Routines.
        </div>
      ) : isLoading ? (
        <p className="text-sm text-[var(--color-text-muted)]">Cargando estado de las grillas…</p>
      ) : isError ? (
        <p className="text-sm text-red-400">Error: {(error as Error)?.message}</p>
      ) : !state?.grids?.length ? (
        <div className="rounded-lg border border-[var(--color-border)] p-8 text-center text-sm text-[var(--color-text-muted)]">
          No hay controllers <code>chessboard_lite</code> activos en <b>{server}</b>.
        </div>
      ) : (
        <>
          {/* Alertas de telemetría */}
          {alerts.length > 0 && (
            <div className="rounded-lg border border-amber-500/40 bg-amber-500/10 px-4 py-2 text-xs">
              {alerts.map((a, i) => (
                <p key={i} className="py-0.5">{a}</p>
              ))}
            </div>
          )}

          {/* KPIs */}
          <div className="grid grid-cols-3 gap-3 sm:grid-cols-6">
            {[
              ["Bots", String(totals.bots)],
              ["Grillas", String(totals.grids)],
              [`Liquidez ${quote}`, fmtCompact(totals.liq)],
              [`Volumen ${quote}`, fmtCompact(totals.vol)],
              [`PnL ${quote}`, `${totals.pnl >= 0 ? "+" : ""}${totals.pnl.toFixed(2)}`],
              ["Precio", currentPrice ? currentPrice.toLocaleString() : "—"],
            ].map(([label, value]) => (
              <div key={label} className="rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] px-3 py-2">
                <p className="text-[10px] uppercase text-[var(--color-text-muted)]">{label}</p>
                <p className={`font-mono text-sm font-semibold ${
                  label.startsWith("PnL") ? (totals.pnl >= 0 ? "text-emerald-400" : "text-red-400") : ""
                }`}>{value}</p>
              </div>
            ))}
          </div>

          {/* Grillas activas: TODAS (clickear cambia el par graficado y selecciona) */}
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {allGrids.map((g) => {
              const t = g.telemetry;
              const gPrice = state?.prices?.[g.trading_pair] ?? 0;
              const inRange = gPrice >= g.low && gPrice <= g.high;
              const taker =
                (t?.long?.taker_volume_quote ?? 0) + (t?.short?.taker_volume_quote ?? 0);
              return (
                <button
                  key={g.id}
                  onClick={() => pickGrid(g)}
                  className={`rounded-lg border px-3 py-2 text-left transition-colors ${
                    selectedGrid?.id === g.id
                      ? "border-[var(--color-primary)]/60 bg-[var(--color-primary)]/5"
                      : "border-[var(--color-border)] bg-[var(--color-surface)] hover:bg-[var(--color-surface-hover)]"
                  }`}
                >
                  <div className="flex items-center justify-between">
                    <span className="font-mono text-xs font-semibold">{g.id}</span>
                    <span className={`text-[10px] ${inRange ? "text-emerald-400" : "text-amber-500"}`}>
                      {gPrice ? (inRange ? "en rango" : "fuera de rango") : "—"}
                    </span>
                  </div>
                  <p className="mt-1 font-mono text-[11px] text-[var(--color-text-muted)]">
                    {g.trading_pair} · {g.low.toLocaleString()}–{g.high.toLocaleString()} · {g.n_levels} niv · liq {fmtCompact(g.liquidity)}
                  </p>
                  <div className="mt-1.5 flex flex-wrap items-center gap-1.5 text-[10px]">
                    <span style={{ color: CB_BLUE }}>L</span>
                    {sideStateBadge(t?.long?.state) ?? <span className="text-[var(--color-text-muted)]">—</span>}
                    <span style={{ color: CB_AMBER }}>S</span>
                    {sideStateBadge(t?.short?.state) ?? <span className="text-[var(--color-text-muted)]">—</span>}
                    {t?.inv_pct != null && (
                      <span className="text-[var(--color-text-muted)]">inv {t.inv_pct.toFixed(0)}%</span>
                    )}
                    {taker > 0 && (
                      <span className="rounded bg-red-500/20 px-1 font-semibold text-red-400">
                        🚨 taker {fmtCompact(taker)}
                      </span>
                    )}
                    <span className={`ml-auto font-mono ${g.pnl >= 0 ? "text-emerald-400" : "text-red-400"}`}>
                      {g.pnl >= 0 ? "+" : ""}{g.pnl.toFixed(2)}
                    </span>
                  </div>
                </button>
              );
            })}
          </div>

          {/* Controles: par + vista + grilla + intervalo */}
          <div className="flex flex-wrap items-center gap-3">
            {pairs.length > 1 && (
              <div className="flex rounded-md border border-[var(--color-border)] overflow-hidden">
                {pairs.map(([key, count]) => {
                  const [, p] = key.split("|");
                  const active = key === `${connector}|${pair}`;
                  return (
                    <button
                      key={key}
                      onClick={() => { setSelectedPairKey(key); setSelectedGridId(null); }}
                      className={`px-3 py-1.5 text-xs transition-colors ${
                        active
                          ? "bg-[var(--color-primary)]/20 text-[var(--color-primary)] font-medium"
                          : "text-[var(--color-text-muted)] hover:bg-[var(--color-surface-hover)]"
                      }`}
                    >
                      {p} <span className="opacity-60">×{count}</span>
                    </button>
                  );
                })}
              </div>
            )}

            <div className="flex rounded-md border border-[var(--color-border)] overflow-hidden">
              {VIEWS.map((v) => (
                <button
                  key={v.key}
                  onClick={() => setView(v.key)}
                  title={v.hint}
                  className={`px-3 py-1.5 text-xs transition-colors ${
                    view === v.key
                      ? "bg-[var(--color-primary)]/20 text-[var(--color-primary)] font-medium"
                      : "text-[var(--color-text-muted)] hover:bg-[var(--color-surface-hover)]"
                  }`}
                >
                  {v.label}
                </button>
              ))}
            </div>

            {(view === "activity" || view === "levels") && grids.length > 1 && (
              <select
                value={selectedGrid?.id ?? ""}
                onChange={(e) => setSelectedGridId(e.target.value)}
                className="rounded-md border border-[var(--color-border)] bg-[var(--color-surface)] px-2 py-1.5 text-xs"
              >
                {grids.map((g) => (
                  <option key={g.id} value={g.id}>{g.id}</option>
                ))}
              </select>
            )}

            <div className="flex rounded-md border border-[var(--color-border)] overflow-hidden">
              {INTERVALS.map((iv) => (
                <button
                  key={iv}
                  onClick={() => setInterval_(iv)}
                  className={`px-2.5 py-1.5 text-xs transition-colors ${
                    interval === iv
                      ? "bg-[var(--color-primary)]/20 text-[var(--color-primary)] font-medium"
                      : "text-[var(--color-text-muted)] hover:bg-[var(--color-surface-hover)]"
                  }`}
                >
                  {iv}
                </button>
              ))}
            </div>

            {selectedGrid && !draft && (
              <button
                onClick={() => setDraft({ low: selectedGrid.low, high: selectedGrid.high })}
                className="rounded-md border border-[var(--color-primary)]/50 px-3 py-1.5 text-xs text-[var(--color-primary)] hover:bg-[var(--color-primary)]/10"
              >
                ✎ Ajustar grilla
              </button>
            )}

            <span className="ml-auto text-[10px] text-[var(--color-text-muted)]">
              azul = LONG · ámbar = SHORT · rosa = rebates · sólido = cobrado · rayado = abierto
            </span>
          </div>

          {/* Toolbar de edición: draft activo */}
          {draft && selectedGrid && (
            <div className="flex flex-wrap items-center gap-3 rounded-lg border border-[var(--color-primary)]/40 bg-[var(--color-primary)]/5 px-3 py-2">
              <span className="text-xs font-semibold text-[var(--color-primary)]">
                Ajustando <span className="font-mono">{selectedGrid.id}</span>
              </span>
              <label className="flex items-center gap-1.5 text-[11px]">
                min
                <input
                  type="number"
                  value={draft.low}
                  step={draft.low >= 1000 ? 100 : 0.01}
                  onChange={(e) => setDraft({ ...draft, low: Number(e.target.value) })}
                  className="w-28 rounded border border-[var(--color-border)] bg-[var(--color-bg)] px-1.5 py-1 font-mono text-[11px]"
                />
              </label>
              <label className="flex items-center gap-1.5 text-[11px]">
                max
                <input
                  type="number"
                  value={draft.high}
                  step={draft.high >= 1000 ? 100 : 0.01}
                  onChange={(e) => setDraft({ ...draft, high: Number(e.target.value) })}
                  className="w-28 rounded border border-[var(--color-border)] bg-[var(--color-bg)] px-1.5 py-1 font-mono text-[11px]"
                />
              </label>
              <span className="text-[10px] text-[var(--color-text-muted)]">
                o arrastrá los bordes / la banda en el chart
              </span>
              <label className="flex items-center gap-1.5 text-[11px]">
                <input
                  type="checkbox"
                  checked={rebalanceWanted}
                  onChange={(e) => setRebalanceWanted(e.target.checked)}
                />
                incluir rebalanceo
              </label>
              <div className="ml-auto flex gap-2">
                <button
                  onClick={() => { setDraft(null); setAdjMsg(null); }}
                  className="rounded-md border border-[var(--color-border)] px-3 py-1 text-xs hover:bg-[var(--color-surface-hover)]"
                >
                  Descartar
                </button>
                <button
                  onClick={evalImpact}
                  disabled={impactBusy || draft.high <= draft.low}
                  className="rounded-md bg-[var(--color-primary)] px-3 py-1 text-xs font-semibold text-white disabled:opacity-40"
                >
                  {impactBusy ? "Evaluando…" : "Evaluar impacto"}
                </button>
              </div>
            </div>
          )}

          {adjMsg && (
            <p className={`rounded-md border px-3 py-2 text-xs ${
              adjMsg.startsWith("✓")
                ? "border-emerald-500/40 bg-emerald-500/10 text-emerald-300"
                : "border-red-500/40 bg-red-500/10 text-red-300"
            }`}>
              {adjMsg}
            </p>
          )}

          {/* El tablero */}
          {view === "aggregator" ? (
            <AggregatorPanel
              grids={grids}
              draft={
                draft && selectedGrid
                  ? { replaceId: selectedGrid.id, low: draft.low, high: draft.high, liquidity: selectedGrid.liquidity }
                  : null
              }
              currentPrice={currentPrice}
              quote={pair.split("-")[1] ?? "quote"}
            />
          ) : (
            <ChessboardChart
              candles={candles}
              grids={grids}
              selectedGrid={selectedGrid}
              currentPrice={currentPrice}
              view={view}
              interval={interval}
              tradingPair={pair}
              draft={draft}
              onDraftChange={setDraft}
            />
          )}

          {impact && (
            <ImpactModal
              impact={impact}
              quote={pair.split("-")[1] ?? "quote"}
              busy={savingAdj}
              onCancel={() => setImpact(null)}
              onConfirm={confirmAdjustment}
            />
          )}

        </>
      )}
    </div>
  );
}
