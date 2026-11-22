"""The public pytest package backed by vfy's independent runner facade.

The independent runner adds this directory to ``PYTHONPATH`` only when no
pytest distribution is importable, so a spawned worker that imports a test
module resolves ``import pytest`` to the same facade as the runner.
"""

from vfy.independent.facade import install_pytest_facade

install_pytest_facade()
