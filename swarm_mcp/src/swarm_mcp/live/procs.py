"""Process helpers (stdlib only): find the Claude Code process that ran a hook, and check whether a PID is alive.
Lets the server shut itself down once every Claude Code session it serves has exited, even if SessionEnd never fired."""

import os
import subprocess

CLAUDE_NAMES = ("node", "bun")  # besides anything named claude*: Claude Code installed via npm runs as node/bun


def _table():
    """{pid: (ppid, exe name)} for every process."""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        class PE(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", ctypes.c_wchar * 260),
            ]

        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        snap = k.CreateToolhelp32Snapshot(2, 0)  # TH32CS_SNAPPROCESS
        if not snap or snap == wintypes.HANDLE(-1).value:
            return {}
        out, e = {}, PE()
        e.dwSize = ctypes.sizeof(PE)
        try:
            ok = k.Process32FirstW(snap, ctypes.byref(e))
            while ok:
                out[e.th32ProcessID] = (e.th32ParentProcessID, e.szExeFile)
                ok = k.Process32NextW(snap, ctypes.byref(e))
        finally:
            k.CloseHandle(snap)
        return out
    res = subprocess.run(["ps", "-A", "-o", "pid=,ppid=,comm="], capture_output=True, text=True, timeout=5)
    out = {}
    for line in res.stdout.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
            out[int(parts[0])] = (int(parts[1]), os.path.basename(parts[2].strip()))
    return out


def claude_pid():
    """PID of the nearest ancestor that looks like Claude Code, or None if it can't be found."""
    try:
        tree = _table()
    except Exception:
        return None
    pid, seen = os.getppid(), set()
    while pid and pid in tree and pid not in seen:
        seen.add(pid)
        ppid, name = tree[pid]
        n = name.lower()
        if n.endswith(".exe"):
            n = n[:-4]
        if n.startswith("claude") or n in CLAUDE_NAMES:
            return pid
        pid = ppid
    return None


def alive(pid):
    if os.name == "nt":  # os.kill(pid, 0) would terminate the process on Windows
        import ctypes

        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.OpenProcess.restype = ctypes.c_void_p
        h = k.OpenProcess(0x00100000 | 0x1000, False, pid)  # SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return ctypes.get_last_error() == 5  # access denied: exists but isn't ours
        try:
            return k.WaitForSingleObject(ctypes.c_void_p(h), 0) == 0x102  # WAIT_TIMEOUT: still running
        finally:
            k.CloseHandle(ctypes.c_void_p(h))
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
