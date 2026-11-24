"""Print the root-managed offline service unit for this Studio worker."""
import grp
from pathlib import Path
import os
import pwd
import re

repo = Path(__file__).resolve().parents[2]
runtime = (repo / ".kitten2").resolve(strict=True)
account = pwd.getpwuid(os.getuid())
if account.pw_uid == 0:
    raise SystemExit("Run this as the dedicated unprivileged GPU worker account.")
values = {"REPO": str(repo), "RUNTIME": str(runtime), "USER": account.pw_name,
          "GROUP": grp.getgrgid(account.pw_gid).gr_name}
template = Path(__file__).with_name("kitten-match.service.in").read_text()
for key, value in values.items():
    if not re.fullmatch(r"[/a-zA-Z0-9._-]+", value):
        raise SystemExit("Worker paths and account names must be safe systemd arguments.")
    template = template.replace("@" + key + "@", value)
print(template, end="")
