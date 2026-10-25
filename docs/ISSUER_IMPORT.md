# Free issuer export adapter

`history-issuer` converts locally supplied iShares SpreadsheetML exports and the
ECB's historical reference-rate XML into the monthly research contract. It makes
no network request, needs no API key and creates no subscription. It does not
select ETFs or change the investment rules.

## Run a fictional example

```sh
python3 examples/issuer_demo.py
```

The example creates 48 months of fictional fund/benchmark data and fictional FX
under `var/issuer-demo/`. It runs with Python alone, including `python3 -S`.
Its successful import proves the software contract, not investment performance.
Repeating it preserves identical files.

## Use supplied exports privately

The issuer offers a fund download on its product pages, including separate fund
growth, benchmark growth and metadata sheets. The parser supports the observed
English SpreadsheetML financial prefix ending at `Key Facts`, not arbitrary Excel
files. It checks the fund name, ISIN, native share-class currency, UCITS declaration
and equity/bond role against explicit operator bindings.

Free access is not an open-data licence. Review the
[issuer's terms](https://www.ishares.com/uk/individual/en/compliance/terms-and-conditions)
for the intended use. Keep real source files, converted datasets and derived
reports under ignored `var/` or `private/`; public examples are entirely fictional.
An `operator_confirmed_private_analysis` declaration records the operator's review,
and does not grant redistribution rights or authenticate the publisher's data.

Obtain the ECB XML from its
[reference-rate downloads](https://www.ecb.europa.eu/stats/policy_and_exchange_rates/euro_reference_exchange_rates/html/index.en.html).
The ECB quotes foreign currency per EUR. These are reference observations for
research, not executable FX quotes. Follow its
[attribution and modification conditions](https://www.ecb.europa.eu/services/using-our-site/disclaimer/html/index.en.html).

Use the example's `spec.json` shape with:

- The actual retrieval timestamp and exact requested signal-month coverage.
- Two research instruments in EUR, one equity role and one bond role.
- Per-instrument bindings: `isin`, exact `fund_name`, `native_currency` (`EUR` or
  `USD`) and a public `evidence_url` without credentials/query parameters.
- `origin: declared_historical` and the reviewed private-use declaration for real
  data. Fictional examples keep their synthetic origin and usage labels.

```sh
etf-autopilot history-issuer var/issuer/spec.json \
  var/issuer/equity.xls var/issuer/bond.xls var/issuer/ecb.xml \
  --out-dir var/issuer/normalized
```

The adapter validates before writing immutable `manifest.json`, `monthly.csv` and
`dataset.json`. Different source bytes or assumptions require a new output
directory. No genuine historical performance result is shipped with this adapter.

## Financial and calendar semantics

Only the **fund** growth column is used. The issuer describes fund performance on
a NAV basis with income reinvested where applicable and fund expenses reflected;
this differs from an exchange execution price. Benchmark-only undated rows are
counted and excluded. A dated row missing a fund value fails; a benchmark never
replaces it. A constant 10,000 seed before class inception is counted/excluded;
other prelaunch values fail.

USD fund indices are divided by the **same-date USD-per-EUR reference rate**.
EUR indices are retained. Both are rebased to 100 on their first joint observation.
Only dates present in both funds and ECB are retained. Each source records how
many dates that policy excludes within requested coverage. This is an explicit
joint-observation calendar, not a complete exchange calendar; values are never
filled, interpolated or carried forward.

Each requested signal month still needs at least ten joint observations and a
final observation within four days of month end. Availability is assumed at 24
hours after UTC day end. The execution proxy must be a strictly later observation
in the following month, within the existing seven-day limit. Missing coverage
fails instead of shortening the period. These timestamps do not prove historical
publication times; the issuer's series may have been revised retrospectively.

Version-3 manifests record `unitized_issuer_return_proxy`, original-file and binding
hashes, reference FX, exclusions, normalization and timing policies. The parser
hashes the entire original file but reads only its completed financial prefix;
malformed financial XML fails and later holdings XML is not parsed or repaired.
UTF-8, document size, row, date, value and entity constraints are enforced.

Use the dataset only through [registered research evaluation](DATA_EVALUATION.md).
Its proxy quantities cannot be sent to a broker or used by the forward paper
engine. Reports retain reference-FX, retrospective-series and calendar limitations;
no model is invoked or strategy promoted. Exact fund selection, source rights and
broker compatibility remain separate decisions.
