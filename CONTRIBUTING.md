# Contributing

## Workflow

1. Work on a branch named `<type>/<short-description>`, for example `feat/redaction-module`.
2. Keep changes small: one concern per commit and per review.
3. Before committing, run the checks (or let pre-commit run them):

   ```powershell
   ruff check . ; ruff format --check . ; mypy ; pytest
   ```

4. Open a review request with a clear description. All checks must pass.

## Commit messages

[Conventional Commits](https://www.conventionalcommits.org/): `<type>(<scope>): <summary>`.
Types: `feat`, `fix`, `docs`, `test`, `refactor`, `chore`, `build`. Use the imperative mood
("add inspector", not "added inspector"). Explain *why* in the body when it is not obvious.

## Code standards

- **Python 3.11+**, `src/` layout, one responsibility per module.
- **Type hints on everything**; `mypy --strict` must pass.
- **Docstrings** (Google style) on every module, class and public function: what it does,
  arguments, return value. Comments explain *why*, not *what*.
- **Naming**: `snake_case` for functions and modules, `PascalCase` for classes,
  `UPPER_CASE` for constants. Names describe purpose (`record_message`, not `handle`).
- **Keep it simple**: small functions, no clever tricks, no code that is not used yet.
- **Formatting and lint**: `ruff` decides. Do not argue with the formatter.
- **Tests**: every behaviour has a unit test. Use hand-written synthetic data in
  `tests/fixtures/`. Never use real transcripts, real secrets or real personal data.
- **Assumptions about Claude Code** are marked `[verify]` in a comment or docstring until
  confirmed in [docs/verify/transcript-facts.md](docs/verify/transcript-facts.md).

## Security rules

- Never commit `.env`, keys, transcripts or database dumps (the `.gitignore` blocks them; do
  not work around it).
- Never print or log a secret or raw personal data. Log the rule that matched, not the value.
- Capture code is read-only toward the developer's work: it must never change their commits,
  branches or files.

## Review checklist

- [ ] Does it do one thing, and is the name of each module, function and variable clear?
- [ ] Are docstrings and comments present and accurate?
- [ ] Are there tests for the new behaviour, including failure cases?
- [ ] Do ruff, mypy and pytest pass?
- [ ] No secrets, real data or unrelated changes?
- [ ] Are documentation and `CHANGELOG.md` updated?
