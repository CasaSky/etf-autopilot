# Free issuer export adapter

`history-issuer` converts locally supplied iShares SpreadsheetML exports and the
observed State Street accumulating-NAV workbook format, with the ECB's historical
reference-rate XML, into the monthly research contract. It makes
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

To exercise a fictional NAV workbook with a declared share split:

```sh
python3 -S examples/issuer_demo.py --equity-nav --output-dir var/nav-demo
```

Both examples are offline, require no additional packages, and retain the same
financial strategy rules.

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

Some exports contain only fund-level metadata. For these, add
`reviewed_share_class_launch_date: YYYY-MM-DD` to the binding after checking an
issuer document for that exact ISIN. The adapter requires matching NAV-header and
base currencies, rejects conflicts with any published class date/currency, and
records this explicit metadata basis/date in provenance. It never assumes a fund
launch establishes share-class inception. Existing class-level exports need no
additional declaration.

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

## Accumulating NAV and share splits

Import spec version 2 declares `source_format` for each binding:
`ishares_growth_spreadsheetml_v1` or `ssga_accumulating_nav_xlsx_v1`. The existing
version-1 spec keeps its original growth-only behavior and version-3 output.
The NAV format currently supports the equity role, a single English `navhist`
worksheet, and the observed date/NAV columns. It is not a general Excel importer.
ISIN, exact name and native currency must match the supplied binding. Check the
issuer's exact share-class documents for UCITS status and accumulation policy.

A NAV binding also requires `reviewed_share_class_launch_date`,
`income_treatment: accumulating`, and an explicit `corporate_actions` list (empty
when none apply). Each split declaration contains:

```json
{
  "ex_date": "2023-03-06",
  "shares_after_per_before": "5",
  "evidence_url": "https://example.invalid/fictional-split-notice"
}
```

This is fictional. Use independently reviewed issuer notices for actual events;
the importer records the declaration and does not fetch or authenticate its URL.
A split changes the number of shares, not the holding's value. The adapter divides
pre-split NAV by the declared factor, compounding multiple reviewed splits. It
requires observations on the ex-date and shortly before it. A factor inconsistent
with the raw boundary fails, including a second adjustment to an already adjusted
series. Distributing funds are rejected: raw NAV alone would omit their payments.

Adjacent adjusted NAV ratios outside 0.5–1.5 stop for review. This conservative
input-quality guard neither infers corporate actions nor clips genuine returns;
large market moves also require source review. It cannot establish that every
corporate action was found. Keep the original workbook and reviewed notices.

The reader checks compressed/expanded sizes, ZIP paths, sheet relationships, cell
references, dates and ordering. It rejects formulas, entities, missing observations
and external data relationships. Hyperlinks are ignored and never followed. Only
the observed final blank row plus marketing-text cell is excluded; a partial
financial row cannot be relabelled as a disclaimer. The entire source is hashed.

Mixed NAV/growth imports produce version-4 manifests with per-source formats,
reviewed class metadata, report date and split declarations. EUR conversion, joint
dates, assumed publication lag, later execution proxy and normalization retain the
same policies as the growth importer. Every historical protocol must be registered
against the resulting data/config/code fingerprints before evaluation. Neither a
new format nor a corrected split makes an already examined period fresh again.

## Aligning candidate comparisons

Two issuer pairs can have different first eligible observations after month end.
Compare their timestamp sequences before computing results. Version-2 specs can
declare an `execution_dates` object mapping **every signal month** to an exact
date in the following month, for example `"2022-12": "2023-01-03"`. Freeze the
calendar before inspecting performance and apply it identically to each candidate.

Every declared date must have a genuine joint fund/FX observation, occur after the
assumed availability time and satisfy the existing seven-day bound. A missing date
fails; there is no nearest-date replacement or price interpolation. Signals retain
their completed-month rule and still need matching timestamps across candidates.
The manifest records the complete calendar, and dataset validation checks each
execution against it. Preserve prior studies: a calendar correction changes the
dataset and requires a new registered protocol, even when investment rules match.

Use the dataset only through [registered research evaluation](DATA_EVALUATION.md).
Its proxy quantities cannot be sent to a broker or used by the forward paper
engine. Reports retain reference-FX, retrospective-series and calendar limitations;
no model is invoked or strategy promoted. Exact fund selection, source rights and
broker compatibility remain separate decisions.
