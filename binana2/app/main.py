from __future__ import annotations
import asyncio, json, signal
from binana2.monitoring.health import health_snapshot
from .bootstrap import build_application

async def main() -> None:
    app=await build_application(); stop=asyncio.Event(); loop=asyncio.get_running_loop()
    for sig in (signal.SIGINT,signal.SIGTERM):
        try: loop.add_signal_handler(sig,stop.set)
        except NotImplementedError: pass
    snapshot=health_snapshot(app.db,app.state); print(json.dumps({"service":"binana2","environment":app.settings.environment,"health":snapshot.ok,"entries_paused":snapshot.entries_paused,"pause_reason":snapshot.pause_reason},sort_keys=True),flush=True)
    try: await stop.wait()
    finally: await app.close()

if __name__=="__main__": asyncio.run(main())
