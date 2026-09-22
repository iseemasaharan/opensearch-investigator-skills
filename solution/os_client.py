"""
Shared OpenSearch client factory.
Reads connection config from environment variables:

  OPENSEARCH_URL   https://<user>:<password>@<host>:<port>
  OPENSEARCH_SSL_HOSTNAME   (optional) hostname for SSL cert verification
                            defaults to host with 'load-balancer.' stripped
"""
import os
import re
from urllib.parse import unquote

from opensearchpy import OpenSearch


def _parse_url(raw: str):
    """
    Parse OPENSEARCH_URL robustly.
    urlparse mis-splits when the password contains '@'.
    This parser uses the LAST '@' as the credentials/host boundary.
    """
    raw = raw.rstrip("/")

    # Extract scheme
    m = re.match(r'^(https?)://', raw)
    scheme = m.group(1) if m else "http"
    rest = raw[len(scheme) + 3:]  # strip "scheme://"

    # Split credentials from host using the LAST '@'
    if "@" in rest:
        at_idx = rest.rfind("@")
        creds = rest[:at_idx]
        hostpart = rest[at_idx + 1:]
        # credentials: split on first ':' only
        if ":" in creds:
            colon = creds.index(":")
            username = unquote(creds[:colon])
            password = unquote(creds[colon + 1:])
        else:
            username = unquote(creds)
            password = ""
        auth = (username, password)
    else:
        hostpart = rest
        auth = None

    # Split host and port
    if hostpart.startswith("["):  # IPv6
        bracket = hostpart.index("]")
        host = hostpart[1:bracket]
        port_str = hostpart[bracket + 2:] if hostpart[bracket + 1:bracket + 2] == ":" else ""
    elif ":" in hostpart:
        host, port_str = hostpart.rsplit(":", 1)
    else:
        host, port_str = hostpart, ""

    port = int(port_str) if port_str.isdigit() else (443 if scheme == "https" else 9200)

    return scheme, host, port, auth


def get_client() -> OpenSearch:
    raw = os.environ.get("OPENSEARCH_URL", "http://localhost:9200")
    scheme, host, port, auth = _parse_url(raw)

    use_ssl = scheme == "https"

    ssl_hostname = os.environ.get(
        "OPENSEARCH_SSL_HOSTNAME",
        host.replace("load-balancer.", "") if host else host,
    )

    kwargs = dict(
        hosts=[{"host": host, "port": port}],
        http_compress=True,
        use_ssl=use_ssl,
        verify_certs=use_ssl,
        ssl_assert_hostname=ssl_hostname if use_ssl else False,
        ssl_show_warn=False,
    )
    if auth:
        kwargs["http_auth"] = auth

    return OpenSearch(**kwargs)
