"""taskDataReadiness: task identification + a deterministic data-readiness preflight (proof of concept).

Stage 1 (task_router.py) answers "which business task is this?".
Stage 2 (engine.py + checks/) answers "is the data this task needs available, current, traceable and authorised?".
Neither stage is an access-control system: the service that finally returns the data must enforce authorisation itself.
"""

__version__ = "0.1.0"
