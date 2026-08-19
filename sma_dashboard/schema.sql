CREATE TABLE IF NOT EXISTS holdings (
    date TEXT NOT NULL,
    ticker TEXT NOT NULL,
    weight REAL NOT NULL,
    currency TEXT NOT NULL DEFAULT 'CAD' CHECK (currency IN ('CAD', 'USD')),
    shares REAL,
    cost_basis REAL,
    PRIMARY KEY (date, ticker)
);

CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    ticker TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('buy', 'sell', 'trim', 'add')),
    weight_change REAL NOT NULL,
    notes TEXT,
    CHECK (
        (action IN ('buy', 'add') AND weight_change > 0)
        OR (action IN ('sell', 'trim') AND weight_change < 0)
    )
);

CREATE TABLE IF NOT EXISTS prices (
    date TEXT NOT NULL,
    ticker TEXT NOT NULL,
    close REAL NOT NULL,
    adj_close REAL NOT NULL,
    PRIMARY KEY (date, ticker)
);

CREATE TABLE IF NOT EXISTS fx_rates (
    date TEXT NOT NULL,
    pair TEXT NOT NULL,
    rate REAL NOT NULL,
    PRIMARY KEY (date, pair)
);

CREATE TABLE IF NOT EXISTS seeded_returns (
    date TEXT PRIMARY KEY,
    return_pct REAL NOT NULL,
    benchmark_return_pct REAL,
    source TEXT NOT NULL,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS corporate_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    ticker TEXT NOT NULL,
    action_type TEXT NOT NULL,
    description TEXT NOT NULL,
    adjustment_applied INTEGER NOT NULL DEFAULT 0 CHECK (adjustment_applied IN (0, 1))
);

CREATE TABLE IF NOT EXISTS transcripts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    quarter_label TEXT NOT NULL UNIQUE,
    full_text TEXT NOT NULL,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS rejected_rows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date_attempted TEXT NOT NULL,
    source_file TEXT NOT NULL,
    raw_row_data TEXT NOT NULL,
    reason TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS model_updates_applied (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    model_date DATE NOT NULL,
    file_name TEXT,
    source TEXT,
    ingested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    ingested_by TEXT,
    notes TEXT,
    UNIQUE(model_date)
);

CREATE INDEX IF NOT EXISTS idx_holdings_date ON holdings (date);
CREATE INDEX IF NOT EXISTS idx_trades_date ON trades (date);
CREATE INDEX IF NOT EXISTS idx_prices_ticker_date ON prices (ticker, date);
CREATE INDEX IF NOT EXISTS idx_fx_rates_pair_date ON fx_rates (pair, date);
