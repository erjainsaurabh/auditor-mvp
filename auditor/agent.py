# Backwards-compatibility shim.
# The canonical implementation has moved to auditor.agents.react_agent.
# This file exists so that any existing imports of `from auditor.agent import ...`
# continue to work without change.
from auditor.agents.react_agent import ReactAgent, run_test_condition

__all__ = ["ReactAgent", "run_test_condition"]
