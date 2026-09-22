"""为完整制品测试写入真实 ConversationStore 数据，或注入可恢复的 SQLite 故障。"""
import sys
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from agent.memory.conversation_store import ConversationStore

db = Path(sys.argv[1]).resolve()
if not db.is_relative_to(ROOT / "tmp") or not any(part.startswith("p03-p04-") for part in db.parts):
    raise ValueError("fixture may only access this task's owned test database")

mode = sys.argv[2]
if mode == "seed":
    store = ConversationStore(db)
    store.claim_session("p04-empty", "web:legacy")
    store.rename_session("p04-empty", "P04_EMPTY", owner_id="web:legacy")
    store.append_messages(
        "p04-existing",
        [{"role": "user", "content": "P04_TEST_INPUT"},
         {"role": "assistant", "content": "# P04_HISTORY_OK\n\n**持久化历史**"}],
        channel_type="web", owner_id="web:legacy",
    )
    store.rename_session("p04-existing", "P04_EXISTING", owner_id="web:legacy")
    if len(sys.argv) > 3:
        store.append_messages(
            sys.argv[3], [{"role": "user", "content": "P04_DRAFT_INPUT"},
                          {"role": "assistant", "content": "# P04_DRAFT_PERSISTED"}],
            channel_type="web", owner_id="web:legacy",
        )
elif mode in {"break", "restore"}:
    with sqlite3.connect(db) as connection:
        if mode == "break":
            connection.execute("ALTER TABLE messages RENAME TO p04_fault_messages")
        else:
            connection.execute("ALTER TABLE p04_fault_messages RENAME TO messages")
else:
    raise ValueError("unknown fixture mode")
print(mode + ": real SQLite fixture operation completed")
