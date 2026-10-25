# Daily export adapter

`history-eodhd` converts operator-supplied daily JSON exports into the existing
monthly evaluation workflow. It uses the standard library, makes no network
requests and creates no subscription. Historical access and usage rights remain
separate prerequisites.

## Run the offline example

```sh
python3 examples/provider_demo.py
```

This generates fictional daily prices/calendars, an import specification and a
48-month dataset under `var/provider-demo/`. It proves software behavior, with no
investment evidence. Repeating it preserves identical artifacts.

## Import licensed exports privately

Keep exports, generated datasets and reports under ignored `var/` or `private/`.
The project's MIT license does not grant redistribution rights to vendor data.
Review EODHD's [terms](https://eodhd.com/financial-apis/terms-conditions) for your use;
its personal-use provisions distinguish private analysis from distribution/display.
Public examples contain only synthetic data.

The [daily API format](https://eodhd.com/financial-apis/api-for-historical-data-and-volumes)
contains `date`, `open`, `high`, `low`, `close`, `adjusted_close` and `volume`.
The adapter uses `adjusted_close`, which the vendor adjusts retrospectively for
dividends/splits. Preserve each complete export vintage; do not splice a newly
adjusted tail onto older adjusted history.

The example's `spec.json` shows the input shape:

| Field | Meaning |
|---|---|
| `version` | `1`, the adapter specification version |
| `origin` | `declared_historical` for actual data; fixtures remain `synthetic_fixture` |
| `retrieved_at` | Actual timezone-aware export retrieval time |
| `signal_start`, `signal_end` | Exact requested signal-month coverage, `YYYY-MM` |
| `instruments` | Two independently checked UCITS share classes: equity/bond, distinct ISINs, EUR |
| `bindings` | Each provider symbol, matching ISIN, `currency: EUR` and public `evidence_url` |
| `usage_rights` | `operator_confirmed_private_analysis`, or `synthetic_fixture` for fixtures |

Both listings must use the same venue and supply matching dates. Verify share
class, provider identity and listing currency against independent documents.
These bindings are operator declarations; the parser does not authenticate them.
Evidence URLs cannot contain credentials, query strings or fragments.

```sh
etf-autopilot history-eodhd var/eodhd/spec.json \
  var/eodhd/equity.json var/eodhd/bond.json \
  --out-dir var/eodhd/normalized
```

The outputs are immutable `manifest.json`, `monthly.csv` and `dataset.json`.
Different bytes/bindings/assumptions require a new output directory. Conversion
is validated before creating outputs; existing artifacts are never replaced with
different content. Feed `dataset.json` into [registered evaluation](DATA_EVALUATION.md).

## Timing and interpretation

Each requested month uses its last supplied weekday close, with at least ten bars
and a close within four calendar days of month end. The calendar still needs
independent checking: matching exports do not prove every trading day is present.

Availability is explicitly **assumed** at 24 hours after that day's UTC end.
Execution uses a strictly later supplied close in the following month, within
the existing seven-day gap limit. A later observation is required; no opening
price is invented. Missing/duplicate bars, incompatible calendars, invalid values,
currency/identity mismatches and observations after retrieval are rejected.
There is no FX conversion, interpolation or automatic coverage shortening.

Version-2 manifests use `unitized_adjusted_close_proxy`, with source/binding hashes,
usage declaration, retrospective-adjustment and availability policies. Reports
retain these limitations. These are research units, not executable shares or
certified point-in-time total-return history. The original version-1 total-return
contract and investment/risk rules remain supported unchanged.
