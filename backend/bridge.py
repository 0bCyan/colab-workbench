"""Run the existing account launcher with SSH auto-provisioning disabled.

This file runs under the launcher's own Python, preserving its account scopes.
Neither a reconnect nor an SSH failure may silently create a new Colab VM.
"""
import sys
from pathlib import Path

from colab_cli.commands import ssh


def deny_creation(*args, **kwargs):
    raise SystemExit("工作台隧道不会创建机器。请先通过工作台启动会话。")


ssh._auto_create_session = deny_creation
launcher, session, identity = sys.argv[1:]
sys.argv = [launcher, "ssh", "--proxy-mode", "-s", session, "-i", identity]
# runpy.run_path temporarily replaces argv[0] with launch.py, which would lose
# the account number. Execute the trusted installed launcher with argv intact.
source = Path(launcher).resolve()
exec(compile(source.read_bytes(), str(source), "exec"), {"__name__": "__main__", "__file__": str(source)})
