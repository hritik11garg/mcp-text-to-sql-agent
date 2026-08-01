"""The four MCP servers, each an independent process.

Four rather than one because each is a capability with genuinely different
properties -- `validate_sql` is side-effect-free and safe to call in a loop,
`execute_sql` costs a real query and is not. Merging them would force the
strictest policy onto every operation, or the most liberal onto the one that
cannot afford it. See docs/architecture/MCP.md section 1.

Each server is a **thin adapter** over a component that was built and tested
without any knowledge of MCP. That ordering is deliberate: every bound worth
having -- `k` clamped, `LIMIT` injected into the AST, identifiers resolved
against the catalog -- lives in the component, because another caller reaching
it directly must get the same treatment. A limit enforced only in the server
would be a limit that only applies over the protocol.

Run one with ``python -m mcp_servers.<name>``.
"""
