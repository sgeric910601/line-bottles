"""Unit tests for the pure decision logic of the clipboard bridge.

The daemon itself needs two live X servers, so only the format choice and the
loop guard are covered here; end-to-end behaviour is on the manual checklist.
"""
import hashlib
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


if __name__ == '__main__':
    unittest.main()
