"""
Result Offloading
=================

Keeps big tool results out of the context window.

This agent reads whole source files, whole diffs, and a workspace summary that
carries a repository's entire contribution rules -- agno-os is 6KB of that, agno
is 29KB. Left inline, every one of those is re-sent on every later turn of the
thread, so a long conversation pays for the same file over and over.

Offloading writes anything past the threshold to the database and leaves a short
envelope behind -- a preview, the size, and a `result_id` -- plus the tools
`search_result` and `read_result` so the agent can go back for the parts it
actually needs.
"""

from agno.offload import ResultStore

# A week: long enough that a thread picked up the next morning still resolves
# its result ids, short enough that checkouts' worth of file content does not
# accumulate forever.
RESULT_TTL_SECONDS = 7 * 24 * 60 * 60

result_store = ResultStore(ttl_seconds=RESULT_TTL_SECONDS)
