#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.13"
# dependencies = ["httpx>=0.28,<1"]
# ///
"""Скачать домашние и лабораторные работы через Journalka."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Callable, Iterable
from contextvars import ContextVar
from datetime import date
from email.message import Message
from email.utils import parsedate_to_datetime
import html
import mimetypes
import os
import re
import sys
from tempfile import NamedTemporaryFile
import time
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urljoin, urlparse

import httpx

BASE_URL = "https://journalka.top-academy.ru"
TASK_TYPES = {0: "Домашние_задания", 1: "Лабораторные_работы"}
STATUSES = (0, 3, 5)
STATUS_FOLDERS = {0: "Просроченные", 1: "Выполненные", 2: "На проверке", 3: "Текущие", 5: "Удалённые"}
STATUS_NAMES = {0: "Просрочено", 1: "Выполнено", 2: "На проверке", 3: "Текущее", 5: "Удалено преподавателем"}
TASK_LIST_NAME = "Список заданий Journalka.md"
MAX_PAGES = 150
MIN_REQUEST_INTERVAL = 1.25
MAX_RATE_LIMIT_RETRIES = 8
_last_request_at = ContextVar("last_request_at", default=0.0)
_report: ContextVar[Callable[[str], None]] = ContextVar("report", default=print)


def error_description(error: Exception) -> str:
    if isinstance(error, httpx.TimeoutException):
        return f"{type(error).__name__}: сервер не ответил за отведённое время"
    return f"{type(error).__name__}: {str(error).strip() or 'без описания'}"


async def paced_request(
    client: httpx.AsyncClient, method: str, url: str, **kwargs: Any
) -> httpx.Response:
    """Ограничивать частоту запросов и повторять временные сетевые сбои."""
    for attempt in range(MAX_RATE_LIMIT_RETRIES + 1):
        delay = MIN_REQUEST_INTERVAL - (time.monotonic() - _last_request_at.get())
        if delay > 0:
            await asyncio.sleep(delay)
        try:
            response = await client.request(method, url, **kwargs)
        except httpx.TransportError as error:
            _last_request_at.set(time.monotonic())
            endpoint = urlparse(url).path
            # Повтор POST-входа может создавать дополнительные сессии и лимиты.
            if method.upper() != "GET" or attempt >= 3:
                raise RuntimeError(
                    f"{method} {endpoint}: {error_description(error)}. Повторите запуск позже."
                ) from None
            wait_seconds = 10 * (attempt + 1)
            _report.get()(
                f"{method} {endpoint}: {error_description(error)}. "
                f"Повтор через {wait_seconds} с..."
            )
            await asyncio.sleep(wait_seconds)
            continue
        _last_request_at.set(time.monotonic())
        if response.status_code != 429:
            return response
        if attempt >= MAX_RATE_LIMIT_RETRIES:
            return response
        try:
            retry_after = float(response.headers.get("Retry-After", ""))
        except ValueError:
            try:
                retry_after = parsedate_to_datetime(response.headers["Retry-After"]).timestamp() - time.time()
            except (ValueError, KeyError, TypeError, OverflowError):
                retry_after = 0
        wait_seconds = max(retry_after, min(15 * (attempt + 1), 120))
        _report.get()(
            f"Journalka ограничила частоту запросов. Повтор через {wait_seconds:.0f} с."
        )
        await asyncio.sleep(wait_seconds)
    raise AssertionError("Недостижимый код")


def safe_name(value: Any, fallback: str, limit: int = 120) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "_", text)
    text = re.sub(r"\s+", " ", text).strip(" ._")
    text = (text or fallback)[:limit].encode("utf-8")[:210].decode("utf-8", errors="ignore").rstrip(" ._")
    if re.fullmatch(r"(?i:CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])", text.split(".")[0]):
        text = f"_{text}"
    return text or fallback


def plain_text(value: Any) -> str:
    if not value:
        return ""
    text = str(value)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</(?:p|div|li|h[1-6])>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text).replace("\xa0", " ")
    return "\n".join(line.rstrip() for line in text.splitlines()).strip()


def as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def unique_path(directory: Path, filename: str) -> Path:
    candidate = directory / safe_name(filename, "вложение", 180)
    if not candidate.exists():
        return candidate
    for number in range(2, 10_000):
        alternative = candidate.with_name(f"{candidate.stem}_{number}{candidate.suffix}")
        if not alternative.exists():
            return alternative
    raise RuntimeError(f"Не удалось подобрать имя для {candidate}")


def filename_from_url(url: str, fallback: str) -> str:
    return safe_name(unquote(Path(urlparse(url).path).name), fallback, 180)


async def request_json(client: httpx.AsyncClient, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    response = await paced_request(client, "GET", path, params=params)
    if response.status_code == 401:
        raise RuntimeError("сессия Journalka закончилась; выполните вход заново")
    if not response.is_success:
        message = ""
        try:
            body = response.json()
            if isinstance(body, dict):
                message = str(body.get("message") or body.get("error") or "")
        except ValueError:
            pass
        raise RuntimeError(f"Journalka вернула HTTP {response.status_code}{f': {message}' if message else ''}")
    body = response.json()
    if not isinstance(body, dict):
        raise RuntimeError(f"Неверный ответ Journalka для {path}")
    return body


async def login(client: httpx.AsyncClient, username: str, password: str) -> None:
    response = await paced_request(
        client,
        "POST",
        "/api/auth/login",
        json={"cityId": None, "username": username, "password": password},
        follow_redirects=False,
    )
    if response.is_success:
        return
    hint = " Проверьте логин и пароль." if response.status_code in (400, 401, 403) else ""
    raise RuntimeError(f"Journalka отклонила вход (HTTP {response.status_code}).{hint}")


def group_ids_from_response(body: dict[str, Any]) -> list[int | None]:
    groups: list[dict[str, Any]] = []
    selected = as_dict(body.get("selectedGroup"))
    if selected:
        groups.append(selected)
    groups.extend(as_dict(item) for item in as_list(body.get("items")))
    groups.extend(as_dict(item) for item in as_list(body.get("groups")))
    ids: list[int | None] = []
    for group in groups:
        try:
            group_id = int(group.get("id"))
        except (TypeError, ValueError):
            continue
        if group_id not in ids:
            ids.append(group_id)
    return ids or [None]


async def fetch_tasks(client: httpx.AsyncClient, group_id: int | None, task_type: int, statuses: Iterable[int] = STATUSES) -> list[dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for status in statuses:
        previous_ids: tuple[str, ...] | None = None
        for page in range(1, MAX_PAGES + 1):
            params: dict[str, Any] = {"type": task_type, "page": page, "status": status}
            if group_id is not None:
                params["groupId"] = group_id
            body = await request_json(client, "/api/dashboard/tasks/list", params)
            items = [as_dict(item) for item in as_list(body.get("items"))]
            items = [item for item in items if item]
            if not items:
                break
            ids = tuple(str(item.get("id") or item.get("teacherWorkId") or "") for item in items)
            if ids == previous_ids:
                break
            previous_ids = ids
            for index, item in enumerate(items):
                if str(item.get("status", status)) != str(status):
                    continue
                key = str(item.get("id") or item.get("teacherWorkId") or f"{status}-{page}-{index}")
                found[key] = {**item, "status": status, "exportId": key}
        else:
            raise RuntimeError(f"Слишком много страниц: тип {task_type}, статус {status}")
    return list(found.values())


async def download_file(client: httpx.AsyncClient, url: str, directory: Path, preferred_name: str, prefix: str) -> Path | None:
    if not url:
        return None
    absolute_url = urljoin(f"{BASE_URL}/", url)
    if urlparse(absolute_url).scheme != "https":
        _report.get()("Пропущено вложение по незащищённой ссылке.")
        return None
    filename = f"{prefix}{preferred_name or 'вложение'}"
    existing = directory / safe_name(filename, "вложение", 180)
    if existing.is_file() and existing.stat().st_size > 0:
        return existing
    try:
        response = await paced_request(client, "GET", absolute_url, follow_redirects=True)
        response.raise_for_status()
    except (httpx.HTTPError, RuntimeError) as error:
        detail = f"HTTP {error.response.status_code}" if isinstance(error, httpx.HTTPStatusError) else type(error).__name__
        _report.get()(f"Не скачался файл {filename}: {detail}")
        return None
    if not response.content or response.headers.get("Content-Type", "").startswith("text/html") and not preferred_name.lower().endswith((".html", ".htm")):
        _report.get()(f"Вместо файла {filename} пришёл пустой ответ или страница входа.")
        return None
    metadata = Message()
    metadata["Content-Disposition"] = response.headers.get("Content-Disposition", "")
    remote_name = metadata.get_filename() or filename_from_url(str(response.url), "вложение")
    remote_suffix = Path(remote_name).suffix
    suffix = Path(preferred_name).suffix
    if not suffix:
        suffix = remote_suffix or mimetypes.guess_extension(response.headers.get("Content-Type", "").split(";")[0]) or ""
    name = preferred_name or remote_name
    stem = name[:-len(Path(name).suffix)] if Path(name).suffix else name
    extension = re.sub(r"[^a-zA-Z0-9]", "", suffix)[:15]
    filename = safe_name(f"{prefix}{stem}", "вложение", 140)
    if extension:
        filename += f".{extension}"
    existing = directory / filename
    if existing.is_file() and existing.stat().st_size > 0:
        return existing
    target = unique_path(directory, filename)
    target.write_bytes(response.content)
    return target


def render_condition(item: dict[str, Any], task_type: int, files: list[tuple[str, Path]]) -> str:
    submission = as_dict(item.get("studentSubmission"))
    feedback = as_dict(item.get("teacherFeedback"))
    try:
        status = int(item.get("status"))
    except (TypeError, ValueError):
        status = -1
    lines = [
        f"# {plain_text(item.get('title')) or 'Задание'}", "",
        f"- Тип: {'Лабораторная работа' if task_type == 1 else 'Домашнее задание'}",
        f"- Статус: {STATUS_NAMES.get(status, 'Не указан')}",
        f"- Предмет: {item.get('subjectName') or 'не указан'}",
        f"- Преподаватель: {item.get('teacherName') or 'не указан'}",
        f"- Выдано: {item.get('createdAt') or 'не указано'}",
        f"- Срок: {item.get('deadlineAt') or item.get('overdueAt') or 'не указан'}",
        f"- ID задания: {item.get('id') or item.get('teacherWorkId') or 'не указан'}", "",
        "## Условие", "", plain_text(item.get("comment")) or "Условие в журнале не заполнено.", "",
        "## Комментарий преподавателя", "", plain_text(feedback.get("text")) or "Комментария преподавателя нет.",
    ]
    if files:
        lines.extend(["", "## Файлы", ""])
        lines.extend(f"- {label}: [{path.name}](<{path.name}>)" for label, path in files)
    if submission:
        mark = submission.get("mark")
        lines.extend(["", "## Сданная работа", "", f"- Оценка: {mark if mark not in (None, '') else 'не выставлена'}", f"- Ответ: {plain_text(submission.get('answerText')) or 'нет'}"])
    return "\n".join(lines).rstrip() + "\n"


async def save_task(client: httpx.AsyncClient, output_root: Path, item: dict[str, Any], task_type: int) -> tuple[Path, int, int]:
    subject = safe_name(item.get("subjectName"), "Предмет_не_указан")
    deadline_prefix = ""
    for field in ("deadlineAt", "overdueAt"):
        try:
            deadline = date.fromisoformat(str(item.get(field) or "")[:10])
        except ValueError:
            continue
        deadline_prefix = f"Срок - {deadline:%d.%m.%y} — "
        break
    title = safe_name(item.get("title"), "Задание")
    task_id = safe_name(item.get("exportId") or item.get("id") or item.get("teacherWorkId"), "без-id", 40)
    suffix = f" [id-{task_id}]"
    title_limit = 250 - len(f"{deadline_prefix}{suffix}".encode("utf-8"))
    title = title.encode("utf-8")[:title_limit].decode("utf-8", errors="ignore").rstrip()
    folder = f"{deadline_prefix}{title}{suffix}"
    status_folder = STATUS_FOLDERS.get(item.get("status"), "Статус не указан")
    directory = output_root / subject / status_folder / TASK_TYPES[task_type] / folder
    directory.mkdir(parents=True, exist_ok=True)
    files: list[tuple[str, Path]] = []

    file_path = str(item.get("filePath") or "")
    if file_path:
        params = httpx.QueryParams({"filePath": file_path, "locale": "ru", "id": item.get("teacherWorkId") or ""})
        assigned = await download_file(client, f"/api/dashboard/tasks/assignment?{params}", directory, str(item.get("fileName") or ""), "Задание — ")
        if assigned:
            files.append(("вложение к заданию", assigned))

    feedback = as_dict(item.get("teacherFeedback"))
    feedback_file = await download_file(client, str(feedback.get("attachmentPath") or ""), directory, str(feedback.get("attachmentName") or ""), "Комментарий преподавателя — ")
    if feedback_file:
        files.append(("вложение к комментарию преподавателя", feedback_file))

    submission = as_dict(item.get("studentSubmission"))
    submission_file = await download_file(client, str(submission.get("filePath") or ""), directory, str(submission.get("fileName") or ""), "Сданная работа — ")
    if submission_file:
        files.append(("сданная работа", submission_file))

    (directory / "Условие.md").write_text(render_condition(item, task_type, files), encoding="utf-8")
    expected = sum(bool(url) for url in (file_path, feedback.get("attachmentPath"), submission.get("filePath")))
    return directory, len(files), expected - len(files)


async def run(output_root: Path, current_group_only: bool) -> None:
    username = os.environ.get("TOP_ACADEMY_USERNAME")
    password = os.environ.get("TOP_ACADEMY_PASSWORD")
    if not username or not password:
        raise RuntimeError("Задайте TOP_ACADEMY_USERNAME и TOP_ACADEMY_PASSWORD")
    result = await download_tasks(output_root, username, password, {kind: STATUSES for kind in TASK_TYPES}, current_group_only=current_group_only, export_list=True)
    print(summary(*result))
    print(f"Папка: {output_root}")


def summary(tasks: int, files: int, failed: int) -> str:
    return f"Готово: заданий {tasks}, файлов {files}" + (f", не скачалось файлов: {failed}." if failed else ".")


def write_task_list(output_root: Path, ordered: list[tuple[tuple[int, str], dict[str, Any]]]) -> None:
    def cell(value: Any) -> str:
        cleaned = " ".join(plain_text(value).split())
        return re.sub(r"([\\|`*_\[\]<>!&])", r"\\\1", cleaned) or "—"

    lines = ["# Список заданий Journalka", ""]
    sections = (("Сдано", (1, 2)), ("Не сдано", (0, 3)), ("Удалено преподавателем", (5,)))
    for title, statuses in sections:
        rows = []
        for (task_type, task_id), item in ordered:
            if item["status"] not in statuses:
                continue
            row = (
                item.get("subjectName"),
                "Лаб" if task_type == 1 else "ДЗ",
                item.get("title"),
                str(item.get("deadlineAt") or item.get("overdueAt") or "")[:10],
                STATUS_NAMES[item["status"]],
                item.get("id") or item.get("teacherWorkId") or task_id,
            )
            rows.append("| " + " | ".join(map(cell, row)) + " |")
        lines.extend([f"## {title} ({len(rows)})", ""])
        if rows:
            lines.extend(["| Предмет | Тип | Задание | Срок | Статус | ID |",
                          "| --- | --- | --- | --- | --- | --- |", *rows, ""])
        else:
            lines.extend(["Нет заданий.", ""])
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=output_root, delete=False) as temp:
        temp_path = Path(temp.name)
        try:
            temp.write("\n".join(lines))
            temp.close()
            temp_path.replace(output_root / TASK_LIST_NAME)
        finally:
            temp.close()
            temp_path.unlink(missing_ok=True)


async def download_tasks(
    output_root: Path, username: str, password: str, selected: dict[int, Iterable[int]],
    report: Callable[[str], None] = print, on_login: Callable[[], None] = lambda: None,
    *, current_group_only: bool = False, export_list: bool = False,
    before_login: Callable[[], None] = lambda: None,
    on_progress: Callable[[str, int, int], None] = lambda stage, completed, total: None,
) -> tuple[int, int, int]:
    selected = {kind: tuple(statuses) for kind, statuses in selected.items()}
    if not export_list and not any(selected.values()):
        raise ValueError("Выберите хотя бы один статус заданий или выгрузку списка.")
    _report.set(report)
    _last_request_at.set(0.0)
    if sys.platform == "win32":
        path = str(output_root)
        if not path.startswith("\\\\?\\"):
            output_root = Path("\\\\?\\UNC\\" + path[2:] if path.startswith("\\\\") else "\\\\?\\" + path)
    output_root.mkdir(parents=True, exist_ok=True)
    headers = {"Accept": "application/json, text/plain, */*", "User-Agent": "Mozilla/5.0"}
    timeout = httpx.Timeout(120.0, connect=30.0)
    before_login()
    async with httpx.AsyncClient(base_url=BASE_URL, headers=headers, follow_redirects=True, timeout=timeout) as client:
        on_progress("Вход в журнал", 0, 1)
        report("Вход в Journalka…")
        await login(client, username, password)
        on_login()
        on_progress("Получение списка заданий", 0, 1)
        report("Получаю группы и список заданий…")
        group_ids = group_ids_from_response(await request_json(client, "/api/dashboard/tasks/groups"))
        if current_group_only:
            group_ids = group_ids[:1]
        all_tasks: dict[tuple[int, str], dict[str, Any]] = {}
        requested = {kind: tuple(STATUS_NAMES) for kind in TASK_TYPES} if export_list else selected
        for group_number, group_id in enumerate(group_ids, start=1):
            for task_type, statuses in requested.items():
                for item in await fetch_tasks(client, group_id, task_type, statuses):
                    task_id = item["exportId"]
                    all_tasks[(task_type, task_id)] = item
            on_progress("Получение списка заданий", group_number, len(group_ids))
        ordered = sorted(all_tasks.items(), key=lambda pair: (str(pair[1].get("subjectName") or ""), str(pair[1].get("createdAt") or ""), pair[0][1]))
        if export_list:
            on_progress("Сохранение списка", 0, 1)
            write_task_list(output_root, ordered)
            report(f"Список сохранён: {TASK_LIST_NAME}")
        to_download = [pair for pair in ordered if pair[1]["status"] in selected.get(pair[0][0], ())]
        file_count = 0
        failures = 0
        if to_download:
            on_progress("Скачивание заданий", 0, len(to_download))
        for number, ((task_type, _), item) in enumerate(to_download, start=1):
            report(f"[{number}/{len(to_download)}] {plain_text(item.get('title')) or 'Задание'}")
            _, downloaded, failed = await save_task(client, output_root, item, task_type)
            file_count += downloaded
            failures += failed
            on_progress("Скачивание заданий", number, len(to_download))
    return len(to_download), file_count, failures


def parse_args() -> argparse.Namespace:
    repository_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description="Скачать ДЗ, лабораторные, комментарии и вложения через Journalka.")
    parser.add_argument("--output", type=Path, default=repository_root / "Учёба", help="папка назначения")
    parser.add_argument("--current-group-only", action="store_true", help="только текущая группа")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        asyncio.run(run(args.output.resolve(), args.current_group_only))
    except KeyboardInterrupt:
        print("Остановлено пользователем", file=sys.stderr)
        return 130
    except Exception as error:
        print(f"Ошибка: {error_description(error)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
