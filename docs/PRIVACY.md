# Privacy and public-release policy

Only source code, tests, documentation, configuration templates, and clearly synthetic examples belong in the public repository.

## Never publish

- Real model-update workbooks, holdings, weights, returns, account identifiers, or manager commentary.
- SQLite databases, raw exports, local manifests, or audit reports.
- `.env` files, API keys, Streamlit secrets, or local override files.
- Private repository history.

## Local-only locations

- `data/raw/`
- `data/db/`
- `config/*.local.json`
- `.streamlit/secrets.toml`
- `.env` and `.env.*`

## Release process

`tools/export_public_release.py` copies only an explicit allowlist into a clean packaging worktree or public repository. It refuses dirty source/target repositories, removes files outside the allowlist from the target, scans names and text for denied private references and likely secrets, and never copies Git history.

Synthetic examples must be labeled as synthetic and use fictional data. A public release is validated again from the destination repository before it is tagged.
