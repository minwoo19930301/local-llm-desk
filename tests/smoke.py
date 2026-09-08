"""Hermetic smoke entry point: temporary HTTP state and the bundled MCP stub.

Both documented commands run the same checks:
    python3 -m unittest tests.smoke
    python3 tests/smoke.py

No installed models, user connector files, user crontab, or notifications are
required or modified. HTTP uses an ephemeral loopback port and stub inference;
MCP launches only tests/mcp_stub.py and writes logs into temporary directories.
"""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests import test_connectors, test_http


def load_tests(loader, tests, pattern):
    suite = unittest.TestSuite()
    suite.addTests(loader.loadTestsFromModule(test_http))
    suite.addTests(loader.loadTestsFromTestCase(test_connectors.MCPStdioTest))
    return suite


if __name__ == "__main__":
    unittest.main()
