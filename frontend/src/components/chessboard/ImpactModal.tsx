import { useState } from "react";

import {
  fmtCompact,
  type ChessboardImpact,
  type ImpactItem,
  type ImpactTag,
} from "@/lib/chessboard";

// El cuadro de confirmación es un CONTRATO: diff completo (directos +
// derivados + rebalanceo), cada ítem con su tag de riesgo salido de una regla
// nombrada. 🔴 exige doble confirmación y la contradicción queda journalizada.

const TAG_DOT: Record<ImpactTag, string> = { green: "🟢", yellow: "🟡", red: "🔴" };
const TAG_CLS: Record<ImpactTag, string> = {
  green: "text-emerald-400",
  yellow: "text-amber-400",
  red: "text-red-400",
};

function fmtVal(v: number | string | null): string {
  if (v == null) return "—";
  if (typeof v === "string") return v;
  if (Number.isInteger(v) && Math.abs(v) < 1000) return String(v);
  if (Math.abs(v) >= 10000) return v.toLocaleString(undefined, { maximumFractionDigits: 2 });
  return v.toLocaleString(undefined, { maximumFractionDigits: 4 });
}

function ItemRow({ it }: { it: ImpactItem }) {
  return (
    <tr className={it.changed ? "" : "opacity-45"}>
      <td className="py-0.5 pr-3 font-mono text-[11px]">{it.name}</td>
      <td className="py-0.5 pr-2 text-right font-mono text-[11px]">{fmtVal(it.current)}</td>
      <td className="py-0.5 pr-2 text-center text-[10px] text-[var(--color-text-muted)]">→</td>
      <td className="py-0.5 pr-3 text-right font-mono text-[11px] font-semibold">
        {fmtVal(it.proposed)}
      </td>
      <td className="py-0.5 text-[10px]">
        {it.tag ? (
          <span className={TAG_CLS[it.tag]} title={it.rule ?? undefined}>
            {TAG_DOT[it.tag]} {it.rule ? `${it.rule}: ` : ""}{it.reason}
          </span>
        ) : (
          <span className="text-[var(--color-text-muted)]">—</span>
        )}
      </td>
    </tr>
  );
}

interface ImpactModalProps {
  impact: ChessboardImpact;
  quote: string;
  onConfirm: (note: string) => void;
  onCancel: () => void;
  busy?: boolean;
}

export function ImpactModal({ impact, quote, onConfirm, onCancel, busy }: ImpactModalProps) {
  const [ack, setAck] = useState(false);
  const [note, setNote] = useState("");
  const isRed = impact.verdict === "red";
  const reb = impact.rebalance;
  const orp = impact.orphan;

  return (
    <div className="fixed inset-0 z-[60] flex items-center justify-center bg-black/60 p-4">
      <div className="max-h-[90vh] w-full max-w-3xl overflow-auto rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] shadow-2xl">
        {/* Header con veredicto */}
        <div className="flex items-center justify-between border-b border-[var(--color-border)] px-4 py-3">
          <div>
            <h3 className="text-sm font-semibold">
              Ajustar <span className="font-mono">{impact.grid_id}</span>
            </h3>
            <p className="text-[10px] text-[var(--color-text-muted)]">
              reglas {impact.rules_version} · el peor tag manda
            </p>
          </div>
          <span className={`rounded-md px-3 py-1 text-sm font-bold ${TAG_CLS[impact.verdict]} ${
            isRed ? "bg-red-500/15" : impact.verdict === "yellow" ? "bg-amber-500/10" : "bg-emerald-500/10"
          }`}>
            {TAG_DOT[impact.verdict]} {impact.verdict.toUpperCase()}
          </span>
        </div>

        <div className="space-y-4 px-4 py-3">
          {/* Bloque 1: parámetros directos */}
          <section>
            <h4 className="mb-1 text-[11px] font-semibold uppercase text-[var(--color-text-muted)]">
              Parámetros
            </h4>
            <table className="w-full">
              <tbody>
                {impact.params.map((it) => <ItemRow key={it.name} it={it} />)}
              </tbody>
            </table>
          </section>

          {/* Bloque 2: derivados */}
          <section>
            <h4 className="mb-1 text-[11px] font-semibold uppercase text-[var(--color-text-muted)]">
              Derivados (lo que tus cambios provocan)
            </h4>
            <table className="w-full">
              <tbody>
                {impact.derived.map((it) => <ItemRow key={it.name} it={it} />)}
              </tbody>
            </table>
          </section>

          {/* Bloque 3: abierto huérfano + rebalanceo */}
          <section className="grid gap-3 sm:grid-cols-2">
            <div className="rounded-md border border-[var(--color-border)] p-2.5">
              <h4 className="text-[11px] font-semibold uppercase text-[var(--color-text-muted)]">
                Abierto huérfano al frenar
              </h4>
              <p className="mt-1 font-mono text-xs">
                {fmtCompact(orp.open_quote)} {quote}
                {orp.be_buy != null && ` · BE buy ${fmtVal(orp.be_buy)}`}
                {orp.be_sell != null && ` · BE sell ${fmtVal(orp.be_sell)}`}
              </p>
              {orp.tag && (
                <p className={`mt-1 text-[10px] ${TAG_CLS[orp.tag]}`}>
                  {TAG_DOT[orp.tag]} {orp.rule}: {orp.reason}
                </p>
              )}
            </div>
            <div className="rounded-md border border-[var(--color-border)] p-2.5">
              <h4 className="text-[11px] font-semibold uppercase text-[var(--color-text-muted)]">
                Orden de rebalanceo
              </h4>
              {reb ? (
                <>
                  <p className="mt-1 font-mono text-xs font-semibold">
                    {reb.side} {reb.base_amount.toFixed(6)} ≈ {fmtCompact(reb.quote_value)} {quote}
                    <span className="ml-1 font-normal text-[var(--color-text-muted)]">
                      (inv {reb.from_inv_pct.toFixed(0)}% → {reb.to_inv_pct.toFixed(0)}%)
                    </span>
                  </p>
                  <p className="mt-1 text-[10px] text-[var(--color-text-muted)]">
                    IL a realizar {reb.il_realized_quote.toFixed(2)} · rebate est. +{reb.est_fee_quote.toFixed(2)}
                    {reb.payback_days != null && ` · payback ~${reb.payback_days.toFixed(0)} días`}
                  </p>
                  {reb.tag && (
                    <p className={`mt-1 text-[10px] ${TAG_CLS[reb.tag]}`}>
                      {TAG_DOT[reb.tag]} {reb.rule}: {reb.reason}
                    </p>
                  )}
                </>
              ) : (
                <p className="mt-1 text-xs text-[var(--color-text-muted)]">no hace falta / no pedida</p>
              )}
            </div>
          </section>

          {/* Economía comparada, la síntesis */}
          <section className="rounded-md border border-[var(--color-border)] p-2.5 text-[11px]">
            <span className="text-[var(--color-text-muted)]">edge/día est.: </span>
            <span className="font-mono">{(impact.economics.old.pnl_day ?? 0).toFixed(1)}</span>
            <span className="text-[var(--color-text-muted)]"> → </span>
            <span className="font-mono font-semibold">{(impact.economics.new.pnl_day ?? 0).toFixed(1)} {quote}</span>
            <span className="ml-3 text-[var(--color-text-muted)]">time-in-range: </span>
            <span className="font-mono">{(impact.economics.old.time_in_range_pct ?? 0).toFixed(0)}%</span>
            <span className="text-[var(--color-text-muted)]"> → </span>
            <span className="font-mono font-semibold">{(impact.economics.new.time_in_range_pct ?? 0).toFixed(0)}%</span>
          </section>

          {/* Nota + doble confirmación en rojo */}
          <input
            value={note}
            onChange={(e) => setNote(e.target.value)}
            placeholder="nota para el journal (opcional): por qué hacés este ajuste"
            className="w-full rounded-md border border-[var(--color-border)] bg-[var(--color-bg)] px-2 py-1.5 text-xs"
          />
          {isRed && (
            <label className="flex items-center gap-2 rounded-md border border-red-500/40 bg-red-500/10 px-3 py-2 text-xs text-red-300">
              <input type="checkbox" checked={ack} onChange={(e) => setAck(e.target.checked)} />
              Entiendo los riesgos marcados en rojo y decido avanzar igual (queda registrado)
            </label>
          )}
        </div>

        <div className="flex items-center justify-between border-t border-[var(--color-border)] px-4 py-3">
          <p className="text-[10px] text-[var(--color-text-muted)]">
            Fase A: la decisión se journaliza; el apply al bot llega con el controller Fase B
          </p>
          <div className="flex gap-2">
            <button
              onClick={onCancel}
              className="rounded-md border border-[var(--color-border)] px-3 py-1.5 text-xs hover:bg-[var(--color-surface-hover)]"
            >
              Cancelar
            </button>
            <button
              disabled={busy || (isRed && !ack)}
              onClick={() => onConfirm(note)}
              className={`rounded-md px-3 py-1.5 text-xs font-semibold disabled:cursor-not-allowed disabled:opacity-40 ${
                isRed
                  ? "bg-red-500/80 text-white hover:bg-red-500"
                  : "bg-[var(--color-primary)] text-white hover:opacity-90"
              }`}
            >
              {busy ? "Registrando…" : isRed ? "Confirmar igual" : "Confirmar ajuste"}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
