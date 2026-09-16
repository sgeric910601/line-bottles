#!/usr/bin/python3
"""從 Windows PE 執行檔讀出版本號。

這台機器沒有 pefile / exiftool，而版本切換只需要「檔案版本」這一個欄位，
不值得為它引入新的相依。作法是直接掃描 VS_FIXEDFILEINFO 結構：

    dwSignature      0xFEEF04BD   offset 0
    dwStrucVersion                offset 4
    dwFileVersionMS               offset 8
    dwFileVersionLS               offset 12

簽章值夠特殊（4 bytes），再加上 dwStrucVersion 的合理性檢查，誤判機率極低。
與 x11.py 一樣只用標準函式庫。
"""
import struct
import sys
from pathlib import Path

SIGNATURE = b'\xbd\x04\xef\xfe'   # 0xFEEF04BD，小端序


def file_version(path):
    """回傳 'a.b.c.d' 字串；讀不到則回傳 None。"""
    try:
        data = Path(path).read_bytes()
    except OSError:
        return None

    start = 0
    while True:
        index = data.find(SIGNATURE, start)
        if index < 0:
            return None
        start = index + 4
        if index + 24 > len(data):
            continue
        struct_version, version_ms, version_ls = struct.unpack_from('<III', data, index + 4)
        # dwStrucVersion 實務上是 0x00010000（1.0）。值為 0 或過大代表撞到的
        # 只是碰巧相同的位元組，不是真正的版本結構。
        if struct_version >> 16 != 1:
            continue
        return '%d.%d.%d.%d' % (version_ms >> 16, version_ms & 0xFFFF,
                                version_ls >> 16, version_ls & 0xFFFF)


def main():
    if len(sys.argv) != 2:
        raise SystemExit('用法：peversion.py <PE 檔路徑>')
    version = file_version(sys.argv[1])
    if not version:
        raise SystemExit(1)
    print(version)


if __name__ == '__main__':
    main()
