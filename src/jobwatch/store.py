"""Persistence. DynamoDB in AWS (TABLE_NAME set), a local JSON file otherwise.

Every job ever seen is remembered (so it is fetched/decoded only once); only
matched life-science jobs keep full details and get `last_seen` refreshed.
Per-competitor run stats are stored under keys starting with "__run__#", and
the prospects/clients watchlist under "__watch__#".
"""

from __future__ import annotations

import json
import threading
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

from .config import env
from .models import Job

RUN_PREFIX = "__run__#"
WATCH_PREFIX = "__watch__#"


def _watch_key(name: str) -> str:
    return WATCH_PREFIX + name.strip().lower()


def _to_dynamo(obj: Any) -> Any:
    if isinstance(obj, float):
        return Decimal(str(obj))
    if isinstance(obj, dict):
        return {k: _to_dynamo(v) for k, v in obj.items() if v not in ("", None, [], {})}
    if isinstance(obj, list):
        return [_to_dynamo(v) for v in obj]
    return obj


def _from_dynamo(obj: Any) -> Any:
    if isinstance(obj, Decimal):
        return int(obj) if obj == obj.to_integral_value() else float(obj)
    if isinstance(obj, dict):
        return {k: _from_dynamo(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_from_dynamo(v) for v in obj]
    return obj


class DynamoStore:
    def __init__(self, table_name: str):
        import boto3
        self.table = boto3.resource("dynamodb").Table(table_name)

    def _scan(self, **kw) -> Iterable[dict]:
        while True:
            page = self.table.scan(**kw)
            yield from page.get("Items", [])
            if "LastEvaluatedKey" not in page:
                return
            kw["ExclusiveStartKey"] = page["LastEvaluatedKey"]

    def known_ids(self) -> set[str]:
        return {i["job_id"] for i in self._scan(ProjectionExpression="job_id")}

    def save(self, job: Job, matched: bool) -> None:
        if matched:
            item = {**job.to_dict(), "matched": True}
        else:  # remember it, but don't keep the body
            item = {"job_id": job.job_id, "competitor": job.competitor, "title": job.title,
                    "url": job.url, "first_seen": job.first_seen, "matched": False}
        self.table.put_item(Item=_to_dynamo(item))

    def touch(self, job_ids: Iterable[str], when: str) -> None:
        for jid in job_ids:
            self.table.update_item(Key={"job_id": jid}, UpdateExpression="SET last_seen = :t",
                                   ExpressionAttributeValues={":t": when})

    def matched_ids(self) -> set[str]:
        from boto3.dynamodb.conditions import Attr
        return {i["job_id"] for i in self._scan(ProjectionExpression="job_id", FilterExpression=Attr("matched").eq(True))}

    def get_job(self, job_id: str) -> dict | None:
        item = self.table.get_item(Key={"job_id": job_id}).get("Item")
        return _from_dynamo(item) if item else None

    def matched_jobs(self) -> list[dict]:
        from boto3.dynamodb.conditions import Attr
        return [_from_dynamo(i) for i in self._scan(FilterExpression=Attr("matched").eq(True))]

    def save_run(self, competitor: str, stats: dict) -> None:
        self.table.put_item(Item=_to_dynamo({"job_id": RUN_PREFIX + competitor, **stats}))

    def runs(self) -> list[dict]:
        from boto3.dynamodb.conditions import Attr
        return [_from_dynamo(i) for i in self._scan(FilterExpression=Attr("job_id").begins_with(RUN_PREFIX))]

    def watchlist(self) -> list[dict]:
        from boto3.dynamodb.conditions import Attr
        return [_from_dynamo(i) for i in self._scan(FilterExpression=Attr("job_id").begins_with(WATCH_PREFIX))]

    def save_watch(self, item: dict) -> None:
        self.table.put_item(Item=_to_dynamo({**item, "job_id": _watch_key(item["name"])}))

    def delete_watch(self, name: str) -> None:
        self.table.delete_item(Key={"job_id": _watch_key(name)})


class FileStore:
    """Single JSON file; for local runs and tests."""

    def __init__(self, path: str):
        self.path = Path(path)
        self._lock = threading.Lock()
        self.data: dict[str, dict] = json.loads(self.path.read_text()) if self.path.exists() else {}

    def _flush(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=1))

    def known_ids(self) -> set[str]:
        return {k for k in self.data if not k.startswith("__")}

    def save(self, job: Job, matched: bool) -> None:
        with self._lock:
            d = job.to_dict()
            self.data[job.job_id] = {**d, "matched": True} if matched else {
                k: d[k] for k in ("job_id", "competitor", "title", "url", "first_seen")} | {"matched": False}
            self._flush()

    def touch(self, job_ids: Iterable[str], when: str) -> None:
        with self._lock:
            for jid in job_ids:
                if jid in self.data:
                    self.data[jid]["last_seen"] = when
            self._flush()

    def matched_ids(self) -> set[str]:
        return {k for k, v in self.data.items() if v.get("matched")}

    def get_job(self, job_id: str) -> dict | None:
        return self.data.get(job_id)

    def matched_jobs(self) -> list[dict]:
        return [v for v in self.data.values() if v.get("matched")]

    def save_run(self, competitor: str, stats: dict) -> None:
        with self._lock:
            self.data[RUN_PREFIX + competitor] = {"job_id": RUN_PREFIX + competitor, **stats}
            self._flush()

    def runs(self) -> list[dict]:
        return [v for k, v in self.data.items() if k.startswith(RUN_PREFIX)]

    def watchlist(self) -> list[dict]:
        return [v for k, v in self.data.items() if k.startswith(WATCH_PREFIX)]

    def save_watch(self, item: dict) -> None:
        with self._lock:
            self.data[_watch_key(item["name"])] = {**item, "job_id": _watch_key(item["name"])}
            self._flush()

    def delete_watch(self, name: str) -> None:
        with self._lock:
            self.data.pop(_watch_key(name), None)
            self._flush()


def get_store():
    table = env("TABLE_NAME")
    return DynamoStore(table) if table else FileStore(env("JOBWATCH_DATA", "data/jobs.json"))
