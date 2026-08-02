# Contributing

Issues and pull requests are welcome.

## Setup

```sh
git clone https://github.com/me-cedric/BrakeSmith.git
cd BrakeSmith
uv sync --extra dev
uv run pytest
uv run ruff check .
```

Keep changes focused. Add tests for new selection, discovery, or safety behavior. Never add a path that deletes or overwrites source media.

## Pull requests

- Explain the user-visible change.
- Include relevant tests.
- Run tests and lint locally.
- Update README examples when CLI behavior changes.
