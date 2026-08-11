"""A small, original dataset so a stranger can run this project.

Every number this project publishes comes from Spider, and Spider cannot ship
here: it is CC BY-SA, and [DATASETS.md](../../docs/ml/DATASETS.md) section 7
says benchmark data is not vendored into an MIT repository. Acquiring it is a
multi-gigabyte download and a conversion run.

That is a reasonable cost to *reproduce a benchmark* and an unreasonable one to
*see whether the thing works*. Without this package the first run of
``docker compose up`` ends with the API refusing to start -- correctly, because
an empty catalog means every identifier would be rejected -- and the shortest
path to a working demo goes through two documents and a download.

So this is a schema written for the purpose: three tables, two foreign keys,
deterministic rows, no external source and no licence to inherit. It is large
enough to need a join, a ``GROUP BY`` and a ``HAVING`` -- which is the shape of
question the demo asks -- and small enough to load and index in seconds.

**It is not a benchmark and must never be quoted as one.** Accuracy on a schema
chosen by the author of the questions measures nothing. `docs/ml/BENCHMARKS.md`
records Spider rows only.
"""
