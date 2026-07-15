"""Клиент REST API Яндекс Трекера (v3) для планировщика.

Аутентификация: OAuth-токен + X-Org-ID (организация Яндекс 360).
Источники учётных данных, по приоритету:
1. переменные окружения YATRACKER_TOKEN_GT / YATRACKER_ORGID_GT
   (заведены пользователем на уровне Windows);
2. файл .env в корне проекта (строки "token = ..." / "org_id = ...").

Запись значений в задачи выполняется ТОЛЬКО из cli по флагу --apply.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import httpx

API_BASE = "https://api.tracker.yandex.net"

# Сентинел «поле не передано» — чтобы отличать «не трогать» от «очистить (None)».
UNSET: Any = object()

# Поля задач, нужные планировщику (кастомные ключи очереди ONE).
FIELD_ANALYST_ESTIMATE = "analystEstimate"
FIELD_DEVELOPER_ESTIMATE = "developerEstimate"
FIELD_TESTERS_ESTIMATE = "testersEstimate"
FIELD_BUSINESS_ESTIMATE = "businessEstimate"
FIELD_IMPORTANCE = "levelOfImportance"
FIELD_PLANNED_COMPLETION = "plannedCompletionDate"


class TrackerAuthError(RuntimeError):
    pass


def load_credentials(env_file: Path | None = None) -> tuple[str, str]:
    token = os.environ.get("YATRACKER_TOKEN_GT") or os.environ.get("TRACKER_TOKEN")
    org_id = os.environ.get("YATRACKER_ORGID_GT") or os.environ.get("TRACKER_ORG_ID")
    if (not token or not org_id) and env_file and env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip().lower(), value.strip()
            if key == "token" and not token:
                token = value
            elif key == "org_id" and not org_id:
                org_id = value
    if not token or not org_id:
        raise TrackerAuthError(
            "Не найдены учётные данные Трекера: задайте YATRACKER_TOKEN_GT/"
            "YATRACKER_ORGID_GT или .env (token=, org_id=)."
        )
    return token, org_id


@dataclass
class Issue:
    """Задача Трекера в объёме, нужном планировщику."""
    key: str
    summary: str
    status: str                       # display
    importance: int | None
    analyst_estimate: float | None    # часы
    developer_estimate: float | None  # часы
    components: list[str]
    direction: str | None             # локальное поле «Направление»
    start: date | None
    end: date | None
    deadline: date | None
    planned_completion: date | None
    issue_type: str
    raw: dict[str, Any]


def _parse_date(value: Any) -> date | None:
    if not value:
        return None
    return date.fromisoformat(str(value)[:10])


def _direction(data: dict[str, Any]) -> str | None:
    # Локальное поле очереди приходит с префиксом вида
    # "67526ccca21d8e1e6a7ad937--direction" — ищем по суффиксу.
    for key, value in data.items():
        if key.endswith("--direction") and value:
            return str(value)
    return None


def _to_issue(data: dict[str, Any]) -> Issue:
    return Issue(
        key=data["key"],
        summary=data.get("summary", ""),
        status=(data.get("status") or {}).get("display", "?"),
        importance=data.get(FIELD_IMPORTANCE),
        analyst_estimate=data.get(FIELD_ANALYST_ESTIMATE),
        developer_estimate=data.get(FIELD_DEVELOPER_ESTIMATE),
        components=[c.get("display", "") for c in data.get("components") or []],
        direction=_direction(data),
        start=_parse_date(data.get("start")),
        end=_parse_date(data.get("end")),
        deadline=_parse_date(data.get("deadline")),
        planned_completion=_parse_date(data.get(FIELD_PLANNED_COMPLETION)),
        issue_type=(data.get("type") or {}).get("display", "?"),
        raw=data,
    )


class TrackerClient:
    def __init__(self, token: str, org_id: str, timeout: float = 60.0):
        self._client = httpx.Client(
            base_url=API_BASE,
            headers={
                "Authorization": f"OAuth {token}",
                "X-Org-ID": org_id,
                "Content-Type": "application/json",
            },
            timeout=timeout,
        )

    def close(self) -> None:
        self._client.close()

    def _request_with_retry(self, method: str, url: str, *, max_retries: int = 5, **kwargs) -> httpx.Response:
        """Запрос с повторами при 429/5xx (Retry-After либо экспоненциальная пауза)."""
        delay = 2.0
        for attempt in range(max_retries + 1):
            resp = self._client.request(method, url, **kwargs)
            if resp.status_code not in (429, 500, 502, 503, 504) or attempt == max_retries:
                resp.raise_for_status()
                return resp
            retry_after = resp.headers.get("Retry-After")
            wait = float(retry_after) if retry_after and retry_after.isdigit() else delay
            time.sleep(wait)
            delay = min(delay * 2, 30.0)
        raise AssertionError("unreachable")

    def search_issues(self, query: str, per_page: int = 100) -> list[Issue]:
        """Все страницы поиска по языку запросов Трекера."""
        issues: list[Issue] = []
        page = 1
        while True:
            resp = self._request_with_retry(
                "POST",
                "/v3/issues/_search",
                params={"perPage": per_page, "page": page},
                json={"query": query},
            )
            batch = resp.json()
            issues.extend(_to_issue(item) for item in batch)
            total_pages = int(resp.headers.get("X-Total-Pages", "1"))
            if page >= total_pages:
                break
            page += 1
            time.sleep(0.3)  # бережём лимиты API
        return issues

    def get_issue(self, key: str) -> dict[str, Any]:
        resp = self._request_with_retry("GET", f"/v3/issues/{key}")
        return resp.json()

    def fetch_current_by_keys(self, keys: list[str], chunk: int = 50) -> list[Issue]:
        """Свежие значения задач по списку ключей (чанки, поиск по Key).

        Используется перед apply для точного снимка ровно тех задач, что
        будут изменены — на момент записи, а не на момент расчёта плана.
        """
        result: list[Issue] = []
        for i in range(0, len(keys), chunk):
            part = keys[i : i + chunk]
            values = ",".join(f'"{k}"' for k in part)
            result.extend(self.search_issues(f"Key: {values}"))
        return result

    def update_issue_dates(
        self,
        key: str,
        start: date | None | object = UNSET,
        end: date | None | object = UNSET,
        planned_completion: date | None | object = UNSET,
    ) -> dict[str, Any]:
        """Запись плановых дат в задачу.

        Для каждого поля:
          - date       -> установить значение;
          - None       -> очистить поле в Трекере (PATCH null);
          - _UNSET     -> не трогать поле.
        Разделение None и _UNSET критично для restore: восстановление
        должно уметь вернуть поле в исходно пустое состояние.
        """
        payload: dict[str, Any] = {}
        for field_key, value in (
            ("start", start),
            ("end", end),
            (FIELD_PLANNED_COMPLETION, planned_completion),
        ):
            if value is UNSET:
                continue
            payload[field_key] = value.isoformat() if isinstance(value, date) else None
        if not payload:
            return {}
        resp = self._request_with_retry("PATCH", f"/v3/issues/{key}", json=payload)
        return resp.json()


def open_client(env_file: Path | None = None) -> TrackerClient:
    token, org_id = load_credentials(env_file)
    return TrackerClient(token, org_id)
