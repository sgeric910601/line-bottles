"""LINE 外框視窗的單元測試。

外框本身需要真的主機顯示器與視窗管理員，這裡只測不碰 X 的決策邏輯（參數、訊息、
焦點、大小同步、結束碼）、Xlib 結構的版面，以及把 X 換成替身後的事件處理；
實機行為在手動驗收清單上。
"""
import ctypes
import os
import sys
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'lib'))

import frame  # noqa: E402
import x11  # noqa: E402

ATOMS = {'WM_PROTOCOLS': 101, 'WM_DELETE_WINDOW': 102, 'WM_TAKE_FOCUS': 103}
NOTIFY_NORMAL, NOTIFY_GRAB, NOTIFY_UNGRAB, NOTIFY_WHILE_GRABBED = 0, 1, 2, 3


class XephyrArgv(unittest.TestCase):
    def test_embeds_into_frame_by_decimal_id(self):
        self.assertEqual(frame.xephyr_argv(':99', 0x2a00003),
                         ['/usr/bin/Xephyr', ':99', '-parent', str(0x2a00003),
                          '-ac', '-br', '-noreset', '-no-host-grab'])

    def test_size_and_title_come_from_the_frame(self):
        # 嵌入後大小取自外框；-title 會在 Ctrl+Shift 抓取時寫到子視窗上。
        argv = frame.xephyr_argv(':99', 1)
        for flag in ('-screen', '-resizeable', '-title'):
            self.assertNotIn(flag, argv)


class SizeHints(unittest.TestCase):
    """外框固定大小：WM_NORMAL_HINTS 的最小＝最大，WM 就不給拖曳也不給最大化。"""

    def test_fixed_size_sets_min_and_max(self):
        hints = frame.size_hints((1280, 800))
        self.assertEqual(len(hints), 18)
        self.assertEqual(hints[0], frame.P_MIN_SIZE | frame.P_MAX_SIZE)
        self.assertEqual(hints[5:9], [1280, 800, 1280, 800])

    def test_frame_starts_at_fixed_size(self):
        self.assertEqual(frame.INITIAL_SIZE, frame.FIXED_SIZE)


class ClientMessageAction(unittest.TestCase):
    def action(self, message_type, data0):
        return frame.client_message_action(message_type, data0, ATOMS)

    def test_close_becomes_minimize(self):
        self.assertEqual(self.action(101, 102), 'minimize')

    def test_take_focus(self):
        self.assertEqual(self.action(101, 103), 'take-focus')

    def test_other_protocol_is_ignored(self):
        self.assertIsNone(self.action(101, 999))

    def test_other_message_type_is_ignored(self):
        self.assertIsNone(self.action(555, 102))


class ShouldRedirectFocus(unittest.TestCase):
    FRAME, CHILD = 0x100, 0x200

    def test_moves_focus_from_frame_to_child(self):
        self.assertTrue(frame.should_redirect_focus(self.FRAME, self.CHILD, self.FRAME,
                                                    NOTIFY_NORMAL))

    def test_waits_until_xephyr_child_exists(self):
        self.assertFalse(frame.should_redirect_focus(self.FRAME, 0, self.FRAME, NOTIFY_NORMAL))

    def test_nothing_to_do_when_child_already_focused(self):
        self.assertFalse(frame.should_redirect_focus(self.FRAME, self.CHILD, self.CHILD,
                                                     NOTIFY_NORMAL))

    def test_leaves_grabs_alone(self):
        for mode in (NOTIFY_GRAB, NOTIFY_UNGRAB, NOTIFY_WHILE_GRABBED):
            self.assertFalse(frame.should_redirect_focus(self.FRAME, self.CHILD, self.FRAME,
                                                         mode))


class NextResize(unittest.TestCase):
    def test_nothing_pending(self):
        self.assertEqual(frame.next_resize(None, (800, 600), 0, 10, 0.2), (None, None))

    def test_already_applied(self):
        self.assertEqual(frame.next_resize((800, 600), (800, 600), 0, 10, 0.2), (None, None))

    def test_waits_while_still_changing(self):
        size, wait = frame.next_resize((900, 700), (800, 600), 10.0, 10.05, 0.2)
        self.assertIsNone(size)
        self.assertAlmostEqual(wait, 0.15)

    def test_applies_after_settling(self):
        self.assertEqual(frame.next_resize((900, 700), (800, 600), 10.0, 10.2, 0.2),
                         ((900, 700), None))


class ExitResult(unittest.TestCase):
    def test_normal_exit_code_passes_through(self):
        # 登出時主機顯示器消失，Xephyr 以 1 結束，服務設定把 1 當成正常。
        self.assertEqual(frame.exit_result(1, 600), (1, None))

    def test_clean_exit(self):
        self.assertEqual(frame.exit_result(0, 0.5), (0, None))

    def test_signal_is_reported_separately(self):
        # Popen 用負數表示被訊號結束；外框要用同一個訊號結束自己，systemd 才看得到原樣。
        self.assertEqual(frame.exit_result(-15, 600), (None, 15))
        self.assertEqual(frame.exit_result(-9, 0.5), (None, 9))

    def test_failure_right_after_start_is_not_mistaken_for_logout(self):
        # :99 被佔用或查不到外框時 Xephyr 也以 1 結束，但那是故障，不能被當成正常收場。
        self.assertEqual(frame.exit_result(1, 0.5), (frame.STARTUP_FAILED, None))
        self.assertNotEqual(frame.STARTUP_FAILED, 1)


class AtomConstants(unittest.TestCase):
    """Xatom.h 的預先定義原子；寫錯型別 WM 會默默忽略那個屬性。"""

    def test_predefined_atoms(self):
        self.assertEqual((frame.XA_ATOM, frame.XA_CARDINAL, frame.XA_STRING,
                          frame.XA_WM_HINTS, frame.XA_WM_SIZE_HINTS), (4, 6, 31, 35, 41))


class Main(unittest.TestCase):
    def test_reraises_xephyr_signal_even_when_handler_cannot_be_reset(self):
        # SIGKILL 不能設處理器，signal.signal 會丟 OSError；照樣要用同一個訊號結束自己。
        with mock.patch.dict(os.environ, {'DISPLAY': ':1'}), \
                mock.patch.object(frame, 'Frame') as frame_class, \
                mock.patch.object(frame.signal, 'signal', side_effect=OSError(22, 'EINVAL')), \
                mock.patch.object(frame.os, 'kill') as kill, \
                mock.patch.object(frame.sys, 'exit'):
            frame_class.return_value.run.return_value = (None, 9)
            frame.main([])
        kill.assert_called_once_with(os.getpid(), 9)

    def run_main(self, env, run=None, error=None):
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(frame, 'Frame') as frame_class, \
                mock.patch.object(frame.sys, 'exit') as exit_:
            if error:
                frame_class.side_effect = error
            else:
                frame_class.return_value.run.return_value = run
            frame.main([])
        return exit_

    def test_exit_code_passes_through(self):
        self.run_main({'DISPLAY': ':1'}, run=(1, None)).assert_called_once_with(1)

    def test_errors_never_exit_with_one(self):
        # 1 被服務設定當成正常；外框自己出錯（連不上顯示器等）要用別的碼。
        exit_ = self.run_main({'DISPLAY': ':1'}, error=RuntimeError('無法連上顯示器 :1'))
        exit_.assert_called_once_with(frame.FRAME_FAILED)

    def test_missing_display_is_a_failure(self):
        self.run_main({}).assert_called_once_with(frame.FRAME_FAILED)


class AnnounceAction(unittest.TestCase):
    """Xephyr 改大小只發 RRScreenChangeNotify；Wine 要 CRTC／輸出變更才會更新螢幕大小。"""

    def test_announces_once_nested_screen_has_new_size(self):
        self.assertEqual(frame.announce_action((1600, 979), (1600, 979), 0), 'announce')

    def test_waits_while_xephyr_still_resizing(self):
        self.assertEqual(frame.announce_action((1280, 800), (1600, 979), 0), 'wait')

    def test_gives_up_after_too_many_tries(self):
        self.assertEqual(frame.announce_action((1280, 800), (1600, 979), 9, max_tries=10),
                         'give-up')


class NextTimeout(unittest.TestCase):
    def test_nothing_pending_uses_ceiling(self):
        self.assertEqual(frame.next_timeout(10.0, [None, None], 1.0), 1.0)

    def test_earliest_deadline_wins(self):
        self.assertAlmostEqual(frame.next_timeout(10.0, [10.3, None, 10.1], 1.0), 0.1)

    def test_overdue_is_zero(self):
        self.assertEqual(frame.next_timeout(10.0, [9.0], 1.0), 0)


def event(kind, part, **fields):
    e = x11.Event()
    getattr(e, part).type = kind
    for name, value in fields.items():
        setattr(getattr(e, part), name, value)
    return e


class FrameEvents(unittest.TestCase):
    """handle() 只靠事件欄位與 X 呼叫；把 X 換成假的就能測子視窗追蹤與大小同步。"""
    FRAME, CHILD = 0x100, 0x200

    def setUp(self):
        self.x = mock.patch.object(frame, 'X').start()
        self.addCleanup(mock.patch.stopall)
        f = frame.Frame.__new__(frame.Frame)
        f.d, f.window, f.screen, f.atoms = 1, self.FRAME, 0, ATOMS
        f.child, f.child_mapped = 0, False
        # 外框啟動時讀到的大小；Xephyr 的子視窗可能用另一個大小建立。
        f.applied, f.pending, f.changed_at = (1000, 700), None, 0.0
        f.announce_at, f.announce_size, f.announce_tries = None, None, 0
        f.xephyr, f.started, f.stopping = None, 0.0, False
        self.f = f

    def test_first_child_is_the_xephyr_screen(self):
        self.f.handle(event(frame.CREATE_NOTIFY, 'create', parent=self.FRAME,
                            window=self.CHILD, width=1280, height=800))
        self.assertEqual(self.f.child, self.CHILD)

    def test_resize_before_child_exists_is_not_lost(self):
        # 外框在 Xephyr 讀完大小之後才被 mutter 調整：子視窗用舊大小建立，之後要補調。
        self.f.handle(event(frame.CONFIGURE_NOTIFY, 'configure', window=self.FRAME,
                            width=1600, height=900))
        self.f.handle(event(frame.CREATE_NOTIFY, 'create', parent=self.FRAME,
                            window=self.CHILD, width=1280, height=800))
        self.assertEqual(self.f.applied, (1280, 800))
        size, _ = frame.next_resize(self.f.pending, self.f.applied, 0, 10)
        self.assertEqual(size, (1600, 900))

    def test_resize_without_child_changes_nothing(self):
        self.f.resize((1600, 900))
        self.assertEqual(self.f.applied, (1000, 700))
        self.x.XResizeWindow.assert_not_called()

    def test_windows_of_other_parents_are_ignored(self):
        self.f.handle(event(frame.CREATE_NOTIFY, 'create', parent=0x999, window=self.CHILD))
        self.assertEqual(self.f.child, 0)

    def test_destroy_clears_child(self):
        self.f.child, self.f.child_mapped = self.CHILD, True
        self.f.handle(event(frame.DESTROY_NOTIFY, 'destroy', window=self.CHILD))
        self.assertEqual((self.f.child, self.f.child_mapped), (0, False))

    def test_map_and_unmap_track_child(self):
        self.f.child = self.CHILD
        self.f.handle(event(frame.MAP_NOTIFY, 'map', window=self.CHILD))
        self.assertTrue(self.f.child_mapped)
        self.f.handle(event(frame.UNMAP_NOTIFY, 'map', window=self.CHILD))
        self.assertFalse(self.f.child_mapped)

    def test_resize_schedules_announcement(self):
        self.f.child = self.CHILD
        self.f.resize((1600, 979))
        self.assertEqual(self.f.announce_size, (1600, 979))
        self.assertIsNotNone(self.f.announce_at)

    def test_cleanup_without_xephyr(self):
        # map() 或 spawn() 之前就出錯時 xephyr 還是 None，收尾不能再出錯。
        self.f.xephyr = None
        self.f.cleanup()
        self.x.XDestroyWindow.assert_called_once_with(1, self.FRAME)
        self.x.XCloseDisplay.assert_called_once_with(1)

    def test_close_request_minimizes(self):
        e = event(frame.CLIENT_MESSAGE, 'client', window=self.FRAME, message_type=101)
        e.client.data.l[0] = 102
        self.f.handle(e)
        self.x.XIconifyWindow.assert_called_once_with(1, self.FRAME, 0)


class FrameClass(unittest.TestCase):
    def test_matches_frame_instance(self):
        self.assertTrue(x11.is_frame_class(b'line-bottles\0LINE\0'))

    def test_ignores_plain_xephyr(self):
        self.assertFalse(x11.is_frame_class(b'Xephyr\0Xephyr\0'))

    def test_test_instance_is_not_the_real_frame(self):
        self.assertFalse(x11.is_frame_class(b'line-bottles-test\0LINE\0'))
        self.assertTrue(x11.is_frame_class(b'line-bottles-test\0LINE\0', 'line-bottles-test'))

    def test_prefix_is_not_enough(self):
        self.assertFalse(x11.is_frame_class(b'line-bottles2\0LINE\0'))

    def test_missing_property(self):
        self.assertFalse(x11.is_frame_class(None))
        self.assertFalse(x11.is_frame_class(b''))


@unittest.skipUnless(ctypes.sizeof(ctypes.c_long) == 8, '版面數字只對 x86_64 的 LP64 成立')
class EventLayout(unittest.TestCase):
    """結構錯位不會報錯，只會讀到錯的欄位，所以把 Xlib.h 的版面釘死。"""

    def test_union_matches_xevent(self):
        self.assertEqual(ctypes.sizeof(x11.Event), 192)

    def test_configure_event(self):
        self.assertEqual(ctypes.sizeof(x11.Configure), 88)
        self.assertEqual(x11.Configure.window.offset, 40)
        self.assertEqual(x11.Configure.width.offset, 56)
        self.assertEqual(x11.Configure.height.offset, 60)

    def test_focus_change_event(self):
        self.assertEqual(ctypes.sizeof(x11.FocusChange), 48)
        self.assertEqual(x11.FocusChange.window.offset, 32)
        self.assertEqual(x11.FocusChange.mode.offset, 40)
        self.assertEqual(x11.FocusChange.detail.offset, 44)

    def test_substructure_events(self):
        self.assertEqual(x11.CreateWindow.parent.offset, 32)
        self.assertEqual(x11.CreateWindow.window.offset, 40)
        self.assertEqual(x11.Map.window.offset, 40)
        self.assertEqual(x11.DestroyWindow.window.offset, 40)


if __name__ == '__main__':
    unittest.main()
