"""Only Playwright uses this application. It cannot provision Colab machines."""
import tempfile
from pathlib import Path

from backend.app import create_app
from tests.fake_colab import PROFILES, FakeColab
from tests.fake_llm import FakeLLM

video = FakeColab(delay=1)
app = create_app(Path(tempfile.mkdtemp(prefix='workbench-e2e-')), driver=video,
                 llm_driver=FakeLLM(active=video.active), account_profiles=PROFILES)
