"""URL parsing shared by submission, extraction and the media egress proxy."""
import ipaddress
import re
from urllib.parse import urlsplit, urlunsplit

URL_ERROR = "Paste a public HTTP or HTTPS link to one video."
LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", re.ASCII)
REEL = re.compile(r"/(reels?|share/reel)/([A-Za-z0-9_-]{5,64})/?", re.ASCII | re.I)
# These can tunnel IPv4 destinations even when the IPv6 address appears global.
TRANSLATED = tuple(ipaddress.ip_network(prefix) for prefix in (
    "64:ff9b::/96", "64:ff9b:1::/48", "2001::/32", "2002::/16",
))


def public_address(value):
    addr = ipaddress.ip_address(value)
    if addr.version == 6 and (addr.scope_id or addr.ipv4_mapped or any(addr in net for net in TRANSLATED)):
        return False
    return addr.is_global and not (addr.is_multicast or addr.is_reserved or addr.is_loopback or addr.is_link_local)


def public_hostname(host):
    if not host or "%" in host:
        raise ValueError(URL_ERROR)
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        name = host.removesuffix(".").encode("idna").decode("ascii").lower()
        labels = name.split(".")
        if (len(name) > 253 or len(labels) < 2 or not all(LABEL.fullmatch(label) for label in labels)
                or labels[-1] in {"localhost", "local", "internal", "lan", "home", "onion"}
                or labels[-1].isdigit() or re.fullmatch(r"0x[\da-f]+", labels[-1])):
            raise ValueError(URL_ERROR)
        return name
    if not public_address(addr):
        raise ValueError(URL_ERROR)
    return str(addr)


def video_url(value, *, submission=True):
    limit = 8192 if submission else 65536
    if (not isinstance(value, str) or len(value) > limit or "\\" in value
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise ValueError(URL_ERROR)
    value = value.strip()
    if any(char.isspace() for char in value) or submission and "#__youtubedl_smuggle" in value:
        raise ValueError(URL_ERROR)
    try:
        url = urlsplit(value)
        if url.scheme not in ("https", "http") or url.username is not None or url.password is not None:
            raise ValueError(URL_ERROR)
        host = public_hostname(url.hostname)
        authority = f"[{host}]" if ":" in host else host
        default = 443 if url.scheme == "https" else 80
        if url.port not in (None, default) or url.netloc.endswith(":"):
            raise ValueError(URL_ERROR)
    except (ValueError, UnicodeError) as error:
        raise ValueError(URL_ERROR) from error
    # Keep old Instagram cache keys stable. All other sites may require the original query or fragment.
    if submission and host in ("instagram.com", "www.instagram.com", "m.instagram.com"):
        match = REEL.fullmatch(url.path)
        if match:
            path = "share/reel" if match[1].lower() == "share/reel" else "reel"
            return f"https://www.instagram.com/{path}/{match[2]}/"
    return urlunsplit((url.scheme, authority, url.path or "/", url.query, url.fragment))
