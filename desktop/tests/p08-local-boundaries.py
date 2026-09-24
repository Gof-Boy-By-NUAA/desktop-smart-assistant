"""Local P08 identity/persistence boundary probe; no WeChat or LLM traffic."""
import json, os, sys
from pathlib import Path
root=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(root))
out=root/'tmp'/'p05-p08-20260923'/'p08-local'
out.mkdir(parents=True,exist_ok=True)
os.environ['COW_DATA_DIR']=str(out/'profile')
from config import conf
conf()['agent_workspace']=str(out/'workspace')
conf()['conversation_persistence']=True
conf()['image_create_prefix']=[]
from channel.weixin.weixin_message import WeixinMessage
from channel.weixin.weixin_channel import WeixinChannel
from bridge.agent_bridge import AgentBridge
from agent.memory.conversation_store import get_conversation_store, ConversationStore
bridge=AgentBridge.__new__(AgentBridge)
channel=WeixinChannel()
channel.channel_type="weixin"
store=get_conversation_store()
results=[]
for user in ('p08-wechat-A','p08-wechat-B'):
    msg=WeixinMessage({'message_id':user,'from_user_id':user,'to_user_id':'p08-bot','item_list':[{'type':1,'text_item':{'text':'input '+user}}]})
    ctx=channel._compose_context(msg.ctype,msg.content,isgroup=False,msg=msg,no_need_at=True)
    sid=ctx['session_id']
    assert sid==user
    assert not ctx.get('session_owner_id')
    assert bridge._pre_persist_user_message(sid,ctx.content,ctx,False)
    # Deterministic assistant fixture, not a claim of real LLM generation.
    assert bridge._persist_messages(sid,[{'role':'assistant','content':'reply '+user}],channel_type=ctx['channel_type'])
    messages=store.load_messages(sid)
    assert len(messages)==2
    assert all(('p08-wechat-B' if user.endswith('A') else 'p08-wechat-A') not in json.dumps(m) for m in messages)
    results.append({'session':sid,'channel':ctx['channel_type'],'roles':[m['role'] for m in messages]})
store.append_messages('p08-desktop',[{'role':'user','content':'desktop'}],channel_type='web',owner_id='web:p08')
visible=store.list_sessions(channel_type='web',owner_id='web:p08')
assert [s['session_id'] for s in visible['sessions']]==['p08-desktop']
reopened=ConversationStore(store._db_path)
assert len(reopened.load_messages('p08-wechat-A'))==2
assert len(reopened.load_messages('p08-wechat-B'))==2
reopened.delete_session('p08-desktop',owner_id='web:p08')
assert len(reopened.load_messages('p08-wechat-A'))==2
result={'status':'PASSED_LOCAL_BOUNDARIES','identities':results,'desktop_visible_before_delete':visible['total'],'restart':True,'delete_does_not_touch_wechat':True,'test_doubles':['synthetic WeChat inbound messages','deterministic assistant response fixture; model not called'],'real_wechat_network':'NOT_RUN','product_semantics':'NEEDS_ESCALATION: no external-user to desktop-owner binding established'}
(out/'results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(result,ensure_ascii=False))
