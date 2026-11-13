import json
from pathlib import Path
import ssl
import sys
from urllib.parse import urlsplit

import httpx


CONFIG = "journalka-report.json"


def report_config():
    locations = [Path(__file__).with_name(CONFIG)]
    if getattr(sys, "frozen", False):
        locations.append(Path(sys.executable).with_name(CONFIG))
    for path in locations:
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            url = urlsplit(data["url"])
            if url.scheme != "https" or not url.hostname or url.username or url.password or url.path != "/usage" or url.query or url.fragment:
                return None
            context = ssl.create_default_context(cadata=data["certificate"])
            return url.geturl(), url.netloc, context
        except (OSError, TypeError, KeyError, ValueError):
            return None
    return None


def send_login(config, login):
    url, _, context = config
    try:
        with httpx.Client(verify=context, timeout=5, follow_redirects=False, trust_env=False) as client:
            return client.post(url, json={"login": login}).status_code == 204
    except httpx.HTTPError:
        return False
