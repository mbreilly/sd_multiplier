#!/usr/bin/env python3
"""Tests for sdmult. Run: python3 -m unittest test_sdmult  (no root, no real cards needed)."""
import hashlib
import io
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sdmult as S

GB = 1000 ** 3

# What lsblk -J looks like on a Pi booted from microSD with three USB card readers.
PI_LSBLK = {"blockdevices": [
    {"name": "sda", "path": "/dev/sda", "maj:min": "8:0", "size": 32 * GB, "type": "disk",
     "tran": "usb", "rm": True, "ro": False, "model": "Reader A", "mountpoint": None,
     "children": [
         {"name": "sda1", "path": "/dev/sda1", "maj:min": "8:1", "size": 1, "type": "part",
          "mountpoint": "/media/pi/bootfs"},
         {"name": "sda2", "path": "/dev/sda2", "maj:min": "8:2", "size": 1, "type": "part",
          "mountpoint": None}]},
    {"name": "sdb", "path": "/dev/sdb", "maj:min": "8:16", "size": 32 * GB, "type": "disk",
     "tran": "usb", "rm": True, "ro": False, "model": "Reader B", "mountpoint": None},
    {"name": "sdc", "path": "/dev/sdc", "maj:min": "8:32", "size": 16 * GB, "type": "disk",
     "tran": "usb", "rm": True, "ro": False, "model": "Reader C", "mountpoint": None},
    {"name": "nvme0n1", "path": "/dev/nvme0n1", "maj:min": "259:0", "size": 500 * GB,
     "type": "disk", "tran": "nvme", "rm": False, "ro": False, "model": "SSD",
     "mountpoint": None},
    {"name": "mmcblk0", "path": "/dev/mmcblk0", "maj:min": "179:0", "size": 64 * GB,
     "type": "disk", "tran": None, "rm": False, "ro": False, "model": None, "mountpoint": None,
     "children": [
         {"name": "mmcblk0p1", "path": "/dev/mmcblk0p1", "maj:min": "179:1", "size": 1,
          "type": "part", "mountpoint": "/boot/firmware"},
         {"name": "mmcblk0p2", "path": "/dev/mmcblk0p2", "maj:min": "179:2", "size": 1,
          "type": "part", "mountpoint": "/"}]},
]}


class FakeDisks(unittest.TestCase):
    def setUp(self):
        self.disks = S.parse_disks(PI_LSBLK)
        # paths are fake, so make realpath a no-op
        self._rp = S.os.path.realpath
        S.os.path.realpath = lambda p: p

    def tearDown(self):
        S.os.path.realpath = self._rp

    def test_boot_disk_protected(self):
        d = S.find_disk(self.disks, "/dev/mmcblk0")
        self.assertTrue(d["protected"])
        with self.assertRaisesRegex(S.SafetyError, "running system"):
            S.check_targets(self.disks, ["/dev/mmcblk0"], 1, allow_fixed=True)

    def test_protected_by_devnum_even_without_mountpoint(self):
        data = {"blockdevices": [dict(PI_LSBLK["blockdevices"][1])]}
        d = S.parse_disks(data, {"8:16"})[0]
        self.assertTrue(d["protected"])

    def test_source_never_target(self):
        with self.assertRaisesRegex(S.SafetyError, "source"):
            S.check_targets(self.disks, ["/dev/sdb", "/dev/sda"], 1, source_path="/dev/sda")

    def test_partition_rejected(self):
        with self.assertRaisesRegex(S.SafetyError, "partition"):
            S.check_targets(self.disks, ["/dev/sda1"], 1)

    def test_fixed_disk_needs_flag(self):
        with self.assertRaisesRegex(S.SafetyError, "allow-fixed"):
            S.check_targets(self.disks, ["/dev/nvme0n1"], 1)
        S.check_targets(self.disks, ["/dev/nvme0n1"], 1, allow_fixed=True)

    def test_too_small(self):
        with self.assertRaisesRegex(S.SafetyError, "bigger card"):
            S.check_targets(self.disks, ["/dev/sdc"], 20 * GB)

    def test_duplicate_target(self):
        with self.assertRaisesRegex(S.SafetyError, "twice"):
            S.check_targets(self.disks, ["/dev/sdb", "/dev/sdb"], 1)

    def test_unknown(self):
        with self.assertRaises(S.SafetyError):
            S.check_targets(self.disks, ["/dev/sdz"], 1)

    def test_auto_targets(self):
        self.assertEqual(S.auto_targets(self.disks, "/dev/sda"), ["/dev/sdb", "/dev/sdc"])

    def test_confirm_requires_yes(self):
        S_input = __builtins__.input if hasattr(__builtins__, "input") else None
        import builtins
        orig = builtins.input
        try:
            builtins.input = lambda _: "y"
            with self.assertRaises(S.SafetyError):
                S.confirm([], False)
            builtins.input = lambda _: "yes"
            S.confirm([], False)
        finally:
            builtins.input = orig


class CopyAndVerify(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.d = self.dir.name
        self.size = 3 * S.CHUNK + 4096   # not a whole number of chunks
        self.src = os.path.join(self.d, "card.bin")
        with open(self.src, "wb") as f:
            f.write(os.urandom(self.size))
            f.write(b"\xEE" * S.CHUNK)    # extra space past the "partitions"

    def tearDown(self):
        self.dir.cleanup()

    def _blank(self, name, size):
        p = os.path.join(self.d, name)
        with open(p, "wb") as f:
            f.truncate(size)
        return p

    def test_read_write_verify_many(self):
        img = os.path.join(self.d, "master.img")
        digest = S.read_device_to_image(self.src, img, self.size)
        self.assertEqual(os.path.getsize(img), self.size)
        with open(self.src, "rb") as f:
            self.assertEqual(digest, hashlib.sha256(f.read(self.size)).hexdigest())
        self.assertTrue(os.path.exists(img + ".sha256"))
        self.assertEqual(S.image_digest(img, self.size), digest)

        targets = [self._blank(f"t{i}", self.size + S.CHUNK) for i in range(4)]
        res, _ = S.duplicate(img, targets, digest, exclusive=False, out=io.StringIO())
        self.assertTrue(all(res.values()), res)
        for t in targets:
            self.assertEqual(S.hash_range(t, self.size), digest)

    def test_verify_catches_corruption(self):
        img = os.path.join(self.d, "master.img")
        digest = S.read_device_to_image(self.src, img, self.size)
        good = self._blank("good", self.size)
        bad = self._blank("bad", self.size)

        def corrupt(t):
            if t == bad:
                with open(t, "r+b") as f:
                    f.seek(12345)
                    f.write(b"\x00\x01\x02")
        res, progs = S.duplicate(img, [good, bad], digest, exclusive=False,
                                 after_write=corrupt, out=io.StringIO())
        self.assertTrue(res[good])
        self.assertFalse(res[bad])
        self.assertIn("verify FAILED", progs[bad].error)

    def test_short_target_fails_cleanly(self):
        img = os.path.join(self.d, "master.img")
        digest = S.read_device_to_image(self.src, img, self.size)
        # a regular file just grows, so simulate a write error with a directory path
        res, progs = S.duplicate(img, [self.d], digest, exclusive=False, out=io.StringIO())
        self.assertFalse(res[self.d])

    def test_partition_table_and_end(self):
        img = self._blank("mbr", 4096)
        with open(img, "r+b") as f:
            f.seek(510)
            f.write(b"\x55\xaa")
        self.assertEqual(S.partition_table_type(img), "mbr")
        with open(img, "r+b") as f:
            f.seek(512)
            f.write(b"EFI PART")
        self.assertEqual(S.partition_table_type(img), "gpt")

        import struct
        mbr = bytearray(512)
        mbr[446:462] = struct.pack("<B3sB3sII", 0, b"\0" * 3, 0x0c, b"\0" * 3, 8192, 1048576)
        mbr[462:478] = struct.pack("<B3sB3sII", 0, b"\0" * 3, 0x83, b"\0" * 3, 1056768, 2000000)
        mbr[510:512] = b"\x55\xaa"
        with open(img, "r+b") as f:
            f.write(mbr)
        self.assertEqual(S.mbr_end_bytes(img), (1056768 + 2000000) * 512)

        sysfs = os.path.join(self.d, "sys")
        for part, start, size in (("sdx1", 8192, 1048576), ("sdx2", 1056768, 2000000)):
            p = os.path.join(sysfs, "sdx", part)
            os.makedirs(p)
            open(os.path.join(p, "partition"), "w").write("1")
            open(os.path.join(p, "start"), "w").write(str(start))
            open(os.path.join(p, "size"), "w").write(str(size))
        os.makedirs(os.path.join(sysfs, "sdx", "queue"))
        self.assertEqual(S.partitions_end_bytes("sdx", sysfs), (1056768 + 2000000) * 512)


if __name__ == "__main__":
    unittest.main()
