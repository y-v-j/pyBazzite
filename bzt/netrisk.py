"""Shared risk classification for network connections.

Used by both the live auditor and the logging daemon so the two can never
disagree about what counts as risky.
"""

from __future__ import annotations

UNSAFE_PORTS = {
    21: "FTP (credentials sent in clear text)",
    23: "Telnet (unencrypted remote shell)",
    80: "HTTP (unencrypted web traffic)",
    137: "NetBIOS name service",
    138: "NetBIOS datagram service",
    139: "NetBIOS session service",
    445: "SMB file sharing",
    1900: "UPnP/SSDP (amplification vector)",
    3389: "RDP (brute-force target)",
}

CRITICAL_PORTS = {23, 445, 3389}

SECURE, WARNING, DANGER = "SECURE", "WARNING", "DANGER"


def assess(status: str, local_ip: str, local_port, remote_port, app: str):
    """Return (verdict, reason) for one socket.

    Listening sockets are judged on the port they expose locally; established
    sockets on the remote port they talk to. The original code only ever looked
    at the remote port, so a machine listening on SMB/445 was rated 'secure'.
    """
    if status == "LISTEN":
        wide_open = local_ip in ("0.0.0.0", "::", "*")
        if local_port in CRITICAL_PORTS and wide_open:
            return DANGER, f"{UNSAFE_PORTS.get(local_port, 'service')} open to the network"
        if local_port in UNSAFE_PORTS and wide_open:
            return WARNING, f"exposes {UNSAFE_PORTS[local_port]}"
        if wide_open:
            return WARNING, f"{app} listening on every interface"
        return SECURE, "bound to a local interface"

    if isinstance(remote_port, int):
        if remote_port in CRITICAL_PORTS:
            return DANGER, f"connected over {UNSAFE_PORTS.get(remote_port, 'a risky port')}"
        if remote_port in UNSAFE_PORTS:
            return WARNING, UNSAFE_PORTS[remote_port]
    return SECURE, "encrypted or standard port"
