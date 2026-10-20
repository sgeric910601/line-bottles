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



class FillScreen(unittest.TestCase):
    """外框改大小時 :99 跟著變，LINE 若不在放大狀態就停在舊大小，要再放大一次。"""

    def setUp(self):
        patcher = mock.patch.object(session, 'xprobe')
        self.xprobe = patcher.start()
        self.addCleanup(patcher.stop)
        session.MAXIMIZED.clear()

    def main(self, wid=1, **extra):
        return {**dict(id=wid, main=True, iconic=False), **extra}

    def test_new_main_window_is_maximized_once(self):
        session.fill_screen([self.main()], [1280, 800])
        session.fill_screen([self.main()], [1280, 800])
        self.xprobe.assert_called_once_with('maximize', 1)

    def test_maximizes_again_when_screen_size_changes(self):
        session.fill_screen([self.main()], [1280, 800])
        session.fill_screen([self.main()], [1600, 979])
        self.assertEqual(self.xprobe.call_count, 2)

    def test_skips_small_and_minimized_windows(self):
        session.fill_screen([dict(id=2, main=False, iconic=False),
                             self.main(3, iconic=True)], [1280, 800])
        self.xprobe.assert_not_called()

    def test_unknown_screen_size_still_maximizes_once(self):
        # 偵測結果缺了畫面大小（screen 是 None）時，新視窗仍要放大一次。
        session.fill_screen([self.main()], None)
        session.fill_screen([self.main()], None)
        self.xprobe.assert_called_once_with('maximize', 1)


if __name__ == '__main__':
    unittest.main()
