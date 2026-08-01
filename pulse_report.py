#!/usr/bin/env python3
"""Generate a dependency-free, read-only Solana ecosystem report.

The collector uses public JSON-RPC/HTTP endpoints only. It never signs,
submits, or simulates a transaction and it never needs an API key.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen


DEFAULT_RPC = "https://api.mainnet-beta.solana.com"
USER_AGENT = "solana-ecosystem-pulse/0.1 (read-only public metrics)"


def fetch_json(url: str, payload: dict[str, Any] | None = None, timeout: int = 20) -> Any:
    """Fetch JSON with a short timeout and a descriptive error."""
    body = None
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(url, data=body, headers=headers, method="POST" if body else "GET")
    try:
        with urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except (OSError, URLError, ValueError) as exc:
        raise RuntimeError(f"request failed for {url}: {exc}") from exc


def rpc_call(rpc_url: str, method: str, params: list[Any] | None = None) -> Any:
    response = fetch_json(
        rpc_url,
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or []},
    )
    if response.get("error"):
        raise RuntimeError(f"RPC {method}: {response['error']}")
    return response.get("result")


def optional(fn, default=None):
    try:
        return fn()
    except RuntimeError as exc:
        return {"error": str(exc)} if default is None else default


def collect_rpc(rpc_url: str) -> dict[str, Any]:
    health = optional(lambda: rpc_call(rpc_url, "getHealth"))
    epoch = optional(lambda: rpc_call(rpc_url, "getEpochInfo"))
    slot = optional(lambda: rpc_call(rpc_url, "getSlot", [{"commitment": "finalized"}]))
    block_time = optional(lambda: rpc_call(rpc_url, "getBlockTime", [slot]) if isinstance(slot, int) else None)
    samples = optional(lambda: rpc_call(rpc_url, "getRecentPerformanceSamples", [5]))
    vote_accounts = optional(lambda: rpc_call(rpc_url, "getVoteAccounts", [{"commitment": "finalized"}]))
    supply = optional(lambda: rpc_call(rpc_url, "getSupply", [{"commitment": "finalized"}]))

    latest_sample = samples[0] if isinstance(samples, list) and samples else {}
    sample_period = latest_sample.get("samplePeriodSecs") or 0
    transactions = latest_sample.get("numTransactions") or 0
    num_slots = latest_sample.get("numSlots") or 0
    current_validators = vote_accounts.get("current", []) if isinstance(vote_accounts, dict) else []
    delinquent_validators = vote_accounts.get("delinquent", []) if isinstance(vote_accounts, dict) else []
    stakes = sorted((int(v.get("activatedStake", 0)) for v in current_validators), reverse=True)
    total_stake = sum(stakes)
    top10_stake = sum(stakes[:10])
    lamports = 1_000_000_000
    supply_value = supply.get("value", {}) if isinstance(supply, dict) else {}

    metrics: dict[str, Any] = {
        "rpcUrl": rpc_url,
        "rpcHealth": health,
        "slot": slot,
        "blockTime": block_time,
        "epoch": epoch.get("epoch") if isinstance(epoch, dict) else None,
        "epochSlotIndex": epoch.get("slotIndex") if isinstance(epoch, dict) else None,
        "slotsInEpoch": epoch.get("slotsInEpoch") if isinstance(epoch, dict) else None,
        "epochProgressPct": round(
            100 * epoch.get("slotIndex", 0) / epoch.get("slotsInEpoch", 1), 2
        ) if isinstance(epoch, dict) and epoch.get("slotsInEpoch") else None,
        "samplePeriodSecs": sample_period,
        "transactionsInSample": transactions,
        "slotsInSample": num_slots,
        "tps": round(transactions / sample_period, 2) if sample_period else None,
        "slotTimeMs": round(sample_period * 1000 / num_slots, 2) if num_slots else None,
        "activeValidators": len(current_validators),
        "delinquentValidators": len(delinquent_validators),
        "top10StakePct": round(100 * top10_stake / total_stake, 2) if total_stake else None,
        "currentStakeSol": round(total_stake / lamports, 2),
        "totalSupplySol": round((supply_value.get("total") or 0) / lamports, 2),
        "circulatingSupplySol": round((supply_value.get("circulating") or 0) / lamports, 2),
        "nonCirculatingSupplySol": round((supply_value.get("nonCirculating") or 0) / lamports, 2),
    }
    return metrics


def collect_market() -> dict[str, Any]:
    price = optional(
        lambda: fetch_json(
            "https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd"
        )
    )
    tvl = optional(lambda: fetch_json("https://api.llama.fi/v2/historicalChainTvl/Solana"))
    price_usd = None
    if isinstance(price, dict):
        price_usd = price.get("solana", {}).get("usd")
    tvl_usd = None
    if isinstance(tvl, list) and tvl:
        latest = tvl[-1]
        tvl_usd = latest.get("tvl") if isinstance(latest, dict) else None
    return {"solPriceUsd": price_usd, "solanaTvlUsd": tvl_usd}


def compare(previous: dict[str, Any] | None, current: dict[str, Any]) -> list[dict[str, Any]]:
    if not previous:
        return []
    old = previous.get("metrics", {})
    alerts: list[dict[str, Any]] = []

    def pct_alert(key: str, label: str, threshold: float) -> None:
        before, after = old.get(key), current.get(key)
        if not isinstance(before, (int, float)) or not isinstance(after, (int, float)) or before == 0:
            return
        change = (after - before) / abs(before) * 100
        if abs(change) >= threshold:
            alerts.append({"metric": key, "label": label, "changePct": round(change, 2), "severity": "medium"})

    pct_alert("tps", "Recent TPS changed materially", 30)
    pct_alert("slotTimeMs", "Slot time changed materially", 25)
    pct_alert("solPriceUsd", "SOL price moved materially", 8)
    pct_alert("solanaTvlUsd", "Solana TVL moved materially", 10)
    old_delinquent, new_delinquent = old.get("delinquentValidators"), current.get("delinquentValidators")
    if isinstance(old_delinquent, int) and isinstance(new_delinquent, int) and new_delinquent - old_delinquent >= 10:
        alerts.append({"metric": "delinquentValidators", "label": "Delinquent validator count increased", "change": new_delinquent - old_delinquent, "severity": "high"})
    return alerts


def make_report(rpc_url: str, previous: dict[str, Any] | None = None) -> dict[str, Any]:
    metrics = collect_rpc(rpc_url)
    metrics.update(collect_market())
    report = {
        "schema": "solana-ecosystem-pulse/v1",
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "readOnly": True,
        "metrics": metrics,
        "anomalies": compare(previous, metrics),
        "sources": {
            "solanaRpc": rpc_url,
            "coinGecko": "https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd",
            "defiLlama": "https://api.llama.fi/v2/historicalChainTvl/Solana",
        },
    }
    return report


def fmt(value: Any, suffix: str = "") -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:,.2f}{suffix}"
    return f"{value:,}{suffix}" if isinstance(value, (int, float)) else f"{value}{suffix}"


def markdown_report(report: dict[str, Any]) -> str:
    m = report["metrics"]
    lines = [
        "# Solana Ecosystem Pulse",
        "",
        f"Generated: `{report['generatedAt']}`",
        "",
        "This report uses read-only public endpoints. It does not sign, submit, or simulate transactions.",
        "",
        "## Network",
        "",
        f"- RPC health: **{m.get('rpcHealth', 'n/a')}**",
        f"- Slot: **{fmt(m.get('slot'))}**",
        f"- Epoch: **{fmt(m.get('epoch'))}**, {fmt(m.get('epochProgressPct'), '%')} complete",
        f"- Recent TPS: **{fmt(m.get('tps'))}**; slot time: **{fmt(m.get('slotTimeMs'), ' ms')}**",
        f"- Active validators: **{fmt(m.get('activeValidators'))}**; delinquent: **{fmt(m.get('delinquentValidators'))}**",
        f"- Top-10 active stake: **{fmt(m.get('top10StakePct'), '%')}**",
        "",
        "## Market and supply",
        "",
        f"- SOL price: **${fmt(m.get('solPriceUsd'))}**",
        f"- Solana TVL: **${fmt(m.get('solanaTvlUsd'))}**",
        f"- Total supply: **{fmt(m.get('totalSupplySol'))} SOL**",
        f"- Circulating supply: **{fmt(m.get('circulatingSupplySol'))} SOL**",
        "",
        "## Anomalies",
        "",
    ]
    anomalies = report.get("anomalies", [])
    lines.extend([f"- **{a['severity'].upper()}** — {a['label']} ({a.get('changePct', a.get('change', 'n/a'))})" for a in anomalies] or ["- No baseline anomalies detected. The first run establishes the baseline."])
    lines.extend(["", "## Sources", "", *[f"- {name}: {url}" for name, url in report["sources"].items()]])
    return "\n".join(lines) + "\n"


def dashboard_html(report: dict[str, Any]) -> str:
    payload = json.dumps(report, separators=(",", ":"), ensure_ascii=False).replace("</", "<\\/")
    return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Solana Ecosystem Pulse</title>
<style>
:root{{--bg:#0a1020;--panel:#111a2f;--panel2:#17233d;--text:#e9efff;--muted:#96a6c5;--cyan:#56d6ff;--green:#53e39b;--amber:#ffc56b;--red:#ff7d91;--line:#273757}}
*{{box-sizing:border-box}}body{{margin:0;background:radial-gradient(circle at 15% 0,#182b52 0,#0a1020 42%);color:var(--text);font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif}}main{{max-width:1180px;margin:auto;padding:42px 22px 70px}}header{{display:flex;justify-content:space-between;gap:20px;align-items:flex-end;margin-bottom:28px}}h1{{font-size:clamp(2rem,5vw,3.7rem);line-height:1;margin:0 0 12px;letter-spacing:-.05em}}h2{{margin:34px 0 14px;font-size:1.05rem;color:var(--cyan);letter-spacing:.08em;text-transform:uppercase}}p{{color:var(--muted);margin:5px 0}}.eyebrow{{color:var(--green);font-weight:700;letter-spacing:.13em;text-transform:uppercase;font-size:.75rem}}.stamp{{font-size:.82rem;color:var(--muted);text-align:right}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:13px}}.card{{background:linear-gradient(145deg,var(--panel),#0e172a);border:1px solid var(--line);border-radius:16px;padding:18px;min-height:118px;box-shadow:0 12px 40px #0002}}.label{{color:var(--muted);font-size:.8rem;text-transform:uppercase;letter-spacing:.08em}}.value{{font-size:1.75rem;font-weight:750;margin-top:12px;letter-spacing:-.03em}}.good{{color:var(--green)}}.warn{{color:var(--amber)}}.bad{{color:var(--red)}}.table{{width:100%;border-collapse:collapse;background:var(--panel);border:1px solid var(--line);border-radius:16px;overflow:hidden}}.table th,.table td{{padding:13px 15px;border-bottom:1px solid var(--line);text-align:left}}.table th{{color:var(--muted);font-size:.75rem;text-transform:uppercase;letter-spacing:.08em}}.table tr:last-child td{{border-bottom:0}}.note{{border-left:3px solid var(--cyan);background:#12223a;padding:13px 16px;color:var(--muted);margin-top:24px}}a{{color:var(--cyan)}}footer{{margin-top:35px;color:var(--muted);font-size:.83rem}}
@media(max-width:700px){{header{{display:block}}.stamp{{text-align:left;margin-top:18px}}main{{padding:28px 14px 50px}}}}
</style></head><body><main><header><div><div class="eyebrow">Read-only ecosystem telemetry</div><h1>Solana Ecosystem Pulse</h1><p>Network health, validator posture, supply and market signals in one compact view.</p></div><div class="stamp">Generated<br><strong id="generated"></strong></div></header>
<section><h2>Network pulse</h2><div class="grid" id="network"></div></section><section><h2>Market and supply</h2><div class="grid" id="market"></div></section><section><h2>Anomaly watch</h2><div id="anomalies"></div></section><div class="note">All measurements come from public read-only endpoints. This dashboard never signs, submits, or simulates a transaction. Missing upstream data is shown as <strong>n/a</strong> rather than guessed.</div><footer>Sources: <a href="https://docs.solana.com/api/http" rel="noreferrer">Solana JSON-RPC</a> · <a href="https://www.coingecko.com/en/api" rel="noreferrer">CoinGecko</a> · <a href="https://defillama.com/docs/api" rel="noreferrer">DeFiLlama</a></footer></main>
<script>const REPORT={payload};const m=REPORT.metrics;const n=v=>v==null?'n/a':(typeof v==='number'?v.toLocaleString(undefined,{{maximumFractionDigits:2}}):v);const card=(label,value,cls='')=>`<div class="card"><div class="label">${{label}}</div><div class="value ${{cls}}">${{value}}</div></div>`;document.getElementById('generated').textContent=new Date(REPORT.generatedAt).toLocaleString();document.getElementById('network').innerHTML=[card('RPC health',n(m.rpcHealth),m.rpcHealth==='ok'?'good':'warn'),card('Finalized slot',n(m.slot)),card('Epoch progress',n(m.epochProgressPct)+'%'),card('Recent TPS',n(m.tps)),card('Slot time',n(m.slotTimeMs)+' ms'),card('Active validators',n(m.activeValidators),'good'),card('Delinquent validators',n(m.delinquentValidators),m.delinquentValidators?'warn':'good'),card('Top-10 stake',n(m.top10StakePct)+'%')].join('');document.getElementById('market').innerHTML=[card('SOL price','$'+n(m.solPriceUsd)),card('Solana TVL','$'+n(m.solanaTvlUsd)),card('Total supply',n(m.totalSupplySol)+' SOL'),card('Circulating supply',n(m.circulatingSupplySol)+' SOL')].join('');const a=REPORT.anomalies||[];document.getElementById('anomalies').innerHTML=a.length?'<table class="table"><thead><tr><th>Severity</th><th>Metric</th><th>Signal</th></tr></thead><tbody>'+a.map(x=>`<tr><td class="${{x.severity==='high'?'bad':'warn'}}">${{x.severity.toUpperCase()}}</td><td>${{x.metric}}</td><td>${{x.label}}</td></tr>`).join('')+'</tbody></table>':'<div class="card"><div class="value good">No baseline anomalies</div><p>The first run establishes the comparison baseline.</p></div>';</script></body></html>'''


def write_outputs(report: dict[str, Any], out_dir: Path) -> None:
    data_dir = out_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (out_dir / "report.md").write_text(markdown_report(report), encoding="utf-8")
    (out_dir / "index.html").write_text(dashboard_html(report), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rpc-url", default=os.getenv("SOLANA_RPC_URL", DEFAULT_RPC))
    parser.add_argument("--out-dir", default="docs")
    args = parser.parse_args(argv)
    out_dir = Path(args.out_dir)
    previous_path = out_dir / "data" / "report.json"
    previous = None
    if previous_path.exists():
        try:
            previous = json.loads(previous_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            previous = None
    report = make_report(args.rpc_url, previous)
    write_outputs(report, out_dir)
    print(json.dumps({"generatedAt": report["generatedAt"], "outDir": str(out_dir), "anomalies": len(report["anomalies"])}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
