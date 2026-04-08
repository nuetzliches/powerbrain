"""Maintenance jobs for the pb-worker container.

Each job is a coroutine ``run(ctx)`` where ``ctx`` is a ``WorkerContext``
holding shared resources (DB pool, HTTP client, env config). Jobs return
a JSON-serializable summary that the scheduler logs.
"""
