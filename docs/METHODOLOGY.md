# Performance methodology

## Return definition

The project calculates daily CAD-denominated time-weighted returns (TWR). Daily returns are geometrically linked for period results. TWR is used to assess the investment process independently of owner contributions and withdrawals; IRR and other money-weighted returns are out of scope.

## Snapshot timing

A holdings snapshot dated on trading day _t_ becomes effective on the next observed trading day. This avoids applying a model update to a return that may have occurred before the update was available.

## Weight drift

Snapshot weights initialize each security's weight. Between snapshots, weights drift according to relative security returns:

`next weight = current weight × security gross return ÷ portfolio gross return`

When a later snapshot becomes effective, target weights are reset to that snapshot. Snapshot weights below 100% imply residual cash with a zero daily return. Long-only snapshots above 100% are rejected.

## Currency

Every holding stores `CAD` or `USD`. Canadian suffix rules cover `.TO`, `.V`, `.NE`, and `.CN`; ambiguous listings require an explicit local override. USD security prices are converted to CAD using stored `CADUSD=X` observations before returns are calculated.

## Missing data

Active positions are not silently dropped or renormalized when prices or FX data are missing. Strict calculations raise a clear data-quality error; dashboard-safe paths preserve valid history and display a warning.

## Benchmark and seeded history

The default benchmark is the S&P/TSX Composite (`^GSPTSE`). Earlier manager-reported monthly results can be loaded into `seeded_returns`; later detailed results are calculated from holdings, prices, and FX. The transition date is explicit in the code and stored data.

## Limitations

The model does not currently handle owner cash flows, fees, taxes, intraday execution, short positions, leverage above 100%, or complex corporate actions. yfinance is suitable for a portfolio demonstration, not a guaranteed institutional data feed.
