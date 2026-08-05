"""The composition root: where process-lifetime dependencies are built.

Two entrypoints now need the same wiring -- the four MCP servers and the HTTP
API -- and neither may import it from the other. An entrypoint that reached
into a sibling entrypoint's package for its dependency graph would make the API
depend on the MCP layer to open a database connection, which is backwards: they
are peers, both adapters over the same components.

So the graph lives here, in a package that depends on everything and that
nothing depends on. This is the one place in the source tree allowed to know
about all the layers at once, which is precisely what a composition root is
for: constructing the object graph is the job, and every other module receives
what it needs rather than building it.
"""

from composition.resources import Resources, assert_read_only

__all__ = ["Resources", "assert_read_only"]
