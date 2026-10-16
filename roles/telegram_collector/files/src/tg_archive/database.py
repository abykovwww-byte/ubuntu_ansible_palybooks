"""Bound, parameterized SQL. Owner is used only by the migration command."""

from __future__ import annotations

from datetime import UTC, datetime

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .config import ConfigError, load_env
from .content import transition
from .discovery import dialog_title


async def connect_database(root, *, owner=False):
    key = "TG_DB_OWNER_DSN" if owner else "TG_DB_WRITER_DSN"
    dsn = load_env(root / "sync.env").get(key)
    if not dsn:
        raise ConfigError(f"Fill {key} in sync.env")
    return await psycopg.AsyncConnection.connect(
        dsn,
        autocommit=True,
        row_factory=dict_row,
        connect_timeout=15,
        options="-c statement_timeout=30000 -c lock_timeout=10000",
    )


async def migrate(root):
    paths = sorted((root / "migrations").glob("[0-9][0-9][0-9]_*.sql"))
    if not paths:
        raise ConfigError("Numbered SQL migrations are missing")
    connection = await connect_database(root, owner=True)
    async with connection, connection.transaction():
        await connection.execute("SELECT pg_advisory_xact_lock(4282347406)")
        for path in paths:
            await connection.execute(path.read_text(encoding="utf-8"), prepare=False)
    return {"migration_files": len(paths), "status": "ok"}


def upsert_sql(table, fields, keys):
    return sql.SQL("INSERT INTO {} ({}) VALUES ({}) ON CONFLICT ({}) DO UPDATE SET {}").format(
        sql.Identifier(table),
        sql.SQL(",").join(map(sql.Identifier, fields)),
        sql.SQL(",").join(sql.Placeholder() for _ in fields),
        sql.SQL(",").join(map(sql.Identifier, keys)),
        sql.SQL(",").join(
            sql.SQL("{}=EXCLUDED.{}").format(sql.Identifier(f), sql.Identifier(f))
            for f in fields
            if f not in keys
        ),
    )


class Store:
    def __init__(self, connection):
        self.connection = connection
        self.run_id = None

    async def begin(self, started):
        # Session-scoped lock also excludes another server container.
        from .locking import AlreadyRunning

        result = await self.connection.execute(
            "SELECT pg_try_advisory_lock(4282347407) AS acquired"
        )
        if not (await result.fetchone())["acquired"]:
            raise AlreadyRunning("Another tg-sync holds the lock")
        async with self.connection.transaction():
            # The OS lock excludes a concurrent collector; previous running rows are interrupted.
            await self.connection.execute(
                "UPDATE sync_run SET status='failed', finished_at=%s, error='interrupted' "
                "WHERE status='running'",
                (started,),
            )
            result = await self.connection.execute(
                "INSERT INTO sync_run(started_at,status) VALUES(%s,'running') RETURNING id",
                (started,),
            )
            self.run_id = (await result.fetchone())["id"]

    async def finish(self, status, counts, error=None):
        await self.connection.execute(
            "UPDATE sync_run SET finished_at=%s,status=%s,counts=%s,error=%s WHERE id=%s",
            (datetime.now(UTC), status, Jsonb(counts), error, self.run_id),
        )

    async def get_chat(self, chat_id):
        result = await self.connection.execute("SELECT * FROM tg_chat WHERE chat_id=%s", (chat_id,))
        return await result.fetchone()

    async def prepare_chat(self, spec, entity, kind, started):
        await self.connection.execute(
            "INSERT INTO tg_chat(chat_id,slug,title,project,kind,public_username,protected,since,"
            "status,last_attempt_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,'new',%s) "
            "ON CONFLICT(chat_id) DO UPDATE SET slug=EXCLUDED.slug,title=EXCLUDED.title,"
            "project=EXCLUDED.project,kind=EXCLUDED.kind,public_username=EXCLUDED.public_username,"
            "protected=EXCLUDED.protected,since=EXCLUDED.since,last_attempt_at=EXCLUDED.last_attempt_at",
            (
                spec.chat_id,
                spec.slug,
                dialog_title(entity),
                spec.project,
                kind,
                getattr(entity, "username", None),
                bool(getattr(entity, "noforwards", False)),
                spec.since,
                started,
            ),
        )
        return await self.get_chat(spec.chat_id)

    async def chat_status(self, chat_id, status, error=None, *, backfill_done=None):
        await self.connection.execute(
            "UPDATE tg_chat SET status=%s,last_error=%s,last_attempt_at=%s,"
            "last_ok_at=CASE WHEN %s='ok' THEN %s ELSE last_ok_at END,"
            "backfill_done=coalesce(%s,backfill_done) WHERE chat_id=%s",
            (status, error, datetime.now(UTC), status, datetime.now(UTC), backfill_done, chat_id),
        )

    async def batch(self, chat_id, records, users, cursor, counts):
        async with self.connection.transaction():
            ids = [row["msg_id"] for row in records]
            result = await self.connection.execute(
                "SELECT * FROM tg_message WHERE chat_id=%s AND msg_id=ANY(%s)",
                (chat_id, ids),
            )
            previous = {row["msg_id"]: row for row in await result.fetchall()}
            async with self.connection.cursor() as cur:
                if users:
                    fields = tuple(users[0])
                    await cur.executemany(
                        upsert_sql("tg_user", fields, ("user_id",)),
                        [tuple(u.values()) for u in users],
                    )
                if records:
                    rows = [transition(row.copy(), previous.get(row["msg_id"])) for row in records]
                    fields = tuple(rows[0])
                    await cur.executemany(
                        upsert_sql("tg_message", fields, ("chat_id", "msg_id")),
                        [tuple(row.values()) for row in rows],
                    )
            if cursor is not None:
                await self.connection.execute(
                    "UPDATE tg_chat SET cursor_msg_id=greatest(cursor_msg_id,%s) WHERE chat_id=%s",
                    (cursor, chat_id),
                )
            if self.run_id is not None:
                await self.connection.execute(
                    "UPDATE sync_run SET counts=%s WHERE id=%s", (Jsonb(counts), self.run_id)
                )

    async def window_ids(self, chat_id, since, until):
        result = await self.connection.execute(
            "SELECT msg_id FROM tg_message WHERE chat_id=%s AND sent_at>=%s AND sent_at<=%s",
            (chat_id, since, until),
        )
        return {row["msg_id"] for row in await result.fetchall()}

    async def delete_confirmed(self, chat_id, ids, counts):
        async with self.connection.transaction():
            result = await self.connection.execute(
                "DELETE FROM tg_message WHERE chat_id=%s AND msg_id=ANY(%s)",
                (chat_id, ids),
            )
            counts[str(chat_id)]["deleted"] += result.rowcount
            if self.run_id is not None:
                await self.connection.execute(
                    "UPDATE sync_run SET counts=%s WHERE id=%s", (Jsonb(counts), self.run_id)
                )

    async def done_snapshot(self):
        async with self.connection.transaction():
            await self.connection.execute(
                "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
            )
            result = await self.connection.execute(
                "SELECT chat_id,msg_id,media_path,media_size FROM tg_message "
                "WHERE media_status='done'"
            )
            return await result.fetchall()

    async def reset_done(self, rows):
        async with self.connection.transaction():
            for row in rows:
                result = await self.connection.execute(
                    "UPDATE tg_message SET media_status='pending',media_path=NULL,"
                    "media_sha256=NULL,"
                    "media_error=NULL,media_attempts=0 WHERE chat_id=%s AND msg_id=%s "
                    "AND media_status='done' AND media_path=%s",
                    (row["chat_id"], row["msg_id"], row["media_path"]),
                )
                if result.rowcount != 1:
                    raise ValueError("Snapshot changed; reset transaction rolled back")
        return len(rows)

    async def unfinished(self, chat_ids, *, pending_only=False):
        condition = (
            "media_status='pending'"
            if pending_only
            else ("(media_status='pending' OR left(media_status,8)='skipped_')")
        )
        result = await self.connection.execute(
            "SELECT * FROM tg_message WHERE chat_id=ANY(%s) AND "
            + condition
            + " ORDER BY sent_at DESC,msg_id DESC",
            (chat_ids,),
        )
        return await result.fetchall()

    async def set_eligibility(self, row, status):
        await self.connection.execute(
            "UPDATE tg_message SET media_status=%s WHERE chat_id=%s AND msg_id=%s "
            "AND media_file_id IS NOT DISTINCT FROM %s AND "
            "(media_status='pending' OR left(media_status,8)='skipped_')",
            (status, row["chat_id"], row["msg_id"], row["media_file_id"]),
        )

    async def media_done(self, row, path, sha256):
        result = await self.connection.execute(
            "UPDATE tg_message SET media_status='done',media_path=%s,media_sha256=%s,"
            "media_error=NULL "
            "WHERE chat_id=%s AND msg_id=%s AND media_status='pending' "
            "AND media_file_id IS NOT DISTINCT FROM %s",
            (path, sha256, row["chat_id"], row["msg_id"], row["media_file_id"]),
        )
        if result.rowcount != 1:
            raise ValueError("Attachment changed before done commit")

    async def media_failure(self, row, error):
        result = await self.connection.execute(
            "UPDATE tg_message SET media_attempts=media_attempts+1,media_error=%s,"
            "media_status=CASE WHEN media_attempts+1>=3 THEN 'failed' ELSE 'pending' END "
            "WHERE chat_id=%s AND msg_id=%s AND media_status='pending' "
            "AND media_file_id IS NOT DISTINCT FROM %s RETURNING media_status",
            (error, row["chat_id"], row["msg_id"], row["media_file_id"]),
        )
        updated = await result.fetchone()
        return updated and updated["media_status"] == "failed"

    async def done_bytes(self):
        result = await self.connection.execute(
            "SELECT coalesce(sum(media_size),0) AS n FROM tg_message WHERE media_status='done'"
        )
        return int((await result.fetchone())["n"])

    async def quota_remaining(self, chat_ids):
        result = await self.connection.execute(
            "UPDATE tg_message SET media_status='skipped_quota' "
            "WHERE chat_id=ANY(%s) AND media_status='pending'",
            (chat_ids,),
        )
        return result.rowcount

    async def unconfigured(self, chat_ids):
        result = await self.connection.execute(
            "SELECT slug FROM tg_chat WHERE NOT(chat_id=ANY(%s)) ORDER BY slug",
            (chat_ids,),
        )
        return [row["slug"] for row in await result.fetchall()]


async def status(root, settings=None):
    connection = await connect_database(root)
    async with connection:
        result = await connection.execute(
            "SELECT id,started_at,finished_at,status,counts,error FROM sync_run "
            "ORDER BY id DESC LIMIT 10"
        )
        runs = await result.fetchall()
        result = await connection.execute(
            "SELECT c.slug,c.kind,c.status,c.cursor_msg_id,c.backfill_done,c.last_ok_at,"
            "count(m.msg_id) AS messages,min(m.sent_at) AS first_available_at,"
            "max(m.sent_at) AS latest_at "
            "FROM tg_chat c LEFT JOIN tg_message m USING(chat_id) "
            "GROUP BY c.chat_id ORDER BY c.slug"
        )
        chats = await result.fetchall()
        for chat in chats:
            chat["stale"] = is_stale(chat["last_ok_at"], datetime.now(UTC))
        result = await connection.execute(
            "SELECT media_status,count(*) AS files,coalesce(sum(media_size),0) AS bytes "
            "FROM tg_message WHERE media_kind IS NOT NULL GROUP BY media_status"
        )
        media = await result.fetchall()
    result = {
        "runs": runs,
        "chats": chats,
        "media_metadata": media,
        "notice": "Archive snapshot; edits/deletions outside refresh_days are not reflected.",
    }
    if settings is not None:
        from .sftp import writer_filesystem

        try:
            async with writer_filesystem(settings) as fs:
                result["server_storage"] = {
                    "status": "ok",
                    "free_bytes": await fs.free_bytes(settings.media_settings.remote_root),
                }
        except Exception:
            result["server_storage"] = {"status": "unavailable"}
    return result


def is_stale(last_ok_at, now):
    return last_ok_at is None or (now - last_ok_at).total_seconds() > 36 * 3600
