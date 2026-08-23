
import asyncio
import json

from astrbot_lark_kit.events import EventStream

SAMPLE = {"type":"im.message.receive_v1","message_id":"om_1","sender_id":"ou_1","sender_name":"a",
          "sender_type":"user","chat_id":"oc_c","chat_type":"group","message_type":"text",
          "content":"hi","create_time":"1"}

def make_fake(tmp_path):
    script = tmp_path / "fake.py"
    dup = [json.dumps(SAMPLE)]*3
    body = "\n".join(f"print({json.dumps(line)})" for line in dup)
    script.write_text("#!/usr/bin/env python3\nimport sys\n"+body+"\n")
    script.chmod(0o755)
    return script

async def body_fn(tmp_path):
    st = EventStream(binary=make_fake(tmp_path), max_backoff_s=0.01)
    agen = st.stream()
    got = []
    async for msg in agen:
        got.append(msg.message_id)
        break
    print("GOT", got, flush=True)
    print("closing...", flush=True)
    await agen.aclose()
    print("closed proc:", st._proc, flush=True)

def test_dedup_repro(tmp_path):
    asyncio.run(asyncio.wait_for(body_fn(tmp_path), timeout=15))
