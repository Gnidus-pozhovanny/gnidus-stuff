#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.13"
# dependencies = ["httpx>=0.28,<1"]
# ///
"""Черновик сдачи: контракт загрузки Journalka ещё не подтверждён."""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import sys
from tempfile import NamedTemporaryFile
from typing import Any

import httpx

from download_top_homework import (
    BASE_URL,
    STATUS_NAMES,
    TASK_TYPES,
    as_dict,
    error_description,
    fetch_tasks,
    group_ids_from_response,
    login,
    plain_text,
    request_json,
)
from journalka_credentials import account_path, read_account


PLAN_VERSION = 1
SUBMITTABLE_STATUSES = {0, 3}
SNAPSHOT_FIELDS = ("subject", "task_type", "title", "deadline", "group", "status", "task_id")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as temp:
        temporary = Path(temp.name)
        json.dump(value, temp, ensure_ascii=False, indent=2)
        temp.write("\n")
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise RuntimeError(f"Не найден файл: {path}") from None
    except json.JSONDecodeError as error:
        raise RuntimeError(f"Неверный JSON в {path}: строка {error.lineno}") from None


def credentials() -> tuple[str, str]:
    account = read_account()
    if not account:
        raise RuntimeError(
            f"Учётка не сохранена. Запустите «Окно авторизации»; ожидаемый файл: {account_path()}"
        )
    return account


def task_snapshot(item: dict[str, Any], task_type: int, group_id: int | None) -> dict[str, Any]:
    try:
        status = int(item.get("status"))
    except (TypeError, ValueError):
        status = -1
    task_id = item.get("id") or item.get("teacherWorkId") or item.get("exportId")
    return {
        "subject": plain_text(item.get("subjectName")),
        "task_type": task_type,
        "task_type_name": TASK_TYPES.get(task_type, str(task_type)),
        "title": plain_text(item.get("title")),
        "deadline": str(item.get("deadlineAt") or item.get("overdueAt") or "")[:10],
        "group": plain_text(item.get("groupName")) or group_id,
        "group_id": group_id,
        "status": status,
        "status_name": STATUS_NAMES.get(status, "Не указан"),
        "task_id": str(task_id),
        "teacher_work_id": item.get("teacherWorkId"),
    }


async def current_tasks(client: httpx.AsyncClient) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    groups = group_ids_from_response(await request_json(client, "/api/dashboard/tasks/groups"))
    result: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for group_id in groups:
        for task_type in TASK_TYPES:
            for item in await fetch_tasks(client, group_id, task_type, STATUS_NAMES):
                result.append((task_snapshot(item, task_type, group_id), item))
    return result


def resolve_task(
    tasks: list[tuple[dict[str, Any], dict[str, Any]]], task_id: str, task_type: int | None
) -> tuple[dict[str, Any], dict[str, Any]]:
    matches = [pair for pair in tasks if pair[0]["task_id"] == task_id]
    if task_type is not None:
        matches = [pair for pair in matches if pair[0]["task_type"] == task_type]
    if not matches:
        raise RuntimeError(f"Задание ID {task_id} не найдено в свежем списке")
    unique = {(pair[0]["task_type"], pair[0]["group_id"], pair[0]["task_id"]) for pair in matches}
    if len(unique) != 1:
        raise RuntimeError(f"ID {task_id} неоднозначен; укажите task_type и group в запросе")
    return matches[0]


def validate_expected(request: dict[str, Any], snapshot: dict[str, Any]) -> None:
    for field in ("subject", "title", "deadline", "group"):
        expected = request.get(field)
        if expected not in (None, "") and str(expected) != str(snapshot[field]):
            raise RuntimeError(
                f"ID {snapshot['task_id']}: поле {field} не совпало: "
                f"ожидалось {expected!r}, в журнале {snapshot[field]!r}"
            )
    if snapshot["status"] not in SUBMITTABLE_STATUSES:
        raise RuntimeError(
            f"ID {snapshot['task_id']} нельзя сдавать: статус {snapshot['status_name']}"
        )


async def prepare(request_path: Path, plan_path: Path) -> str:
    request = read_json(request_path)
    items = request.get("items") if isinstance(request, dict) else None
    if not isinstance(items, list) or not items:
        raise RuntimeError("В запросе нужен непустой массив items")
    username, password = credentials()
    headers = {"Accept": "application/json, text/plain, */*", "User-Agent": "Mozilla/5.0"}
    async with httpx.AsyncClient(base_url=BASE_URL, headers=headers, follow_redirects=True, timeout=120) as client:
        await login(client, username, password)
        tasks = await current_tasks(client)
    planned = []
    seen: set[tuple[str, int]] = set()
    for requested in items:
        if not isinstance(requested, dict):
            raise RuntimeError("Каждый элемент items должен быть объектом")
        task_id = str(requested.get("task_id") or "").strip()
        if not task_id:
            raise RuntimeError("У каждой пары нужен task_id")
        task_type_value = requested.get("task_type")
        task_type = int(task_type_value) if task_type_value is not None else None
        snapshot, _raw = resolve_task(tasks, task_id, task_type)
        validate_expected(requested, snapshot)
        key = (task_id, snapshot["task_type"])
        if key in seen:
            raise RuntimeError(f"ID {task_id} повторяется в запросе")
        seen.add(key)
        file = Path(str(requested.get("file") or "")).expanduser().resolve()
        if not file.is_file() or file.stat().st_size <= 0:
            raise RuntimeError(f"ID {task_id}: готовый файл не найден или пуст: {file}")
        planned.append({
            "task": snapshot,
            "file": str(file),
            "file_name": file.name,
            "file_size": file.stat().st_size,
            "file_sha256": sha256_file(file),
        })
    payload = {
        "version": PLAN_VERSION,
        "prepared_at": datetime.now(timezone.utc).isoformat(),
        "items": planned,
    }
    token = canonical_hash(payload)
    payload["confirmation_token"] = token
    write_json(plan_path, payload)
    return token


async def post_submission(client: httpx.AsyncClient, raw: dict[str, Any], file: Path) -> None:
    """Черновой запрос загрузки. Его поля требуют проверки."""
    task_id = raw.get("id") or raw.get("teacherWorkId")
    teacher_work_id = raw.get("teacherWorkId") or task_id
    content_type = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
    with file.open("rb") as source:
        response = await client.post(
            "/api/dashboard/tasks",
            data={"id": str(task_id), "teacherWorkId": str(teacher_work_id), "answerText": ""},
            files={"file": (file.name, source, content_type)},
        )
    if response.status_code == 401:
        raise RuntimeError("сессия Journalka закончилась перед отправкой")
    if not response.is_success:
        try:
            body = response.json()
        except ValueError:
            body = {}
        message = body.get("message") if isinstance(body, dict) else ""
        raise RuntimeError(f"Journalka отклонила файл: HTTP {response.status_code}{f': {message}' if message else ''}")


async def submit(plan_path: Path, confirmation: str) -> list[dict[str, Any]]:
    plan = read_json(plan_path)
    if not isinstance(plan, dict) or plan.get("version") != PLAN_VERSION or not isinstance(plan.get("items"), list):
        raise RuntimeError("Неверный формат плана сдачи")
    stored_token = str(plan.pop("confirmation_token", ""))
    actual_token = canonical_hash(plan)
    if not confirmation or confirmation != stored_token or confirmation != actual_token:
        raise RuntimeError("План изменился или не подтверждён; заново выполните prepare")
    for planned in plan["items"]:
        file = Path(planned["file"])
        if not file.is_file() or file.stat().st_size != planned["file_size"] or sha256_file(file) != planned["file_sha256"]:
            raise RuntimeError(f"Файл изменился после подтверждения: {file}")

    username, password = credentials()
    headers = {"Accept": "application/json, text/plain, */*", "User-Agent": "Mozilla/5.0"}
    results = []
    async with httpx.AsyncClient(base_url=BASE_URL, headers=headers, follow_redirects=True, timeout=120) as client:
        await login(client, username, password)
        for planned in plan["items"]:
            tasks = await current_tasks(client)
            expected = planned["task"]
            snapshot, raw = resolve_task(tasks, expected["task_id"], expected["task_type"])
            if any(snapshot[field] != expected[field] for field in SNAPSHOT_FIELDS):
                raise RuntimeError(f"Карточка ID {expected['task_id']} изменилась; заново выполните prepare")
            validate_expected(expected, snapshot)
            file = Path(planned["file"])
            await post_submission(client, raw, file)
            refreshed, _ = resolve_task(await current_tasks(client), expected["task_id"], expected["task_type"])
            accepted = refreshed["status"] in {1, 2}
            results.append({"task_id": expected["task_id"], "file": file.name, "accepted": accepted,
                            "status": refreshed["status_name"]})
            if not accepted:
                raise RuntimeError(
                    f"Journalka не подтвердила сдачу ID {expected['task_id']}: статус {refreshed['status_name']}"
                )
    return results


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Подготовить и отправить подтверждённые работы в Journalka.")
    commands = parser.add_subparsers(dest="command", required=True)
    prepare_parser = commands.add_parser("prepare", help="проверить пары и сохранить неизменяемый план")
    prepare_parser.add_argument("--request", type=Path, required=True)
    prepare_parser.add_argument("--plan", type=Path, required=True)
    submit_parser = commands.add_parser("submit", help="отправить подтверждённый план")
    submit_parser.add_argument("--plan", type=Path, required=True)
    submit_parser.add_argument("--confirm", required=True, help="токен из prepare после подтверждения пользователя")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.command == "submit":
        print("Отправка отключена до проверки контракта загрузки Journalka.", file=sys.stderr)
        return 1
    try:
        if args.command == "prepare":
            token = asyncio.run(prepare(args.request.resolve(), args.plan.resolve()))
            print(f"План сохранён: {args.plan.resolve()}")
            print(f"Токен подтверждения: {token}")
        else:
            results = asyncio.run(submit(args.plan.resolve(), args.confirm))
            for result in results:
                print(f"ID {result['task_id']} — {result['file']}: {result['status']}")
    except KeyboardInterrupt:
        print("Остановлено пользователем", file=sys.stderr)
        return 130
    except Exception as error:
        print(f"Ошибка: {error_description(error)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
