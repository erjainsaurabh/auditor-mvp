# Backwards-compatibility shim.
# The canonical implementation has moved to flowprobe.agents.react_agent.
# This file exists so that any existing imports of `from flowprobe.agent import ...`
# continue to work without change.
from flowprobe.agents.react_agent import ReactAgent, run_test_condition

__all__ = ["ReactAgent", "run_test_condition"]
