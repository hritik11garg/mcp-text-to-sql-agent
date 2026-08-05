"""Entrypoint: ``python -m api``.

Reads the bind address from settings rather than taking it on the command
line. The default is loopback, and a flag would make "serve this to the whole
network" a thing somebody types once while debugging and leaves in a shell
history -- as configuration it is at least written down in the environment that
deployed it.

``reload`` is never enabled here. The development server is
``uvicorn api.app:create_app --factory --reload``, which keeps the
auto-reloader out of the module a container runs.
"""

from __future__ import annotations

import logging

import uvicorn

from api.app import create_app
from core.settings import Settings


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )
    settings = Settings.load()
    uvicorn.run(
        create_app(settings),
        host=settings.api.api_host,
        port=settings.api.api_port,
        # Uvicorn's access log writes the full path of every request. That is
        # useful and it is also where a question would end up if one were ever
        # put in a query string -- see docs/operations/SECURITY.md. Left on
        # because no endpoint takes user text in a URL today; the entry in
        # RISKS.md is what keeps that true.
        access_log=True,
        # Never echo the framework and version to every client. It is a free
        # fingerprint for anyone matching services against a CVE list.
        server_header=False,
    )


if __name__ == "__main__":
    main()
