import asyncio
import tempfile
import time
import unittest

from freqtrade.binana.user_stream import PrivateUserStream


class IdleStream(PrivateUserStream):
    async def _session(self):
        self._publish(ok=True, subscribed=True, last_verified_at=time.time())
        await asyncio.sleep(3600)


class PrivateStreamShutdown(unittest.TestCase):
    def test_shutdown_cancels_idle_receive_and_allows_clean_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            stream=IdleStream('test-key','test-secret',scope='test-scope',runtime=directory)
            for _ in range(2):
                stream.start()
                deadline=time.monotonic()+2
                while not stream.health()['subscribed'] and time.monotonic()<deadline:
                    time.sleep(0.01)
                self.assertTrue(stream.health()['subscribed'])
                stream.stop()
                self.assertFalse(stream._thread.is_alive())
                self.assertFalse(stream.health()['subscribed'])


if __name__=='__main__':
    unittest.main()
