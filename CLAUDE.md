# Working in this repo

## Tests

- `uv run pytest` runs everything against Postgres 12, 15, 16, 17, 18 and 19. It needs Docker and builds
  a `postgres-hypopg:<N>-hypopg<version>` image per Postgres version the first time.
- Select versions with `PG_TEST_VERSIONS`, for example `PG_TEST_VERSIONS=15,19 uv run pytest`.
- `PG_TEST_STRICT=1` (set in CI) makes environment problems and any skip not listed in
  `ALLOWED_STRICT_SKIPS` (`tests/conftest.py`) fail the run instead of skipping. Run with it before
  you trust a green result. If you add an intentional skip, add it to that list and say why.
- Compatibility policy: Postgres 15 and later through the newest release are supported and tested;
  12 is a best-effort older representative. 13 and 14 are intentionally not tested.

## Index advisor (DTA): run its tests when you touch it

The DTA integration tests (`tests/integration/dta/`, marker `dta`) are slow and planner-sensitive, so
PR CI deselects them (`-m "not dta"`); only the full CI tier (tags, manual, daily) runs them.
If you change `src/postgres_mcp/index/` or anything the advisor depends on (SQL driver, hypothetical
index handling, `pg_stat_statements` queries), run them yourself before finishing:

    PG_TEST_STRICT=1 PG_TEST_VERSIONS=15,19 uv run pytest -m dta

`test_diminishing_returns` is known to be flaky (planner-dependent). If it fails, re-run it once and
say so in your report; do not delete or weaken it.

## HypoPG pin

Test images build HypoPG from the release tag `HYPOPG_VERSION` in `tests/utils.py` (never from master).
A weekly workflow (`.github/workflows/hypopg-update-check.yml`) opens an issue when a newer stable
release exists. To bump: change the constant, run the full matrix (`workflow_dispatch` on the CI
workflow, or `uv run pytest` locally), and fix anything HypoPG changed.

## CI

`.github/workflows/build.yml`: PRs and pushes to `main` run Postgres 15 and 19 without DTA. Tags, the
manual "Run workflow" button, and a daily schedule run all six versions with DTA. The daily run skips
if that exact commit already passed a full run. Require the single `postgres-mcp-ci` check in branch
protection.
