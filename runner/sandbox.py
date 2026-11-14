from __future__ import annotations

import ctypes
import errno
import os
import sys

SECCOMP_ALLOW = 0x7FFF0000
SECCOMP_ERRNO = 0x00050000 | errno.EPERM
PR_SET_NO_NEW_PRIVS = 38
RLIMIT_MEMORY = 128 * 1024 * 1024
RLIMIT_STACK_BYTES = 16 * 1024 * 1024

BLOCKED_SYSCALLS = (
    "accept", "accept4", "bind", "connect", "getpeername", "getsockname",
    "getsockopt", "listen", "recvfrom", "recvmmsg", "recvmsg", "sendmmsg",
    "sendmsg", "sendto", "setsockopt", "shutdown", "socket", "socketpair",
    "socketcall", "io_uring_enter", "io_uring_register", "io_uring_setup",
    "clone", "clone3", "execve", "execveat", "fork", "vfork",
    "chmod", "fchmod", "fchmodat", "chown", "fchown", "fchownat",
    "lchown", "umask", "link", "linkat", "symlink", "symlinkat",
    "mknod", "mknodat", "mount", "umount2", "pivot_root", "chroot",
    "unshare", "setns", "ptrace", "process_vm_readv", "process_vm_writev",
    "pidfd_getfd", "setgid", "setgroups", "setresgid", "setresuid", "setuid",
)


def set_execution_limits(uid: int, gid: int) -> None:
    if not sys_platform_is_linux():
        raise OSError("The isolated runner requires Linux.")
    import resource

    os.setgroups([])
    os.setgid(gid)
    os.setuid(uid)
    resource.setrlimit(resource.RLIMIT_CPU, (2, 2))
    resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
    resource.setrlimit(resource.RLIMIT_NOFILE, (32, 32))
    resource.setrlimit(resource.RLIMIT_AS, (RLIMIT_MEMORY, RLIMIT_MEMORY))
    resource.setrlimit(resource.RLIMIT_STACK, (RLIMIT_STACK_BYTES, RLIMIT_STACK_BYTES))
    resource.setrlimit(resource.RLIMIT_NPROC, (8, 8))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    os.umask(0o077)
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def sys_platform_is_linux() -> bool:
    return sys.platform == "linux"


def install_execution_filter() -> None:
    seccomp = ctypes.CDLL("libseccomp.so.2")
    seccomp.seccomp_init.argtypes = [ctypes.c_uint32]
    seccomp.seccomp_init.restype = ctypes.c_void_p
    seccomp.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    seccomp.seccomp_syscall_resolve_name.restype = ctypes.c_int
    seccomp.seccomp_rule_add.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint]
    seccomp.seccomp_rule_add.restype = ctypes.c_int
    seccomp.seccomp_load.argtypes = [ctypes.c_void_p]
    seccomp.seccomp_load.restype = ctypes.c_int
    seccomp.seccomp_release.argtypes = [ctypes.c_void_p]
    seccomp.seccomp_release.restype = None

    context = seccomp.seccomp_init(SECCOMP_ALLOW)
    if not context:
        raise OSError("Could not initialize the Python syscall filter.")
    try:
        for name in BLOCKED_SYSCALLS:
            syscall_number = seccomp.seccomp_syscall_resolve_name(name.encode("ascii"))
            if syscall_number < 0:
                continue
            result = seccomp.seccomp_rule_add(context, SECCOMP_ERRNO, syscall_number, 0)
            if result < 0:
                raise OSError(-result, f"Could not block syscall {name}.")
        result = seccomp.seccomp_load(context)
        if result < 0:
            raise OSError(-result, "Could not enable the Python syscall filter.")
    finally:
        seccomp.seccomp_release(context)
