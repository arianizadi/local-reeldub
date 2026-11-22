"""Apply Linux socket restrictions before entering the worker or model sandbox."""
import errno
import os
import socket
import sys

import seccomp

mode, program, *args = sys.argv[1:]
policy = seccomp.SyscallFilter(defaction=seccomp.ALLOW)
denied = seccomp.ERRNO(errno.EPERM)
policy.add_rule(denied, "io_uring_setup")
if mode == "engine":
    # CUDA creates a domain-scoped Unix SOCK_SEQPACKET listener for its UVM driver.
    # No client connections are needed; the optional CUDA MPS endpoint is treated as absent.
    policy.add_rule(denied, "socket", seccomp.Arg(0, seccomp.NE, socket.AF_UNIX))
    for kind in (socket.SOCK_STREAM, socket.SOCK_DGRAM, socket.SOCK_RAW):
        policy.add_rule(denied, "socket", seccomp.Arg(1, seccomp.MASKED_EQ, 15, kind))
    policy.add_rule(seccomp.ERRNO(errno.ENOENT), "connect")
    policy.add_rule(denied, "socketpair", seccomp.Arg(1, seccomp.MASKED_EQ, 15, socket.SOCK_DGRAM))
    policy.add_rule(denied, "sendto", seccomp.Arg(4, seccomp.NE, 0))
elif mode == "worker":
    # The coordinator uses TCP and verified TLS. This ABI cannot filter pathname Unix sockets.
    policy.add_rule(denied, "socket", seccomp.Arg(0, seccomp.EQ, socket.AF_UNIX),
                    seccomp.Arg(1, seccomp.MASKED_EQ, 15, socket.SOCK_STREAM))
    for kind in (socket.SOCK_DGRAM, socket.SOCK_RAW):
        policy.add_rule(denied, "socket", seccomp.Arg(1, seccomp.MASKED_EQ, 15, kind))
else:
    raise ValueError("Expected worker or engine")
policy.load()
os.execv(program, [program, *args])
