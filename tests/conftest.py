"""Load bot.py with a throwaway database so the helpers can be tested without Discord."""
import importlib
import os
import sys
import tempfile

import pytest

_tmp = tempfile.mkdtemp(prefix="aiblock-test-")
os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ["DB_PATH"] = os.path.join(_tmp, "synthid.db")
os.environ["REMOVAL_LOG_FILE"] = os.path.join(_tmp, "removals.log")
os.environ.setdefault("LOG_LEVEL", "WARNING")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture(scope="session")
def bot():
    return importlib.import_module("bot")
