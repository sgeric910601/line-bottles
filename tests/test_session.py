"""殘留 Wine 行程清理邏輯的單元測試。

實機上這段只有在「上一輪沒收乾淨」時才會跑到，不方便重現，所以用替身把
bottle_pids 與 os.kill 換掉，驗證訊號的升級順序與放棄條件。
"""
import signal
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'lib'))

import session  # noqa: E402


class ClearStale(unittest.TestCase):
    def setUp(self):
        # 清理過程會寫日誌與送桌面通知，測試時都不要真的發出去。
        for name in ('logging', 'notify'):
            patcher = mock.patch.object(session, name)
            patcher.start()
            self.addCleanup(patcher.stop)

    @staticmethod
    def run_with(pid_sequence, wait_results=None):
        """pid_sequence 是 bottle_pids 每次被呼叫的回傳值。"""
        with mock.patch.object(session, 'bottle_pids', side_effect=pid_sequence), \
             mock.patch.object(session.os, 'kill') as kill:
            if wait_results is None:
                session.clear_stale()
            else:
                with mock.patch.object(session, 'wait_for', side_effect=wait_results):
                    session.clear_stale()
            return kill

    def test_does_nothing_when_nothing_is_left(self):
        kill = self.run_with([[]])
        kill.assert_not_called()

    def test_sigterm_alone_is_enough(self):
        # 第一次問有兩個，wait_for 內部再問一次已經空了。
        kill = self.run_with([[11, 22], []])
        self.assertEqual([c.args[1] for c in kill.call_args_list],
                         [signal.SIGTERM, signal.SIGTERM])

    def test_escalates_to_sigkill_when_sigterm_is_ignored(self):
        # 死鎖的 wineserver 就是這樣：SIGTERM 叫不動，只有 SIGKILL 有效。
        kill = self.run_with([[11], [11], [11]], wait_results=[False, True])
        self.assertEqual([c.args[1] for c in kill.call_args_list],
                         [signal.SIGTERM, signal.SIGKILL])

    def test_raises_when_even_sigkill_fails(self):
        with self.assertRaises(RuntimeError):
            self.run_with([[11], [11], [11]], wait_results=[False, False])

    def test_survives_a_process_that_already_exited(self):
        with mock.patch.object(session, 'bottle_pids', side_effect=[[11], []]), \
             mock.patch.object(session.os, 'kill', side_effect=ProcessLookupError):
            session.clear_stale()  # 不應該往外丟例外


if __name__ == '__main__':
    unittest.main()
