"""Общее локальное хранение учётной записи Journalka."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
from tempfile import NamedTemporaryFile


SERVICE = "Journalka Downloader"
ACCOUNT_FILE = "journalka-account.json"


def account_path() -> Path:
    """Хранить учётку рядом со сборками или в корне проекта при запуске исходника."""
    if getattr(sys, "frozen", False):
        directory = Path(sys.executable).resolve().parent
    else:
        directory = Path(__file__).resolve().parent.parent
    return directory / ACCOUNT_FILE


def read_account() -> tuple[str, str] | None:
    try:
        saved = account_path().read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    try:
        data = json.loads(saved)
        username, password = data["username"], data["password"]
    except (KeyError, TypeError, ValueError):
        return None
    if isinstance(username, str) and isinstance(password, str) and username and password:
        return username, password
    return None


def save_account(username: str, password: str) -> None:
    destination = account_path()
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"username": username, "password": password}, ensure_ascii=False)
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=destination.parent, delete=False) as temp:
        temporary = Path(temp.name)
        temp.write(payload)
    try:
        if os.name != "nt":
            temporary.chmod(0o600)
        temporary.replace(destination)
        if os.name != "nt":
            destination.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)


def delete_account() -> None:
    account_path().unlink(missing_ok=True)
