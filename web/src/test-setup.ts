/**
 * Test-run configuration, loaded by vitest before any spec.
 *
 * The one thing set here is how many examples the generated tests run, and it
 * is keyed on `CI` for the same reason `tests/conftest.py` is on the Python
 * side: a hundred examples is a tolerable wait on a laptop and a weak search,
 * five hundred is the opposite of both. Keeping the two languages on the same
 * numbers means "500 in CI" is one sentence in the docs rather than two.
 *
 * `process` is reached through `globalThis` because this project's `tsconfig`
 * lists only `vitest/globals` in `types`, so Node's ambient declarations are
 * deliberately not in scope for application code.
 */
import fc from 'fast-check';

const env = (globalThis as { process?: { env?: Record<string, string | undefined> } }).process?.env;

fc.configureGlobal({
  numRuns: env?.CI ? 500 : 100,
  // Put the thrown assertion in the counterexample report. Without it a failure
  // names the shrunk input but not which assertion rejected it, and the two
  // together are what make a generated failure reproducible by reading.
  includeErrorInReport: true,
});
