"""Resumable Scholay journal collector; Python standard library only."""
import argparse
import datetime as dt
import gzip
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
import urllib.error
import urllib.request

BASE = "https://www.scholay.com/api/v1/public/journals/"


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


class APIError(Exception):
    def __init__(self, status, code, message):
        self.status, self.code = status, code
        super().__init__(message)


class Collector:
    def __init__(self, root, interval):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.interval, self.last_request = interval, 0.0
        self.rate_lock = threading.Lock()
        self.log_lock = threading.Lock()
        self.backoff_until = 0.0
        self.token = os.environ.get("SCHOLAY_BEARER_TOKEN", "")
        self.db = sqlite3.connect(root / "journals.sqlite3")
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS journals (
          id INTEGER PRIMARY KEY, canonical_name TEXT, list_json TEXT NOT NULL,
          detail_json TEXT, detail_status TEXT NOT NULL DEFAULT 'pending',
          fetched_at TEXT, error TEXT);
        CREATE TABLE IF NOT EXISTS journal_issns (
          journal_id INTEGER NOT NULL, issn TEXT NOT NULL,
          PRIMARY KEY(journal_id, issn));
        CREATE TABLE IF NOT EXISTS journal_sources (
          journal_id INTEGER NOT NULL, source TEXT NOT NULL, data_json TEXT,
          PRIMARY KEY(journal_id, source));
        CREATE TABLE IF NOT EXISTS checkpoint (key TEXT PRIMARY KEY, value TEXT);
        ''')

    def get(self, key, fallback=None):
        row = self.db.execute("SELECT value FROM checkpoint WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else fallback

    def put(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO checkpoint VALUES (?,?)", (key, encode(value)))

    def request(self, endpoint, payload, label):
        # Credentials are used only for this origin, never written to files/logs.
        headers = {"Content-Type": "application/json", "User-Agent": "ScholayJournalArchive/1.0"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        req = urllib.request.Request(BASE + endpoint, encode(payload).encode(), headers, method="POST")
        for attempt in range(4):
            with self.rate_lock:
                time.sleep(max(0, self.interval - (time.monotonic() - self.last_request),
                               self.backoff_until - time.monotonic()))
                self.last_request = time.monotonic()
            status, retry_after = None, None
            try:
                try:
                    with urllib.request.urlopen(req, timeout=60) as response:
                        status, body = response.status, response.read()
                except urllib.error.HTTPError as exc:
                    status, body = exc.code, exc.read()
                    retry_after = exc.headers.get("Retry-After")
            except (urllib.error.URLError, TimeoutError) as exc:
                if attempt == 3:
                    raise APIError(None, None, type(exc).__name__) from exc
                time.sleep(min(30, 2 ** (attempt + 1)))
                continue
            raw = self.root / "raw" / endpoint / (label + f"-{time.time_ns()}-attempt{attempt}.json.gz")
            raw.parent.mkdir(parents=True, exist_ok=True)
            temp = raw.with_suffix(raw.suffix + ".tmp")
            with gzip.open(temp, "wb") as output:
                output.write(body)
            temp.replace(raw)
            with self.log_lock, (self.root / "requests.jsonl").open("a", encoding="utf-8") as log:
                log.write(encode({"at": now(), "endpoint": endpoint, "request": payload,
                                  "http_status": status, "raw": str(raw.relative_to(self.root)),
                                  "sha256": hashlib.sha256(body).hexdigest()}) + "\n")
            try:
                obj = json.loads(body)
            except (ValueError, UnicodeError):
                obj = {"message": "Non-JSON response"}
            if status == 200 and obj.get("code") == 0:
                return obj["data"]
            if status == 429 or status >= 500 or (status == 400 and obj.get("code") == 4030):
                if attempt < 3:
                    delay = min(60, float(retry_after)) if retry_after and retry_after.isdigit() else 5 * 2 ** attempt
                    if status == 429:
                        with self.rate_lock:
                            self.interval = min(10.0, self.interval * 2)
                            self.backoff_until = max(self.backoff_until, time.monotonic() + delay)
                    time.sleep(delay)
                    continue
            raise APIError(status, obj.get("code"), obj.get("message", "Request failed"))
        raise APIError(status, None, "Retry limit reached")

    def lists(self):
        if self.get("list_exhausted", False):
            return
        page = self.get("next_page", 1)
        while True:
            try:
                data = self.request("search", {"type": "journal", "page": page, "page_size": self.get('page_size', 20),
                                                "sort_by": "id", "sort_dir": "asc"}, f"page-{page:06}")
            except APIError as exc:
                self.put("list_blocker", {"at": now(), "page": page, "status": exc.status,
                                          "code": exc.code, "message": str(exc)})
                self.db.commit()
                self.report()
                print(f"List stopped at page {page}: {exc}", flush=True)
                return
            rows = data.get("rows")
            if not isinstance(rows, list):
                raise ValueError("Search response missing rows array")
            ids = [row.get("unified", {}).get("id") for row in rows]
            if any(not isinstance(i, int) or i <= 0 for i in ids) or len(set(ids)) != len(ids):
                raise ValueError("Invalid or duplicate IDs in response")
            if ids and (ids != sorted(ids) or ids[0] <= self.get("last_id", 0)):
                raise ValueError("Unstable ID order; collection stopped to avoid omissions")
            with self.db:
                for row in rows:
                    unified = row["unified"]
                    self.db.execute('''INSERT INTO journals(id, canonical_name, list_json)
                      VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET
                      canonical_name=excluded.canonical_name, list_json=excluded.list_json''',
                      (unified["id"], unified.get("canonical_name"), encode(row)))
                self.put("next_page", page + 1)
                self.put("list_blocker", None)
                if ids:
                    self.put("last_id", ids[-1])
                meta = data.get("pageMeta", {})
                truncated = data.get("truncated", False) or meta.get("truncated", False)
                more = data.get("hasMore", meta.get("has_more"))
                if truncated or more is None or (more and not rows):
                    self.put("list_blocker", {"message": "Truncated or ambiguous pagination", "page": page})
                    self.put("next_page", page)
                    # Discard this page so a retry cannot skip records.
                    raise ValueError("Truncated or ambiguous pagination")
                if not more:
                    self.put("list_exhausted", True)
            print(f"List page {page}: {len(rows)} rows", flush=True)
            if page % 25 == 0 or not more:
                self.report()
            if not more:
                return
            page += 1

    def details(self):
        ids = [r[0] for r in self.db.execute("SELECT id FROM journals WHERE detail_status != 'ok' ORDER BY id")]
        self.put("detail_blocker", None)
        self.db.commit()
        for position, journal_id in enumerate(ids, 1):
            try:
                data = self.request("get", {"id": journal_id}, str(journal_id))
                unified = data.get("unified", {})
                if unified.get("id") != journal_id:
                    raise ValueError("Detail identity mismatch")
                with self.db:
                    self.db.execute("UPDATE journals SET detail_json=?, detail_status='ok', fetched_at=?, error=NULL WHERE id=?",
                                    (encode(data), now(), journal_id))
                    issns = []
                    for field in ("issn_l", "print_issn", "electronic_issn"):
                        issns.extend(str(unified.get(field) or "").split(","))
                    issns.extend(x for x in unified.get("all_issns", []) if isinstance(x, str))
                    for issn in set(x.strip() for x in issns if x.strip()):
                        self.db.execute("INSERT OR IGNORE INTO journal_issns VALUES (?,?)", (journal_id, issn))
                    sources = data.get("sources", {})
                    if isinstance(sources, dict):
                        for source, value in sources.items():
                            self.db.execute("INSERT OR REPLACE INTO journal_sources VALUES (?,?,?)",
                                            (journal_id, source, encode(value)))
            except APIError as exc:
                with self.db:
                    self.db.execute("UPDATE journals SET detail_status='error', error=? WHERE id=?", (str(exc), journal_id))
                    if exc.status in (401, 403, 429) or exc.code == 4036:
                        self.put("detail_blocker", {"id": journal_id, "message": str(exc), "status": exc.status})
                if exc.status in (401, 403, 429) or exc.code == 4036:
                    break
            if position % 10 == 0 or position == len(ids):
                print(f"Details: {position}/{len(ids)} processed", flush=True)
                self.report()

    def report(self):
        count, details = self.db.execute("SELECT COUNT(*), COALESCE(SUM(detail_status='ok'),0) FROM journals").fetchone()
        baseline = self.get("stats_start", {})
        ending = self.get("stats_end", {})
        expected = baseline.get("totalJournals")
        complete = bool(expected is not None and count == expected and details == count
                        and self.get("list_exhausted", False)
                        and ending.get("totalJournals") == expected
                        and not self.get("list_blocker") and not self.get("detail_blocker"))
        report = {"updated_at": now(), "complete": complete,
                  "scope": "Journal search rows and public detail JSON; binary attachments are not included",
                  "expected_journals": expected, "unique_journals": count, "details_saved": details,
                  "missing_journals": max(0, expected-count) if expected is not None else None,
                  "pending_or_failed_details": count-details, "next_page": self.get("next_page", 1),
                  "page_size": self.get("page_size", 20),
                  "list_exhausted": self.get("list_exhausted", False),
                  "list_blocker": self.get("list_blocker"), "detail_blocker": self.get("detail_blocker"),
                  "source_snapshot_changed": bool(ending and ending != baseline),
                  "snapshot_note": "Sequential live API reads, not a transactionally consistent source snapshot."}
        save(self.root / "manifest.json", report)
        return report

    def export(self):
        schema = {}
        def walk(value, prefix):
            if isinstance(value, dict):
                for key, child in value.items():
                    walk(child, prefix + "." + key if prefix else key)
            elif isinstance(value, list):
                schema.setdefault(prefix, set()).add("array")
                for child in value:
                    walk(child, prefix + "[]")
            else:
                schema.setdefault(prefix, set()).add(type(value).__name__)
        with (self.root / "journals.jsonl").open("w", encoding="utf-8") as output:
            for journal_id, listing, detail in self.db.execute("SELECT id,list_json,detail_json FROM journals ORDER BY id"):
                record = {"id": journal_id, "listing": json.loads(listing), "detail": json.loads(detail) if detail else None}
                walk(record, "")
                output.write(encode(record) + "\n")
        save(self.root / "field_dictionary.json", {k: sorted(v) for k, v in sorted(schema.items())})
        report = self.report()
        text = (f"# Scholay 数据获取报告\n\n生成时间：{report['updated_at']}\n\n"
                f"全量完成：{'是' if report['complete'] else '否'}\n\n"
                f"基线总量：{report['expected_journals']}；已保存唯一记录：{report['unique_journals']}；"
                f"已保存详情：{report['details_saved']}；未获取记录：{report['missing_journals']}。\n\n"
                f"列表阻塞原因：{encode(report['list_blocker'])}\n\n"
                f"详情阻塞原因：{encode(report['detail_blocker'])}\n\n"
                "原始响应位于 raw/；请求与校验摘要位于 requests.jsonl。数据库保留全部列表和详情 JSON，"
                "并提供 ISSN、来源数据关联表。字段字典只表示本次已观察字段，不代表全站字段全集。\n\n"
                "二进制投稿附件未下载；不以附件可用标记代替附件。接口逐条读取不能保证源站事务级快照。\n")
        (self.root / "acquisition_report.md").write_text(text, encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parent / "data" / "scholay")
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument("--export-only", action="store_true")
    args = parser.parse_args()
    from robust_runtime import InstanceLock
    instance = InstanceLock(args.output.resolve() / 'collector.lock').acquire()
    collector = Collector(args.output.resolve(), max(1.0, args.interval))
    try:
        if not args.export_only:
            stats = collector.request("stats", {}, "start-" + str(int(time.time())))
            if collector.get("stats_start") is None:
                collector.put("stats_start", stats)
            collector.put("stats_latest", stats)
            collector.db.commit()
            save(collector.root / "stats_latest.json", stats)
            collector.lists()
            collector.details()
            collector.put("stats_end", collector.request("stats", {}, "end-" + str(int(time.time()))))
            collector.db.commit()
    finally:
        collector.export()
        print(encode(collector.report()), flush=True)
        collector.db.close()
        instance.close()


if __name__ == "__main__":
    main()
