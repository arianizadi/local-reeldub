"""Run native intake with TCP-only internet sockets. DNS uses TCP to the host resolver."""
import errno
import os
import socket
import sys

import seccomp

policy = seccomp.SyscallFilter(defaction=seccomp.ALLOW)
policy.add_rule(seccomp.ERRNO(errno.EACCES), "socket", seccomp.Arg(1, seccomp.MASKED_EQ, 15, socket.SOCK_DGRAM))
policy.load()
os.execvp(sys.argv[1], sys.argv[1:])
