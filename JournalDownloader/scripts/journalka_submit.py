"""Preview and submit one homework file through the Journalka API."""

import argparse
import asyncio
from getpass import getpass
from hashlib import sha256
import json
import os
from pathlib import Path

import httpx

from download_top_homework import BASE_URL, as_dict, fetch_tasks, group_ids_from_response, login, request_json

TASKS = "/api/dashboard/tasks"
KINDS = {"homework": 0, "lab": 1}


async def execute(args, client):
    file = args.file.resolve(strict=True)
    content = file.read_bytes()
    if not content:
        raise ValueError("Файл пуст.")
    groups = group_ids_from_response(await request_json(client, f"{TASKS}/groups"))
    matches = []
    for group in groups:
        for item in await fetch_tasks(client, group, KINDS[args.kind], (0, 3)):
            if str(item.get("id")) == args.id:
                matches.append((group, item))
    if len(matches) != 1:
        raise ValueError("Нужна ровно одна текущая или просроченная карточка с указанным ID.")
    group, item = matches[0]
    deadline = str(item.get("deadlineAt") or item.get("overdueAt") or "")[:10]
    if deadline != args.deadline:
        raise ValueError(f"Срок в журнале ({deadline or 'не указан'}) не совпадает с указанным.")
    work_id = str(item.get("teacherWorkId", ""))
    if not work_id.isascii() or not work_id.isdecimal():
        raise ValueError("Журнал не вернул корректный идентификатор для сдачи.")
    if as_dict(item.get("studentSubmission")).get("id") is not None:
        raise ValueError("У карточки уже есть отправленная работа.")
    plan = {
        "id": args.id, "teacherWorkId": work_id, "group": group, "kind": args.kind,
        "subject": item.get("subjectName"), "title": item.get("title"), "deadline": deadline,
        "status": item["status"], "file": str(file), "sha256": sha256(content).hexdigest(),
    }
    token = sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest()
    if not args.confirm:
        print(json.dumps({**plan, "confirmation": token}, ensure_ascii=False, indent=2))
        return
    if args.confirm != token:
        raise ValueError("Карточка или файл изменились. Повторите предварительную проверку.")

    data = {
        "action": "submit", "id": work_id, "answerText": "",
        "spentTimeHour": "00", "spentTimeMin": "00", "evaluationMark": "0",
        "evaluationTags": "[]", "evaluationComment": "",
    }
    try:
        response = await client.post(
            TASKS, data=data, files={"file": (file.name, content)}, follow_redirects=False,
        )
    except httpx.TransportError:
        response = None

    try:
        submitted = await fetch_tasks(client, group, KINDS[args.kind], (1, 2))
    except (RuntimeError, httpx.HTTPError) as error:
        raise RuntimeError("Не удалось проверить карточку после запроса. Проверьте журнал перед повтором.") from error
    for result in submitted:
        submission = as_dict(result.get("studentSubmission"))
        if str(result.get("id")) == args.id and submission.get("fileName") == file.name:
            print(json.dumps({"id": args.id, "file": file.name, "status": "verified"}, ensure_ascii=False))
            return
    detail = f"HTTP {response.status_code}" if response is not None else "ответ не получен"
    raise RuntimeError(f"Сдача не подтверждена ({detail}). Проверьте карточку перед повтором.")


async def run(args):
    username = os.environ.get("TOP_ACADEMY_USERNAME")
    password = os.environ.get("TOP_ACADEMY_PASSWORD")
    if not (username and password):
        from journalka_credentials import read_account

        try:
            saved = read_account()
        except Exception:
            saved = None
        if saved:
            username, password = saved
        else:
            username = input("Логин журнала: ").strip()
            password = getpass("Пароль журнала: ")
    async with httpx.AsyncClient(
        base_url=BASE_URL, follow_redirects=False,
        headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0"},
        timeout=httpx.Timeout(120, connect=30),
    ) as client:
        await login(client, username, password)
        await execute(args, client)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--id", required=True, help="ID карточки, например hw-12345")
    parser.add_argument("--kind", required=True, choices=KINDS)
    parser.add_argument("--deadline", required=True, help="Срок из журнала в формате ГГГГ-ММ-ДД")
    parser.add_argument("--file", required=True, type=Path)
    parser.add_argument("--confirm", help="Код из предварительной проверки")
    args = parser.parse_args()
    try:
        asyncio.run(run(args))
    except (OSError, ValueError, RuntimeError, httpx.HTTPError) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
