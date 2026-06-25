# Privacy

This repository is prepared as a public portfolio package. It should contain source code, tests, documentation, configuration templates, and synthetic examples only.

## Do Not Commit

- Real model-update workbooks.
- Private seeded returns files.
- Local SQLite databases.
- Local ticker override files.
- `.env` files.
- Streamlit secrets.
- Private holdings, weights, returns, account details, or manager commentary.

## Ignored Local Paths

Private local files should remain in ignored paths:

- `data/raw/`
- `data/db/`
- `config/ticker_overrides.local.json`
- `.env` / `.env.*`
- `.streamlit/secrets.toml`

The `.gitkeep` files under `data/raw/` and `data/db/` exist only to preserve the directory structure.

## Public Examples

Examples committed to this repo should be synthetic and clearly labeled as examples. Filenames should use neutral placeholders such as `manager_model_update_YYYY-MM-DD.xlsx` rather than names from a real manager or account.

## Clean-History Publishing

This worktree is intended to prepare files that can later be copied into a separate clean-history public repository. The existing private development history should not be assumed safe for public release.
