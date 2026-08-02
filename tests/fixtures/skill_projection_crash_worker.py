"""在技能投影替换后立即终止进程，用于验证持久化恢复。"""

from __future__ import annotations

import os
import sys
from dataclasses import replace
from pathlib import Path

# 直接执行夹具脚本时，显式加入仓库根目录以加载被测包。
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agent.skills.governance import (
    GovernedSkillRepository,
    GovernedSkillService,
    SkillStatus,
)


def main() -> None:
    skills_dir = Path(sys.argv[1])
    database_path = Path(sys.argv[2])
    tenant_id = sys.argv[3]
    skill_id = sys.argv[4]
    version = int(sys.argv[5])
    repository = GovernedSkillRepository(database_path)
    service = GovernedSkillService(repository, skills_dir, tenant_id)
    with repository.transaction() as connection:
        candidate = repository.get_version(
            connection, tenant_id, skill_id, version
        )
        active = repository.get_active_by_name(
            connection, tenant_id, candidate.name
        )
        _, previous_bytes, previous_existed = service._snapshot_projection(
            active, candidate
        )
        projected = replace(candidate, status=SkillStatus.ACTIVE)
        service._write_projection_journal(
            projected, previous_bytes, previous_existed, "publish"
        )
        service._project_record(projected)
        os._exit(73)


if __name__ == "__main__":
    main()
