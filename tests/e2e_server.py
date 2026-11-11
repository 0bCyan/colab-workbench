"""Only Playwright uses this application. It cannot provision Colab machines."""
import tempfile
from pathlib import Path

from backend.app import create_app
from tests.fake_colab import PROFILES, FakeColab

app = create_app(Path(tempfile.mkdtemp(prefix='workbench-e2e-')), driver=FakeColab(delay=1), account_profiles=PROFILES)
