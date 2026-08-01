# Solana Ecosystem Pulse

Solana Ecosystem Pulse is a dependency-free, read-only report and dashboard
for current Solana network health, validator posture, supply, and market
signals. The published snapshot includes `index.html`, `report.md`, and
`report.json`; `pulse_report.py` can refresh them at any time.

## Run

Python 3.10+ is enough:

```text
python pulse_report.py --out-dir generated
```

Open `generated/index.html`. Set `--rpc-url` to another public Solana JSON-RPC
endpoint when desired. The collector has no API keys, wallets, secrets, or
third-party Python dependencies.

## What it measures

- Solana RPC health, finalized slot, epoch progress, recent TPS, and slot time.
- Active/delinquent validators and top-ten active stake concentration.
- Total, circulating, and non-circulating SOL supply.
- SOL/USD price from CoinGecko and Solana TVL from DeFiLlama.
- Transparent baseline alerts for material TPS, slot-time, price, TVL, and
  delinquent-validator changes.

## Sources and safety

The collector uses public read-only Solana JSON-RPC (`getHealth`, `getSlot`,
`getEpochInfo`, `getRecentPerformanceSamples`, `getVoteAccounts`, and
`getSupply`), CoinGecko public HTTP JSON, and DeFiLlama public HTTP JSON. It
never signs, submits, simulates, or mutates a transaction. Missing upstream
data is rendered as `n/a` instead of guessed.

## Test

```text
python -m unittest test_pulse_report.py -v
```

The tests cover the anomaly thresholds, first-run baseline, read-only metric
derivation, and the JSON/Markdown/HTML output contract.

## Reproducibility

The generated snapshot is timestamped in UTC and lists its source endpoints.
Run the script again to produce a new snapshot; a previous JSON report can be
used as the baseline for anomaly detection.
