"""Bobrik mascot chat — internal prototype only.

This module implements a deliberately narrow, local-only slice of the
``bobrik-agent-plan.md`` chat contract: anonymous sessions, idempotent runs,
durable event journal (SSE replay), explicit cancellation and a separate
worker process that owns the single paid LLM call.

Every route is hard-disabled with ``404`` unless ``mascot_enabled`` is true
and the environment is not production. The flag defaults to false, so nothing
is exposed until the composition root enables it explicitly.
"""
