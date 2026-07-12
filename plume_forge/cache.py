import os
import shutil
import ctypes

from .utils import output_directory_for_object

MANIFEST = "plume_forge_cache.json"
PREVIEW_DIRECTORY = ".plume_forge_preview_cache"
LOCK_FILE = ".plume_forge.lock"


def manifest_path(domain):
    return os.path.join(output_directory_for_object(domain), MANIFEST)


def preview_directory(domain):
    return os.path.join(output_directory_for_object(domain), PREVIEW_DIRECTORY)


def invalidate_cache(domain):
    path = manifest_path(domain)
    if os.path.isfile(path):
        try:
            os.remove(path)
        except OSError:
            pass


def invalidate_preview_cache(domain):
    directory = preview_directory(domain)
    if os.path.isdir(directory):
        try:
            shutil.rmtree(directory)
        except OSError:
            pass


class CacheLock:
    def __init__(self, directory):
        self.directory = os.path.normpath(directory)
        self.path = os.path.join(self.directory, LOCK_FILE)
        self._owned = False

    def acquire(self):
        os.makedirs(self.directory, exist_ok=True)
        try:
            descriptor = os.open(
                self.path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            )
        except FileExistsError:
            if _lock_is_stale(self.path):
                try:
                    os.remove(self.path)
                except FileNotFoundError:
                    pass
                return self.acquire()
            raise RuntimeError(
                f"Plume Forge cache is already in use: {self.directory}"
            )
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(str(os.getpid()))
        self._owned = True
        return self

    def release(self):
        if not self._owned:
            return
        try:
            os.remove(self.path)
        except FileNotFoundError:
            pass
        self._owned = False

    def __enter__(self):
        return self.acquire()

    def __exit__(self, _type, _value, _traceback):
        self.release()


def cache_lock(directory):
    return CacheLock(directory)


def recover_cache_lock(directory):
    path = os.path.join(os.path.normpath(directory), LOCK_FILE)
    if os.path.isfile(path) and _lock_is_stale(path):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass


def _lock_is_stale(path):
    try:
        with open(path, encoding="utf-8") as stream:
            pid = int(stream.read().strip())
        return not _pid_is_alive(pid)
    except (FileNotFoundError, ValueError):
        return True


def _pid_is_alive(pid):
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except PermissionError:
            return True
        except ProcessLookupError:
            return False

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = (ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong)
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.GetExitCodeProcess.argtypes = (
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_ulong),
    )
    kernel32.GetExitCodeProcess.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
    kernel32.CloseHandle.restype = ctypes.c_int
    handle = kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        return ctypes.get_last_error() == 5
    try:
        exit_code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return True
        return exit_code.value == 259
    finally:
        kernel32.CloseHandle(handle)
