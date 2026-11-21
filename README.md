# eu-gas-dash

European gas market dashboard built on public data: storage, LNG, pipeline flows, urgent market
messages and capacity auctions. Same design as [gb-power-dash](https://github.com/amun0905/gb-power-dash):
GitHub Actions fetch the data on a schedule, store it as Parquet in a bucket, and write small JSON
files that a static website (GitHub Pages) reads.

```
 GitHub Actions (every 2 h)                    Object storage (Cloudflare R2)
 ┌─────────────────────────────────┐           ┌────────────────────────────────────────┐
 │ gasdash update                  │  Parquet  │ eu-gas/agsi_storage/2026-11.parquet    │
 │  fetch → validate → upsert  ────┼─────────► │ eu-gas/entsog_flows/_manifest.json ... │
 │ gasdash export   (web/*.json)   │           │ eu-gas/web/storage.json  flows.json …  │
 │ gasdash status --fail-on-stale  │           └──────────────────┬─────────────────────┘
 └─────────────────────────────────┘                              │ read by the browser
   ▲ GIE AGSI+/ALSI/IIP · ENTSOG · National Gas · Open-Meteo      ▼
                                                     site/ on GitHub Pages
```

## Pages

| Tab | Shows | Sources |
|---|---|---|
| Storage | EU / country fill vs 5-year range, fill by country, net injection, heating degree days | GIE AGSI+, Open-Meteo |
| LNG | Send-out by country, tank inventory, send-out vs 5-year range, ENTSOG LNG-point cross-check, terminal table (utilisation, days of cover, likely unloadings) with drill-down | GIE ALSI, ENTSOG |
| Flows | Norway landings (incl. Langeled into GB), Russian routes (TurkStream), other pipeline imports, flows to Ukraine, key interconnectors, GB NTS supply & demand | ENTSOG, National Gas |
| Events | Active and planned unavailability messages, capacity-offline timeline | ENTSOG UMMs, GIE IIP |
| Auctions | Capacity auctions that cleared with a premium | ENTSOG CMP data |
| Sources | Every source, its terms, and how fresh each dataset is | all |

A **GWh | mcm** toggle in the header switches Storage, LNG, Flows and Events between energy and volume.
Continental figures use 10.55 kWh/m³ (the common European reference); GB figures show National Gas's
own published volumes. The choice is remembered per browser.

Every chart names its source; GIE requires a visible credit and gets one on every page.
Prices are deliberately left out (no free official API for TTF/THE).

## Datasets

| name | source | what | cadence |
|---|---|---|---|
| `agsi_storage` | GIE AGSI+ | storage by country + EU, 2016→ | daily, D+1 ~19:30 CET |
| `alsi_lng` | GIE ALSI | LNG send-out & inventory by country + EU, 2018→ | daily |
| `alsi_terminals` | GIE ALSI | send-out & tank inventory for each LNG terminal, 2022→ | daily |
| `gie_umm` | GIE IIP | REMIT UMMs from SSOs/LSOs/TSOs | event-driven |
| `entsog_flows` | ENTSOG | physical flow + nomination at the points in `config/entsog_points.yaml`, 2021→ | daily |
| `entsog_umm` | ENTSOG | TSO urgent market messages | event-driven |
| `entsog_cmp_auctions` | ENTSOG | CMP auction premiums | sparse |
| `gb_nts` | National Gas | GB NTS supply by source, demand, storage stock | daily, D+1 |
| `weather_temp` | Open-Meteo | daily mean temperature, 8 cities (→ HDD) | daily + 9-day forecast |

## Choosing ENTSOG points

`config/entsog_points.yaml` lists every line on the Flows tab. A line is the sum of one or more
point-directions, given as explicit keys (`operatorKey + pointKey + direction`) or as a `match:` rule
(e.g. every "LNG Entry point"). Readings are taken on the EU side of each border and from one operator
per physical flow, so nothing is counted twice; net interconnector flows use `sign: -1` on the exit.

`gasdash points` checks every key against ENTSOG's live point list and prints what each rule picks up.
The keys shipped here come from ENTSOG's own data, but ENTSOG renames points from time to time, so
run the **probe** workflow once after setup and fix any `unknown_key` it reports. Unknown keys are
skipped (with a warning and a badge on the Flows tab), never fatal.

## How robustness is handled

Same core as gb-power-dash:
- **Idempotent upserts** on declared keys; with a `version_column` the latest publication wins.
- **Revisions**: every run re-fetches `lookback_days` (GIE revises the last few days; ENTSOG nominations become physical flows).
- **Schema guard**: only declared columns are kept and typed; missing key columns fail loudly with the columns the API returned.
- **Retries** with back-off on 429/5xx; GIE requests are paced under its 60 calls/minute limit; ENTSOG queries are split into small batches to stay under its 60 s query limit.
- **Fallbacks**: National Gas JSON API → documented CSV download; ENTSOG "no data" (404) is treated as empty, not as an error.
- **Graceful degradation**: one broken feed never stops the others; GIE datasets are skipped (not failed) until `GIE_API_KEY` is set.
- **Resumable backfill**, **monitoring** (`status --fail-on-stale` → GitHub emails you) and the cron **keep-alive** step.

## Deploy (about 15 minutes, reusing the gb-power-dash bucket)

1. **Repo.** Push this folder to `amun0905/eu-gas-dash`.
2. **Secrets and variables** (*Settings → Secrets and variables → Actions*):
   - Variable `GASDASH_STORE` = `s3://<your R2 bucket>/eu-gas` (same bucket as gb-power-dash, new prefix)
   - Secret `GASDASH_S3_ENDPOINT` = `https://<ACCOUNT_ID>.r2.cloudflarestorage.com`
   - Secrets `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` = the R2 token's keys (same as gb-power-dash)
   - Secret `GIE_API_KEY` = your AGSI/ALSI key (one key covers AGSI+, ALSI and IIP)
3. **Check the sources.** *Actions → probe → Run workflow.* Read the log: ENTSOG point check, then a
   sample from every dataset. Fix any unknown ENTSOG keys in `config/entsog_points.yaml`.
4. **History.** *Actions → backfill → Run workflow* (about an hour; GIE's rate limit sets the pace).
5. **Website.** *Settings → Pages → Source: GitHub Actions.* If the bucket isn't on the same public
   `r2.dev` URL as gb-power-dash, change `DATA_BASE` in `site/config.js`. The bucket's CORS rule for
   `https://amun0905.github.io` already covers this site.
6. **Live updates.** `update-data` now runs every two hours.

## Run locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest -q                          # offline tests: every API is replaced by a fake (tests/fakes.py)
python scripts/demo_data.py        # synthetic store in ./demo-data for website work
python -m http.server 8000         # then open http://localhost:8000/site/?data=/demo-data

export GIE_API_KEY=...             # real data:
gasdash points                     # check ENTSOG keys
gasdash probe                      # 3 days from every source, printed
gasdash update && gasdash export   # into ./data
```

## Licences and attribution

- GIE AGSI+/ALSI/IIP: free reuse with a visible "GIE (Gas Infrastructure Europe), AGSI/ALSI" credit.
- ENTSOG Transparency Platform, National Gas Data Portal: public transparency data.
- Open-Meteo: CC BY 4.0, free for non-commercial use.

Check each source's terms before any commercial use.
