#!/usr/bin/env python3
"""
sdmult - clone one SD / microSD card and write it to many cards at once.

Built for Raspberry Pi OS (works on any Linux with util-linux). Pure Python 3
standard library, no extra packages needed. Must be run as root (sudo).

    sudo ./sdmult.py list
    sudo ./sdmult.py read  /dev/sda  -o master.img
    sudo ./sdmult.py write master.img /dev/sdb /dev/sdc /dev/sdd
    sudo ./sdmult.py write master.img --targets auto
    sudo ./sdmult.py clone /dev/sda --targets auto

Safety rules (enforced, not optional):
  * The disk holding /, /boot, /boot/firmware or swap is never written.
  * The source card is never written.
  * Targets must be whole disks (e.g. /dev/sdb, not /dev/sdb1).
  * Targets must be removable or USB-attached unless --allow-fixed is given.
  * Targets must be at least as large as the image.
  * Targets are opened exclusively (O_EXCL), so a mounted/in-use disk fails.
  * You must type "yes" after seeing the target list (unless --yes).
  * Every target is read back and compared (SHA-256) after writing.
"""

import argparse
import hashlib
import json
import mmap
import os
import stat
import subprocess
import sys
import threading
import time

VERSION = "1.0.0"
CHUNK = 4 * 1024 * 1024          # 4 MiB I/O blocks (multiple of any sector size)
SECTOR = 512
PROTECTED_MOUNTS = {"/", "/boot", "/boot/firmware", "/usr", "/var", "[SWAP]"}


class SafetyError(Exception):
    """Raised when an operation would be unsafe; nothing has been written."""


# --------------------------------------------------------------------------
# Device discovery
# --------------------------------------------------------------------------

def _run(cmd):
    return subprocess.run(cmd, check=True, capture_output=True, text=True).stdout


def lsblk_json():
    out = _run(["lsblk", "-J", "-b", "-o",
                "NAME,PATH,MAJ:MIN,SIZE,TYPE,TRAN,RM,RO,MODEL,MOUNTPOINT"])
    return json.loads(out)


def _flatten(node):
    yield node
    for child in node.get("children") or []:
        yield from _flatten(child)


def _root_devnums():
    """MAJ:MIN strings of the devices backing the running system."""
    nums = set()
    for p in ("/", "/boot", "/boot/firmware"):
        try:
            st = os.stat(p)
        except OSError:
            continue
        nums.add(f"{os.major(st.st_dev)}:{os.minor(st.st_dev)}")
    return nums


def _truthy(v):
    return v in (True, 1, "1", "true")


def parse_disks(lsblk_data, root_devnums=frozenset()):
    """Turn lsblk JSON into a list of whole-disk dicts with a 'protected' flag."""
    disks = []
    for top in lsblk_data.get("blockdevices", []):
        if top.get("type") not in ("disk", "loop"):
            continue
        nodes = list(_flatten(top))
        mounts = sorted({n["mountpoint"] for n in nodes if n.get("mountpoint")})
        reasons = []
        for n in nodes:
            if n.get("mountpoint") in PROTECTED_MOUNTS:
                reasons.append(f"{n.get('path')} mounted at {n['mountpoint']}")
            if n.get("maj:min") in root_devnums:
                reasons.append(f"{n.get('path')} backs the running system")
        disks.append({
            "path": top.get("path") or "/dev/" + top["name"],
            "name": top["name"],
            "size": int(top.get("size") or 0),
            "tran": top.get("tran") or "",
            "removable": _truthy(top.get("rm")),
            "readonly": _truthy(top.get("ro")),
            "model": (top.get("model") or "").strip(),
            "mounts": mounts,
            "partitions": [n.get("path") for n in nodes[1:]],
            "protected": bool(reasons),
            "protect_reasons": sorted(set(reasons)),
        })
    return disks


def get_disks():
    return parse_disks(lsblk_json(), _root_devnums())


def looks_removable(d):
    """Card reader, USB stick/reader, or the Pi's own SD slot (mmcblk*)."""
    return d["removable"] or d["tran"] in ("usb", "mmc") or d["name"].startswith("mmcblk")


def is_candidate_card(d):
    """A disk that looks like a card in a reader: removable or on USB, not protected."""
    return (not d["protected"] and not d["readonly"] and d["size"] > 0
            and looks_removable(d))


def find_disk(disks, path):
    real = os.path.realpath(path)
    for d in disks:
        if os.path.realpath(d["path"]) == real:
            return d
    for d in disks:
        if real in [os.path.realpath(p) for p in d["partitions"]]:
            raise SafetyError(f"{path} is a partition of {d['path']}; give the whole disk.")
    raise SafetyError(f"{path} is not a disk this system knows about (see `sdmult list`).")


def human(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1000 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1000


# --------------------------------------------------------------------------
# Safety checks
# --------------------------------------------------------------------------

def check_source(disks, src):
    d = find_disk(disks, src)
    if d["size"] <= 0:
        raise SafetyError(f"{src} reports size 0 (no card inserted?).")
    return d


def check_targets(disks, targets, image_size, source_path=None, allow_fixed=False):
    """Validate every target. Raises SafetyError on the first problem."""
    if not targets:
        raise SafetyError("No target cards given.")
    chosen, seen = [], set()
    src_real = os.path.realpath(source_path) if source_path else None
    for t in targets:
        d = find_disk(disks, t)
        real = os.path.realpath(d["path"])
        if real in seen:
            raise SafetyError(f"{t} is listed twice.")
        seen.add(real)
        if src_real and real == src_real:
            raise SafetyError(f"{t} is the source card; refusing to overwrite it.")
        if d["protected"]:
            raise SafetyError(f"{t} holds the running system ({'; '.join(d['protect_reasons'])}). "
                              "Refusing to write to it.")
        if d["readonly"]:
            raise SafetyError(f"{t} is read-only (check the card's lock switch).")
        if not looks_removable(d) and not allow_fixed:
            raise SafetyError(f"{t} is not a removable/USB disk. Use --allow-fixed only "
                              "if you are sure.")
        if d["size"] < image_size:
            raise SafetyError(f"{t} is {human(d['size'])} but the image is "
                              f"{human(image_size)}. Use a bigger card.")
        chosen.append(d)
    return chosen


def auto_targets(disks, source_path=None):
    src_real = os.path.realpath(source_path) if source_path else None
    return [d["path"] for d in disks
            if is_candidate_card(d) and os.path.realpath(d["path"]) != src_real]


def unmount_disk(d):
    for part in [d["path"]] + d["partitions"]:
        subprocess.run(["umount", part], capture_output=True)


def confirm(prompt_lines, assume_yes):
    for line in prompt_lines:
        print(line)
    if assume_yes:
        print("--yes given, continuing.")
        return
    ans = input('Type "yes" to ERASE these cards and continue: ').strip().lower()
    if ans != "yes":
        raise SafetyError("Cancelled by user. Nothing was written.")


# --------------------------------------------------------------------------
# Image sizing (trim to partitions so slightly smaller cards still work)
# --------------------------------------------------------------------------

def partition_table_type(dev_path):
    with open(dev_path, "rb") as f:
        head = f.read(2 * SECTOR)
    if len(head) >= 2 * SECTOR and head[SECTOR:SECTOR + 8] == b"EFI PART":
        return "gpt"
    if len(head) >= SECTOR and head[510:512] == b"\x55\xaa":
        return "mbr"
    return "none"


def mbr_end_bytes(dev_path):
    """End of the furthest MBR partition (extended container covers logicals)."""
    import struct
    with open(dev_path, "rb") as f:
        mbr = f.read(SECTOR)
    if len(mbr) < SECTOR or mbr[510:512] != b"\x55\xaa":
        return 0
    end = 0
    for i in range(4):
        e = mbr[446 + 16 * i: 462 + 16 * i]
        ptype = e[4]
        start, count = struct.unpack("<II", e[8:16])
        if ptype and count:
            end = max(end, (start + count) * SECTOR)
    return end


def partitions_end_bytes(disk_name, sysfs="/sys/class/block"):
    """Byte offset where the last partition ends, from sysfs. 0 if no partitions."""
    end = 0
    base = os.path.join(sysfs, disk_name)
    try:
        entries = os.listdir(base)
    except OSError:
        return 0
    for e in entries:
        p = os.path.join(base, e)
        if os.path.exists(os.path.join(p, "partition")):
            with open(os.path.join(p, "start")) as f:
                start = int(f.read())
            with open(os.path.join(p, "size")) as f:
                size = int(f.read())
            end = max(end, (start + size) * SECTOR)
    return end


def round_up(n, mult=CHUNK):
    return ((n + mult - 1) // mult) * mult


# --------------------------------------------------------------------------
# Low-level copy / verify
# --------------------------------------------------------------------------

class Progress:
    def __init__(self, name, total):
        self.name, self.total = name, total
        self.done = 0
        self.phase = "waiting"
        self.error = None
        self.started = time.time()
        self.phase_started = self.started

    def set_phase(self, phase):
        self.phase, self.done, self.phase_started = phase, 0, time.time()

    def line(self):
        pct = 100.0 * self.done / self.total if self.total else 100.0
        el = max(time.time() - self.phase_started, 1e-6)
        rate = self.done / el
        bar = "#" * int(pct / 5)
        tail = f"ERROR: {self.error}" if self.error else f"{rate / 1e6:6.1f} MB/s"
        return f"{self.name:<14} {self.phase:<9} [{bar:<20}] {pct:5.1f}%  {tail}"


def _aligned_buffer(size):
    return mmap.mmap(-1, size)   # page-aligned, usable with O_DIRECT


def _open_direct_read(path):
    flags = os.O_RDONLY | getattr(os, "O_DIRECT", 0)
    try:
        return os.open(path, flags), True
    except OSError:
        return os.open(path, os.O_RDONLY), False


def hash_range(path, length, progress=None, direct=True):
    """SHA-256 of the first `length` bytes of path, bypassing page cache if possible."""
    h = hashlib.sha256()
    if direct:
        fd, is_direct = _open_direct_read(path)
    else:
        fd, is_direct = os.open(path, os.O_RDONLY), False
    buf = _aligned_buffer(CHUNK)
    try:
        if not is_direct:
            try:
                os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
            except (OSError, AttributeError):
                pass
        off = 0
        while off < length:
            want = min(CHUNK, length - off)
            if is_direct and want % SECTOR:
                # unaligned tail: finish with a normal read
                os.close(fd)
                fd, is_direct = os.open(path, os.O_RDONLY), False
            if is_direct:
                n = os.preadv(fd, [buf], off)
                with memoryview(buf) as mv:
                    data = mv[:min(n, want)]
                    h.update(data)
                    got = len(data)
                    data.release()
            else:
                data = os.pread(fd, want, off)
                h.update(data)
                got = len(data)
            if not got:
                raise IOError(f"{path}: unexpected end of data at byte {off}")
            off += got
            if progress:
                progress.done = off
    finally:
        os.close(fd)
        buf.close()
    return h.hexdigest()


def read_device_to_image(src, image_path, length, progress=None):
    """Copy the first `length` bytes of src into image_path. Returns sha256."""
    h = hashlib.sha256()
    tmp = image_path + ".partial"
    with open(src, "rb", buffering=0) as fin, open(tmp, "wb") as fout:
        off = 0
        while off < length:
            data = fin.read(min(CHUNK, length - off))
            if not data:
                raise IOError(f"{src}: unexpected end of device at byte {off}")
            fout.write(data)
            h.update(data)
            off += len(data)
            if progress:
                progress.done = off
        fout.flush()
        os.fsync(fout.fileno())
    os.replace(tmp, image_path)
    digest = h.hexdigest()
    with open(image_path + ".sha256", "w") as f:
        f.write(f"{digest}  {os.path.basename(image_path)}\n")
    return digest


def write_image_to_target(image_path, target, length, progress=None, exclusive=True):
    flags = os.O_WRONLY
    if exclusive:
        flags |= os.O_EXCL   # on a block device: fails if mounted / in use
    fd = os.open(target, flags)
    try:
        with open(image_path, "rb", buffering=0) as fin:
            off = 0
            while off < length:
                data = fin.read(min(CHUNK, length - off))
                if not data:
                    raise IOError(f"{image_path}: shorter than expected")
                view = memoryview(data)
                while view:
                    n = os.pwrite(fd, view, off)
                    view = view[n:]
                    off += n
                if progress:
                    progress.done = off
                # keep dirty pages bounded so progress is honest and RAM stays free
                if off % (64 * 1024 * 1024) == 0:
                    os.fdatasync(fd)
        os.fsync(fd)
        try:
            os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
        except (OSError, AttributeError):
            pass
    finally:
        os.close(fd)


def image_digest(image_path, length):
    side = image_path + ".sha256"
    if (os.path.exists(side) and os.path.getsize(image_path) == length
            and os.path.getmtime(side) >= os.path.getmtime(image_path)):
        with open(side) as f:
            first = f.read().split()
        if first and len(first[0]) == 64:
            return first[0]
    print(f"Hashing {image_path} ...")
    d = hash_range(image_path, length, direct=False)
    with open(side, "w") as f:
        f.write(f"{d}  {os.path.basename(image_path)}\n")
    return d


def duplicate(image_path, targets, expected_digest, verify=True, exclusive=True,
              after_write=None, out=sys.stdout, progs=None):
    """Write image to all targets in parallel, then verify each. Returns {target: ok}.

    Pass a dict as `progs` to watch live Progress objects from another thread (GUI)."""
    length = os.path.getsize(image_path)
    if progs is None:
        progs = {}
    progs.update({t: Progress(os.path.basename(t), length) for t in targets})

    def worker(t):
        p = progs[t]
        try:
            p.set_phase("writing")
            write_image_to_target(image_path, t, length, p, exclusive=exclusive)
            if after_write:
                after_write(t)
            if verify:
                p.set_phase("verifying")
                got = hash_range(t, length, p)
                if got != expected_digest:
                    raise IOError("verify FAILED: data on card does not match image")
            p.set_phase("done")
            p.done = p.total
        except Exception as e:   # report per card, keep the others going
            p.error = str(e)
            p.phase = "failed"

    threads = [threading.Thread(target=worker, args=(t,), daemon=True) for t in targets]
    for th in threads:
        th.start()
    if out is None:
        for th in threads:
            th.join()
    else:
        _watch(progs, threads, out)
    return {t: progs[t].error is None for t in targets}, progs


def _watch(progs, threads, out):
    tty = out.isatty()
    drawn = 0
    last_plain = 0
    while True:
        alive = any(th.is_alive() for th in threads)
        lines = [p.line() for p in progs.values()]
        if tty:
            if drawn:
                out.write(f"\x1b[{drawn}F")
            out.write("\n".join(l + "\x1b[K" for l in lines) + "\n")
            drawn = len(lines)
            out.flush()
        elif time.time() - last_plain > 10 or not alive:
            out.write("\n".join(lines) + "\n")
            out.flush()
            last_plain = time.time()
        if not alive:
            break
        time.sleep(0.5)


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def require_root():
    if os.geteuid() != 0:
        raise SafetyError("Run with sudo (raw disk access needs root).")


def cmd_list(args):
    disks = get_disks()
    print(f"{'DEVICE':<14}{'SIZE':>10}  {'BUS':<5} {'STATUS':<22} MODEL")
    for d in disks:
        if d["protected"]:
            status = "PROTECTED (system)"
        elif is_candidate_card(d):
            status = "card / usb"
        else:
            status = "fixed disk"
        if d["mounts"] and not d["protected"]:
            status += " mounted"
        print(f"{d['path']:<14}{human(d['size']):>10}  {d['tran'] or '-':<5} "
              f"{status:<22} {d['model']}")
    return 0


def plan_read(src, full):
    """Decide how many bytes of the source to copy. Returns (disk, length)."""
    disks = get_disks()
    d = check_source(disks, src)
    ptype = partition_table_type(d["path"])
    length = d["size"]
    if not full:
        if ptype == "mbr":
            end = max(mbr_end_bytes(d["path"]), partitions_end_bytes(d["name"]))
            if end:
                length = min(d["size"], round_up(end, 1024 * 1024))
                print(f"MBR card: copying up to the end of the last partition "
                      f"({human(length)} of {human(d['size'])}). Use --full for every byte.")
        elif ptype == "gpt":
            print("GPT card: copying the whole card so the backup GPT header is kept.")
    return disks, d, length


def do_read(d, image, length):
    folder = os.path.dirname(os.path.abspath(image))
    os.makedirs(folder, exist_ok=True)
    st = os.statvfs(folder)
    if st.f_bavail * st.f_frsize < length + 64 * 1024 * 1024:
        raise SafetyError(f"Not enough free space for a {human(length)} image in {folder}.")
    p = Progress(os.path.basename(d["path"]), length)
    p.set_phase("reading")
    t = threading.Thread(target=lambda: _read_thread(d["path"], image, length, p), daemon=True)
    t.start()
    _watch({d["path"]: p}, [t], sys.stdout)
    if p.error:
        raise IOError(p.error)
    print(f"Image saved: {image}  ({human(length)})  sha256 {p.digest}")
    return p.digest


def _read_thread(src, image, length, p):
    try:
        p.digest = read_device_to_image(src, image, length, p)
        p.phase = "done"
    except Exception as e:
        p.error, p.phase = str(e), "failed"


def cmd_read(args):
    require_root()
    _, d, length = plan_read(args.source, args.full)
    do_read(d, args.output, length)
    return 0


def _resolve_targets(args, disks, source_path):
    if args.targets == ["auto"] or args.targets == "auto":
        t = auto_targets(disks, source_path)
        if not t:
            raise SafetyError("No removable cards found for --targets auto.")
        return t
    return args.targets


def _do_write(image, targets, source_path, args):
    if not os.path.isfile(image):
        raise SafetyError(f"Image file {image} not found.")
    length = os.path.getsize(image)
    if length == 0:
        raise SafetyError(f"{image} is empty.")
    if image.endswith((".xz", ".gz", ".zip", ".zst")):
        raise SafetyError("Compressed images are not supported yet; decompress to a .img first.")
    disks = get_disks()
    targets = _resolve_targets(args, disks, source_path)
    chosen = check_targets(disks, targets, length, source_path, args.allow_fixed)
    lines = [f"Image: {image}  ({human(length)})", "Cards that will be ERASED:"]
    for d in chosen:
        lines.append(f"   {d['path']:<12} {human(d['size']):>10}  {d['model']}"
                     + (f"  (mounted: {', '.join(d['mounts'])} - will unmount)" if d["mounts"] else ""))
    confirm(lines, args.yes)
    digest = image_digest(image, length)
    for d in chosen:
        unmount_disk(d)

    def reread(t):
        subprocess.run(["blockdev", "--flushbufs", t], capture_output=True)

    results, progs = duplicate(image, [d["path"] for d in chosen], digest,
                               verify=not args.no_verify, after_write=reread)
    for d in chosen:
        subprocess.run(["blockdev", "--rereadpt", d["path"]], capture_output=True)
    ok = [t for t, good in results.items() if good]
    bad = [t for t, good in results.items() if not good]
    print(f"\n{len(ok)} of {len(results)} cards written"
          + (" and verified." if not args.no_verify else " (NOT verified)."))
    for t in bad:
        print(f"  FAILED {t}: {progs[t].error}")
    if ok:
        print("Safe to remove the good cards once their activity LEDs stop.")
    return 0 if not bad else 2


def cmd_write(args):
    require_root()
    return _do_write(args.image, args.targets, None, args)


def cmd_clone(args):
    require_root()
    image = args.image or os.path.join("/var/tmp", "sdmult-master.img")
    disks, d, length = plan_read(args.source, args.full)
    # check every target BEFORE spending time reading the source
    check_targets(disks, _resolve_targets(args, disks, d["path"]), length,
                  d["path"], args.allow_fixed)
    try:
        do_read(d, image, length)
        return _do_write(image, args.targets, args.source, args)
    finally:
        if not args.keep_image and not args.image:
            for p in (image, image + ".sha256", image + ".partial"):
                try:
                    os.remove(p)
                except OSError:
                    pass


def build_parser():
    ap = argparse.ArgumentParser(prog="sdmult", description="Clone one SD card to many.")
    ap.add_argument("--version", action="version", version=VERSION)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="show disks and which are safe targets").set_defaults(fn=cmd_list)

    r = sub.add_parser("read", help="save a card to an image file")
    r.add_argument("source")
    r.add_argument("-o", "--output", required=True)
    r.add_argument("--full", action="store_true", help="copy every byte, not just partitions")
    r.set_defaults(fn=cmd_read)

    def add_write_opts(p):
        p.add_argument("--targets", nargs="+", default=None,
                       help='target disks, or "auto" for every removable card')
        p.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
        p.add_argument("--no-verify", action="store_true", help="skip read-back verification")
        p.add_argument("--allow-fixed", action="store_true",
                       help="allow non-removable, non-USB targets (dangerous)")

    w = sub.add_parser("write", help="write an image file to many cards")
    w.add_argument("image")
    w.add_argument("target_list", nargs="*", metavar="target")
    add_write_opts(w)
    w.set_defaults(fn=cmd_write)

    c = sub.add_parser("clone", help="read a card then write it to many cards")
    c.add_argument("source")
    c.add_argument("target_list", nargs="*", metavar="target")
    c.add_argument("--image", help="where to keep the master image (default: temp, deleted)")
    c.add_argument("--keep-image", action="store_true")
    c.add_argument("--full", action="store_true")
    add_write_opts(c)
    c.set_defaults(fn=cmd_clone)
    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
    if hasattr(args, "target_list"):
        args.targets = (args.targets or []) + args.target_list
    try:
        return args.fn(args)
    except SafetyError as e:
        print(f"Refused: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted. Cards being written are now incomplete; rewrite them.",
              file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
