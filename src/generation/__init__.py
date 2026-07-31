"""SQL generation: question plus retrieved schema, into a candidate query.

Two modules, split along the line that matters for measurement:

    prompts.py    pure -- builds messages, renders schema context
    generator.py  calls the LLM port, cleans the response

The prompt format is the highest-leverage variable in generation quality and is
the thing Stage 2 measures. Keeping it in pure functions means it can be
changed and compared without a provider, a key, or a network call.
"""
