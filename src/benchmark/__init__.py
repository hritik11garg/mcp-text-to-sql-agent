"""Turning a published benchmark into something this project can be measured on.

Acquisition, SQLite to PostgreSQL conversion, verification of that conversion,
and the database-level splits every number is reported against. Offline tools:
nothing here is imported by a server or reachable from a request.

The package is named ``benchmark`` rather than ``datasets`` on purpose. This is
a ``src`` layout, so a top-level ``datasets`` package would shadow the
HuggingFace library of that name for every import in the process --
``transformers`` imports it optionally, and the failure would be an unrelated
library breaking at a distance.
"""
