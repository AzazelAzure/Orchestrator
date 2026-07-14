# Contributing

Thanks for considering a contribution.

## Ground rules

1. Keep the core product-agnostic. Do not add adapters, brands, or default paths for specific private products.
2. Do not commit secrets, credentials, private absolute paths, databases, or generated caches.
3. Prefer small, reviewable changes with tests.
4. Run `pytest` before proposing a change. Use `ruff check` when touching Python.
5. Follow `AGENTS.md` for agent-assisted work (gates, trust-but-verify, finding logging, CPPRD when publishing changes).

## Development setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev,mcp]'
pytest
ruff check src tests
```

## Pull requests

- Describe intent and risk, not only the diff.
- Note which checks you ran.
- Call out any intentional omissions (partial verification, known residual risk).

## Security reports

See `SECURITY.md`. Do not file public issues for undisclosed vulnerabilities.
