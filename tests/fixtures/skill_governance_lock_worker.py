"""在独立进程中持有或探测技能治理跨进程锁。"""

from __future__ import annotations

import sys
import time
from pathlib import Path

# 直接执行夹具脚本时，显式加入仓库根目录以加载被测包。
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agent.skills.governance import GovernedSkillRepository, GovernedSkillService


def main() -> None:
    mode = sys.argv[1]
    skills_dir = Path(sys.argv[2])
    database_path = Path(sys.argv[3])
    tenant_id = sys.argv[4]
    marker = Path(sys.argv[5])
    release = Path(sys.argv[6]) if len(sys.argv) > 6 else None
    repository = GovernedSkillRepository(database_path)
    try:
        service = GovernedSkillService(repository, skills_dir, tenant_id)
        if mode == "probe":
            marker.write_text("acquired", encoding="utf-8")
            return
        if mode != "hold" or release is None:
            raise ValueError("未知的锁测试模式")
        with service._projection_lock:
            marker.write_text("held", encoding="utf-8")
            deadline = time.monotonic() + 15.0
            while not release.exists():
                if time.monotonic() >= deadline:
                    raise TimeoutError("等待释放标记超时")
                time.sleep(0.02)
    finally:
        repository.close()


if __name__ == "__main__":
    main()
