"""The eval harness: what turns a claim of improvement into a measurement.

Deliberately outside the test suite. Tests answer pass/fail and run per commit;
evals produce *numbers*, cost real tokens, and are non-deterministic. Conflating
them gives a flaky suite and an unmeasured model.

See docs/ml/EVALUATION.md for what each metric means and
docs/ml/BENCHMARKS.md for where results are recorded.
"""
