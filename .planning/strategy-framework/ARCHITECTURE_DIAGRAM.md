# Architecture Diagram — Adaptive Strategy Framework

> Vista conceptual única, comunicable. Variables clave inline.
> Si querés más detalle de cualquier capa: ver DESIGN.md / DECISIONS.md / CAPITAL_DYNAMICS.md.

## Diagrama principal — flujo del agente por tick

```mermaid
flowchart TD
    Start([Tick del agente · cada 10 min]) --> ReadRoutines

    subgraph Layer1[" 1. ROUTINES · data sources sin side effects "]
        direction TB
        R1["capital_state<br/>━━━━━━━━━━━━━━<br/>nominal_budget · committed_now<br/>worst_case · utilization<br/>oversub por activo · headroom"]
        R2["market_regime<br/>━━━━━━━━━━━━━━<br/>direccionalidad · volatilidad<br/>microestructura · estabilidad<br/>→ etiqueta + favorabilidad"]
        R3["controller_performance<br/>━━━━━━━━━━━━━━<br/>PnL realizado / no-realizado<br/>volume · fill_rate · market_share<br/>PnL velocity · executor turnover"]
    end

    ReadRoutines --> Layer1
    Layer1 --> Diagnose

    Diagnose{"¿Régimen subóptimo?<br/>(D9)<br/>━━━━━━━━━━━━━━<br/>PnL flat + vol bajo<br/>+ tiempo en régimen adverso<br/>+ market_regime confirma"}

    Diagnose -- No --> NoAction([no_action · loggear])
    Diagnose -- Sí --> LLMDecision

    subgraph Layer2[" 2. AGENTE · capa de decisión "]
        direction TB
        LLMDecision["LLM razona sobre régimen<br/>+ policy del usuario<br/>━━━━━━━━━━━━━━<br/>elige DIMENSIÓN:<br/>· qué campo tocar<br/>· qué dirección<br/>(no valores específicos)"]
        Expander["Expansor determinístico<br/>━━━━━━━━━━━━━━<br/>genera 3 candidatos discretos<br/>ej: take_profit × {1.5, 2, 3}"]
        LLMDecision --> Expander
    end

    Expander --> BacktestLoop

    subgraph Layer3[" 2.5 BACKTEST EVIDENCE LOOP · D9 "]
        direction TB
        BTBaseline["Backtest BASELINE<br/>(config actual, período subóptimo)"]
        BTAlts["Backtest × 3 ALTERNATIVAS<br/>(en serie, ~12-40s c/u)"]
        Score["Score function<br/>━━━━━━━━━━━━━━<br/>vol_norm + 0.3 × pnl_norm<br/>(PnL ≥ 0 obligatorio)"]
        Cache[("BacktestStore<br/>cache hash-based")]
        BTBaseline --> BTAlts --> Score
        Cache -. dedup .- BTBaseline
        Cache -. dedup .- BTAlts
    end

    BacktestLoop --> Score
    Score --> Invariants

    subgraph Layer4[" 3. GUARDRAILS · invariants duros (D3) "]
        direction TB
        Inv1["validate_no_oversub<br/>━━━━━━━━━━━━━━<br/>safety_margin = 0.95<br/>chequea contra capital_state"]
        Inv2["cooldown · max_delta · whitelist<br/>━━━━━━━━━━━━━━<br/>no manual_kill_switch<br/>no leverage · no connector"]
        Inv1 --> Inv2
    end

    Invariants -- rechaza --> NoAction
    Invariants -- aprueba --> Mode

    subgraph Layer5[" 4. DISPATCH · según modo "]
        direction TB
        Mode{Modo activo}
        Propose["modo PROPOSE<br/>(D4 · MVP default)<br/>━━━━━━━━━━━━━━<br/>Telegram con botones<br/>✅ Aplicar / ❌ Rechazar / ⏸ Snooze<br/>incluye comparativo de backtests"]
        Shadow["modo SHADOW<br/>━━━━━━━━━━━━━━<br/>solo loggea<br/>cero side effects"]
        Auto["modo AUTO<br/>━━━━━━━━━━━━━━<br/>aplica directo<br/>(solo después de validación)"]
        Mode --> Propose
        Mode --> Shadow
        Mode --> Auto
    end

    Mode --> Apply

    subgraph Layer6[" 5. APPLY · MCP tool "]
        Apply["update_controller_config<br/>━━━━━━━━━━━━━━<br/>setattr in-place sobre<br/>campos is_updatable<br/>(D1 · 25 campos)"]
    end

    Apply --> Audit
    Audit["Audit log inmutable (D8)<br/>━━━━━━━━━━━━━━<br/>config antes/después<br/>+ evidencia (backtests)<br/>+ razón LLM<br/>+ veredicto humano"]
    Audit --> End([Próximo tick])
    NoAction --> End

    classDef routine fill:#e1f5ff,stroke:#0288d1,color:#000
    classDef agent fill:#fff3e0,stroke:#f57c00,color:#000
    classDef backtest fill:#f3e5f5,stroke:#7b1fa2,color:#000
    classDef guard fill:#ffebee,stroke:#c62828,color:#000
    classDef dispatch fill:#e8f5e9,stroke:#388e3c,color:#000
    classDef apply fill:#fffde7,stroke:#f9a825,color:#000

    class R1,R2,R3 routine
    class LLMDecision,Expander agent
    class BTBaseline,BTAlts,Score,Cache backtest
    class Inv1,Inv2 guard
    class Propose,Shadow,Auto dispatch
    class Apply apply
```

---

## Diagrama secundario — modelo de capital (3 planos)

```mermaid
flowchart LR
    subgraph Wallet[" WALLET REAL · compartida "]
        BTC[("BTC<br/>0.123 · $8.2k")]
        USDT[("USDT<br/>$5.4k")]
        BRL[("BRL<br/>R$12k · $2.4k")]
    end

    subgraph Plane1[" Plano 1 · controller-local "]
        direction TB
        C1["C1 · BTC-USDT<br/>━━━━━━━━━━━━━━<br/>nominal $30 · worst $300<br/>util_now 87%<br/>p50 31% / p95 72%"]
        C2["C2 · BTC-BRL<br/>━━━━━━━━━━━━━━<br/>nominal $50 · worst $200<br/>util_now 18%<br/>p50 12% / p95 45%"]
    end

    subgraph Plane2[" Plano 2 · inter-controller "]
        direction TB
        Demand["demanda potencial por activo<br/>━━━━━━━━━━━━━━<br/>BTC: 11.5k · USDT: 4.2k · BRL: 0<br/>oversub_ratio: BTC 1.4× ⚠️"]
    end

    subgraph Plane3[" Plano 3 · cross-pair (efectos colaterales) "]
        direction TB
        Cross["C1 vende BTC → menos BTC para C2<br/>C2 compra BTC → más BTC para C1<br/>━━━━━━━━━━━━━━<br/>current_base_pct local puede mentir"]
    end

    BTC -. nominal .-> C1
    BTC -. nominal .-> C2
    USDT -. nominal .-> C1
    BRL -. nominal .-> C2

    C1 --> Demand
    C2 --> Demand

    C1 -.->|trades reales| Cross
    C2 -.->|trades reales| Cross
    Cross -.->|drift| Wallet

    classDef wallet fill:#e3f2fd,stroke:#1565c0,color:#000
    classDef ctrl fill:#fff3e0,stroke:#ef6c00,color:#000
    classDef plane2 fill:#fce4ec,stroke:#c2185b,color:#000
    classDef plane3 fill:#f3e5f5,stroke:#6a1b9a,color:#000

    class BTC,USDT,BRL wallet
    class C1,C2 ctrl
    class Demand plane2
    class Cross plane3
```

---

## Variables clave del framework — referencia rápida

| Variable | Decisión | Origen |
|---|---|---|
| Tick del agente | **10 min** | D9 |
| Modos de operación | propose (default MVP) / shadow / auto | D4 |
| Hot-reload | sí, nativo via Pydantic `is_updatable` | D1 |
| Campos NO updatables | connector, trading_pair, position_mode, leverage*, kill_switch* | D7 + Hummingbot |
| Safety margin (oversub) | 0.95 | D9 |
| Cooldown entre cambios | configurable, default 30 min | D4 |
| Histórico de capital | JSONL, 1 archivo por controller, retain 30d | CAPITAL_DYNAMICS |
| Lookback percentiles | 24h | CAPITAL_DYNAMICS |
| Connectors no soportados (backtest) | hyperliquid, dydx, kraken, coinbase_advanced | D9 |
| Score backtest | `vol_norm + 0.3 × pnl_norm` (PnL≥0) | D9 |
| Expansor de propuestas | LLM elige dimensión + dirección, código genera 3 valores | D9 |

\* leverage es technically `is_updatable` pero por política humano-only.

---

## Cómo leer este diagrama

- **Cajas con borde azul** = Routines (data, sin acción).
- **Naranja** = Capa de decisión (LLM + código determinístico).
- **Violeta** = Backtests (evidencia empírica antes de proponer).
- **Rojo** = Guardrails (invariants duros, último filtro).
- **Verde** = Dispatch (cómo se entrega la propuesta según modo).
- **Amarillo** = Aplicación (la única capa que toca el controller real).

**Toda decisión que llega al humano pasó por: routines → LLM → expansor → backtests → invariants.** Cualquier paso que falle, no llega.
