"""Unit tests for the pure decision logic of the clipboard bridge.

The daemon itself needs two live X servers, so only the format choice, the
loop guard and the Wine clipboard window detection are covered here;
end-to-end behaviour is on the manual checklist.
"""
import ctypes
import hashlib
import io
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'lib'))

import clipboard  # noqa: E402


class PickTarget(unittest.TestCase):
    def test_prefers_png_over_text(self):
        self.assertEqual(
            clipboard.pick_target(['STRING', 'UTF8_STRING', 'image/png', 'TIMESTAMP']),
            'image/png')

    def test_order_in_source_list_is_irrelevant(self):
        self.assertEqual(clipboard.pick_target(['image/png', 'UTF8_STRING']),
                         clipboard.pick_target(['UTF8_STRING', 'image/png']))

    def test_falls_back_to_bmp_when_wine_offers_no_png(self):
        # winex11.drv 對 CF_DIB 可能只給 image/bmp。
        self.assertEqual(clipboard.pick_target(['TARGETS', 'image/bmp']), 'image/bmp')

    def test_prefers_utf8_over_latin1_string(self):
        self.assertEqual(clipboard.pick_target(['STRING', 'UTF8_STRING']), 'UTF8_STRING')

    def test_returns_none_when_nothing_usable(self):
        self.assertIsNone(clipboard.pick_target(['TARGETS', 'TIMESTAMP', 'MULTIPLE']))

    def test_returns_none_for_empty_list(self):
        self.assertIsNone(clipboard.pick_target([]))


class Accept(unittest.TestCase):
    def test_accepts_new_content(self):
        digest, reason = clipboard.accept(b'hello', None)
        self.assertEqual(reason, 'ok')
        self.assertEqual(digest, hashlib.sha256(b'hello').hexdigest())

    def test_rejects_our_own_echo(self):
        digest = hashlib.sha256(b'hello').hexdigest()
        self.assertEqual(clipboard.accept(b'hello', digest), (None, 'echo'))

    def test_accepts_when_content_changed(self):
        stale = hashlib.sha256(b'old').hexdigest()
        self.assertEqual(clipboard.accept(b'new', stale)[1], 'ok')

    def test_rejects_empty(self):
        self.assertEqual(clipboard.accept(b'', None), (None, 'empty'))

    def test_accepts_exactly_at_the_limit(self):
        self.assertEqual(clipboard.accept(b'x' * clipboard.LIMIT, None)[1], 'ok')

    def test_rejects_one_byte_over_the_limit(self):
        self.assertEqual(clipboard.accept(b'x' * (clipboard.LIMIT + 1), None),
                         (None, 'too-large'))


STRUCTURE_NOTIFY_MASK = 1 << 17

# Wine 11.0 在 clipboard_init 建立的 import_window 長相（2026-10-20 在 :99 實測）。
WINE_IMPORT_WINDOW = dict(width=1, height=1, x=0, y=0, border=0, input_output=True,
                          mapped=False, override_redirect=False,
                          event_mask=clipboard.PROPERTY_CHANGE_MASK, property_count=0)


def wine_window(**changes):
    """以 Wine 的 import_window 為底，逐項改掉來測反例。"""
    return clipboard.WindowInfo(**{**WINE_IMPORT_WINDOW, **changes})


class ReadCapped(unittest.TestCase):
    """上限要邊讀邊檢查：先整份讀進記憶體再比，來源就能用超大內容撐爆橋接。"""

    def test_keeps_content_exactly_at_the_limit(self):
        self.assertEqual(clipboard.read_capped(io.BytesIO(b'x' * 10), 10, chunk=3),
                         (b'x' * 10, False))

    def test_stops_one_byte_over_the_limit(self):
        self.assertEqual(clipboard.read_capped(io.BytesIO(b'x' * 11), 10, chunk=3),
                         (b'', True))

    def test_stops_without_reading_the_rest(self):
        stream = io.BytesIO(b'x' * 1000)
        clipboard.read_capped(stream, 10, chunk=4)
        self.assertLessEqual(stream.tell(), 12)  # 超過上限的那一塊就停，不再往下讀

    def test_empty_stream(self):
        self.assertEqual(clipboard.read_capped(io.BytesIO(b''), 10), (b'', False))

    def test_joins_many_chunks_in_order(self):
        data = bytes(range(256)) * 4
        self.assertEqual(clipboard.read_capped(io.BytesIO(data), len(data), chunk=7),
                         (data, False))


class WineClipboardWindow(unittest.TestCase):
    def test_matches_wine_import_window(self):
        self.assertTrue(clipboard.is_wine_clipboard_window(wine_window()))

    def test_rejects_per_thread_window_with_structure_notify_only(self):
        # 每條 Wine 連線都有一個只選 StructureNotify 的 1x1 視窗，不能誤認。
        info = wine_window(event_mask=STRUCTURE_NOTIFY_MASK)
        self.assertFalse(clipboard.is_wine_clipboard_window(info))

    def test_rejects_extra_event_bits(self):
        mask = clipboard.PROPERTY_CHANGE_MASK | STRUCTURE_NOTIFY_MASK
        self.assertFalse(clipboard.is_wine_clipboard_window(wine_window(event_mask=mask)))

    def test_rejects_window_with_properties(self):
        self.assertFalse(clipboard.is_wine_clipboard_window(wine_window(property_count=1)))

    def test_rejects_mapped_window(self):
        self.assertFalse(clipboard.is_wine_clipboard_window(wine_window(mapped=True)))

    def test_rejects_other_size(self):
        self.assertFalse(clipboard.is_wine_clipboard_window(wine_window(width=2, height=2)))

    def test_rejects_other_position(self):
        self.assertFalse(clipboard.is_wine_clipboard_window(wine_window(x=5)))

    def test_rejects_border(self):
        self.assertFalse(clipboard.is_wine_clipboard_window(wine_window(border=1)))

    def test_rejects_override_redirect(self):
        self.assertFalse(clipboard.is_wine_clipboard_window(wine_window(override_redirect=True)))

    def test_rejects_input_only(self):
        self.assertFalse(clipboard.is_wine_clipboard_window(wine_window(input_output=False)))


class WakeTargets(unittest.TestCase):
    def test_excludes_current_selection_owner(self):
        # xclip 持有剪貼簿時的視窗特徵和 Wine 的 import_window 一樣，靠「它是擁有者」排除。
        self.assertEqual(clipboard.wake_targets([0x1400003, 0x2600000], 0x2600000), [0x1400003])

    def test_empty_when_nothing_found(self):
        self.assertEqual(clipboard.wake_targets([], 0), [])

    def test_caps_number_of_targets(self):
        self.assertEqual(clipboard.wake_targets(list(range(1, 10)), 0, cap=4), [1, 2, 3, 4])

    def test_keeps_order(self):
        self.assertEqual(clipboard.wake_targets([3, 1, 2], 0), [3, 1, 2])


class SelectTimeout(unittest.TestCase):
    def test_waits_until_next_wake(self):
        self.assertAlmostEqual(clipboard.select_timeout(10.0, 10.3, 1.0), 0.3)

    def test_zero_when_wake_overdue(self):
        self.assertEqual(clipboard.select_timeout(10.0, 9.0, 1.0), 0)

    def test_never_exceeds_ceiling(self):
        self.assertEqual(clipboard.select_timeout(10.0, 15.0, 1.0), 1.0)


@unittest.skipUnless(ctypes.sizeof(ctypes.c_long) == 8, '版面數字只對 x86_64 的 LP64 成立')
class WindowAttributesLayout(unittest.TestCase):
    """結構錯位不會報錯，只會讀到錯的欄位，所以把 Xlib.h 的版面釘死。"""

    def test_size_matches_xlib(self):
        self.assertEqual(ctypes.sizeof(clipboard.WindowAttributes), 136)

    def test_field_offsets_match_xlib(self):
        attrs = clipboard.WindowAttributes
        self.assertEqual(attrs.map_state.offset, 92)
        self.assertEqual(attrs.all_event_masks.offset, 96)
        self.assertEqual(attrs.override_redirect.offset, 120)


if __name__ == '__main__':
    unittest.main()
