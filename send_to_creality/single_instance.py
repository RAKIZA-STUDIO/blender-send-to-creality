# SPDX-License-Identifier: GPL-3.0-or-later
"""Change Creality's single instance option and keep its settings file valid.

Also runs on its own, without Blender: a window started with the option off
saves it as off when closed, so this waits for that window to close and turns
the option back on.
"""

import ctypes
import hashlib
import os
import re
import sys
import time

SYNCHRONIZE = 0x00100000
INFINITE = 0xFFFFFFFF


def set_single_instance(path, value):
    """Set the option to "true" or "false" and update the file's checksum."""
    with open(path, "rb") as config:
        data = config.read()
    updated = re.sub(
        rb'("single_instance"\s*:\s*)(true|false)', rb"\g<1>" + value.encode(), data)
    line = re.search(rb"(\r?\n)# MD5 checksum [0-9A-Fa-f]{32}", updated)
    if line:
        body = updated[:line.start()]
        digest = hashlib.md5(body.replace(b"\r\n", b"\n")).hexdigest().upper().encode()
        updated = body + line.group(1) + b"# MD5 checksum " + digest + updated[line.end():]
    if updated != data:
        temporary = f"{path}.send_to_creality"
        with open(temporary, "wb") as config:
            config.write(updated)
        os.replace(temporary, path)


def wait_for_process(pid):
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
    if handle:
        kernel32.WaitForSingleObject(handle, INFINITE)
        kernel32.CloseHandle(handle)


def restore_after_exit(pid, path):
    wait_for_process(pid)
    for _attempt in range(10):
        try:
            set_single_instance(path, "true")
            return
        except OSError:
            time.sleep(1.0)


if __name__ == "__main__":
    restore_after_exit(int(sys.argv[1]), sys.argv[2])
