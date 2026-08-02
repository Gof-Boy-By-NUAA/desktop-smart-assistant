from __future__ import annotations

import sys
from types import SimpleNamespace

from agent.tools.bash import background
from agent.tools.bash.bash import Bash


OWNER_A = "web:" + "a" * 32
OWNER_B = "web:" + "b" * 32


def _tool(owner_id: str, session_id: str, cwd: str) -> Bash:
    tool = Bash({"cwd": cwd, "safety_mode": False})
    tool.context = SimpleNamespace(
        conversation_owner_id=owner_id,
        session_id=session_id,
    )
    return tool


def test_background_jobs_are_owner_and_session_scoped(tmp_path):
    background.reset()
    owner = _tool(OWNER_A, "session-a", str(tmp_path))
    attacker = _tool(OWNER_B, "session-b", str(tmp_path))
    command = f'"{sys.executable}" -c "import time; time.sleep(30)"'
    started = owner.execute({"command": command, "run_in_background": True})
    assert started.status == "success"
    job_id = started.result["bash_id"]

    assert attacker.execute({"bash_id": job_id}).status == "error"
    assert attacker.execute({"bash_id": job_id, "kill": True}).status == "error"
    attacker_message = str(attacker.execute({"bash_id": job_id}).result)
    assert "none are being tracked" in attacker_message
    assert "Currently tracked" not in attacker_message
    assert background.read(job_id, owner_id=OWNER_A, session_id="session-a")["running"]

    killed = owner.execute({"bash_id": job_id, "kill": True})
    assert killed.status == "success"
    background.reset()
