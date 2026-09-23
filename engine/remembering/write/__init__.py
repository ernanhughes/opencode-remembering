# Bundled remembering engine (opencode-remembering product code).
# Ported from ernanhughes/project-memory (Apache-2.0); behavior preserved.
"""Explicit memory actions (Stage 9): attributed append-only writes.

Automatic capture records what happened; explicit memory records what a
caller explicitly asked the system to remember, correct, supersede, or
retract. Every write is an event, never an edit to the past, and
permission to store a memory is never permission for that memory to
steer behavior (Stage 5 remains the final trust authority).
"""

WRITE_ENGINE_VERSION = "write-engine-v0.1"
WRITE_POLICY_SCHEMA = "write-policy-v0.1"
WRITE_GATE_VERSION = "write-gate-v0.1"
MEMORY_ACTION_SCHEMA = "memory-action-v0.1"
EXPLICIT_RECORD_SCHEMA = "explicit-memory-record-v0.1"
WRITE_STORE_VERSION = "memory-action-store-v0.1"
RECORD_PROJECTION_VERSION = "memory-record-projection-v0.1"
RELATION_VERSION = "memory-relation-v0.1"
WRITE_EVAL_VERSION = "write-eval-v0.1"
