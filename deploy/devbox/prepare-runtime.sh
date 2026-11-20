#!/usr/bin/env bash
# Install the native Ubuntu runtime in the current user's home, without a Docker daemon.
set -euo pipefail
umask 077
. /etc/os-release
[[ $ID == ubuntu && $VERSION_ID == 24.04 && $(uname -m) == x86_64 ]] || {
    echo "This native runtime requires Ubuntu 24.04 on x86-64. Use Compose on other systems." >&2
    exit 1
}
prefix="$HOME/.local/opt/reeldub-host"
install -d -m 0700 "$prefix"
build=$(mktemp -d "$prefix/.runtime.XXXXXXXX")
trap 'rm -rf -- "$build"' EXIT
cd "$build"
for pair in gocryptfs:crypt nginx:nginx python3-seccomp:seccomp; do
    pkg=${pair%:*}
    target="$prefix/${pair#*:}-package"
    apt-get download "$pkg"
    dpkg-deb -x "$pkg"_*.deb "$target"
done
curl -fsSL --retry 2 -o mongo.tgz https://fastdl.mongodb.org/linux/mongodb-linux-x86_64-ubuntu2404-8.0.38.tgz
echo '2885d4382bd47b96bfaf3edc7dec0ef44463e3e2a79479f584d20deb6182cda9  mongo.tgz' | sha256sum -c -
tar -xzf mongo.tgz --strip-components=2 mongodb-linux-x86_64-ubuntu2404-8.0.38/bin/mongod
curl -fsSL --retry 2 -o landrun https://github.com/Zouuup/landrun/releases/download/v0.1.17/landrun-linux-amd64
echo '6ada66a06669e8994e174a7271af2db636308e55a0d6ec896cc7d326b46727f6  landrun' | sha256sum -c -
for program in mongod landrun; do
    chmod 0700 "$program"
    mv -f "$program" "$prefix/$program"
done
