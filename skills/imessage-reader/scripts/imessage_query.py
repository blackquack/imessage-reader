#!/usr/bin/env python3
"""Read a small, filtered view of a macOS Messages database.

This helper is intentionally read-only. It uses SQLite's URI mode=ro and
query_only pragma, never returns raw attributedBody bytes, and has no network
or message-sending functionality.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote


DEFAULT_DB = Path.home() / "Library" / "Messages" / "chat.db"
APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)
MAX_LIMIT = 200
MAX_SCAN = 20_000


class QueryError(Exception):
    """An expected, user-actionable query failure."""


def json_print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=False))


def db_path_from_args(args: argparse.Namespace) -> Path:
    raw = getattr(args, "db", None) or os.environ.get("IMESSAGE_DB")
    return Path(os.path.expanduser(raw)) if raw else DEFAULT_DB


def open_read_only(path: Path) -> sqlite3.Connection:
    """Open only an existing database, without creating or mutating it."""

    if not path.exists():
        raise QueryError(f"Messages database not found: {path}")
    if not path.is_file():
        raise QueryError(f"Messages database path is not a file: {path}")

    uri = f"file:{quote(str(path.resolve()))}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=2)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.execute("PRAGMA busy_timeout=2000")
        return conn
    except (OSError, sqlite3.Error) as exc:
        raise QueryError(
            "macOS denied access to the Messages database. Grant Full Disk "
            "Access to the app launching this helper, or provide a read-only "
            f"snapshot. ({exc})"
        ) from exc


def table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    if not table.replace("_", "").isalnum():
        raise QueryError("Unsafe table name")
    try:
        rows = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
    except sqlite3.Error as exc:
        raise QueryError(f"Could not inspect the Messages schema: {exc}") from exc
    return {str(row[1]) for row in rows}


def has_table(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (table,),
    ).fetchone()
    return row is not None


def require_messages_schema(conn: sqlite3.Connection) -> dict[str, set[str]]:
    if not has_table(conn, "message"):
        raise QueryError(
            "This file does not look like a macOS Messages database: "
            "the message table is missing."
        )
    names = {"message": table_columns(conn, "message")}
    for table in ("handle", "chat", "chat_message_join"):
        names[table] = table_columns(conn, table) if has_table(conn, table) else set()
    return names


def parse_date(value: str, *, end: bool = False) -> datetime:
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise QueryError(f"Date must use YYYY-MM-DD: {value}") from exc
    return parsed + timedelta(days=1) if end else parsed


def apple_seconds(value: datetime) -> float:
    return (value - APPLE_EPOCH).total_seconds()


def date_scale(conn: sqlite3.Connection, message_columns: set[str]) -> float:
    if "date" not in message_columns:
        return 1.0
    row = conn.execute("SELECT MAX(ABS(date)) FROM message").fetchone()
    maximum = row[0] if row and row[0] is not None else 0
    # macOS commonly stores NSDate values as nanoseconds; some snapshots use
    # seconds. The threshold keeps both forms usable without rewriting data.
    return 1_000_000_000.0 if abs(float(maximum)) > 1_000_000_000_000 else 1.0


def typedstream_length(data: bytes, position: int) -> tuple[int, int] | None:
    if position >= len(data):
        return None
    marker = data[position]
    if marker <= 0x7F:
        return marker, position + 1
    if marker == 0x81 and position + 3 <= len(data):
        return int.from_bytes(data[position + 1 : position + 3], "little"), position + 3
    if marker == 0x82 and position + 5 <= len(data):
        return int.from_bytes(data[position + 1 : position + 5], "little"), position + 5
    if marker == 0x83 and position + 9 <= len(data):
        length = int.from_bytes(data[position + 1 : position + 9], "little")
        return length, position + 9
    return None


def looks_like_text(value: str) -> bool:
    if not value or "\x00" in value:
        return not value
    allowed = sum(char.isprintable() or char in "\r\n\t" for char in value)
    return allowed / len(value) >= 0.85


def decode_attributed_body(blob: bytes | memoryview | None) -> str | None:
    """Decode the NSString payload used by modern macOS attributedBody blobs."""

    if blob is None:
        return None
    data = bytes(blob)
    marker = b"NSString"
    # Both 0x94 and 0x95 have appeared in the typedstream preamble used by
    # macOS Messages (immutable and mutable attributed strings).
    preambles = (
        b"\x01\x94\x84\x01\x2b",
        b"\x01\x95\x84\x01\x2b",
        b"\x01\x96\x84\x01\x2b",
    )
    position = 0
    while True:
        marker_position = data.find(marker, position)
        if marker_position < 0:
            return None
        after_marker = marker_position + len(marker)
        for preamble in preambles:
            if not data.startswith(preamble, after_marker):
                continue
            length_info = typedstream_length(data, after_marker + len(preamble))
            if length_info is None:
                continue
            length, text_start = length_info
            text_end = text_start + length
            if text_end > len(data):
                continue
            try:
                candidate = data[text_start:text_end].decode("utf-8")
            except UnicodeDecodeError:
                continue
            if looks_like_text(candidate):
                return candidate
        position = after_marker


def apple_date_to_iso(value: Any) -> str | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if abs(number) > 1_000_000_000_000:
        number /= 1_000_000_000.0
    try:
        return (APPLE_EPOCH + timedelta(seconds=number)).isoformat().replace(
            "+00:00", "Z"
        )
    except (OverflowError, ValueError):
        return str(value)


def select_parts(schema: dict[str, set[str]]) -> tuple[str, list[str]]:
    message = schema["message"]
    handle = schema["handle"]
    chat = schema["chat"]
    join = schema["chat_message_join"]

    def column(alias: str, columns: set[str], name: str, output: str) -> str:
        return f"{alias}.\"{name}\" AS {output}" if name in columns else f"NULL AS {output}"

    selected = [
        "m.ROWID AS message_rowid",
        column("m", message, "guid", "guid"),
        column("m", message, "text", "plain_text"),
        column("m", message, "attributedBody", "attributed_body"),
        column("m", message, "is_from_me", "is_from_me"),
        column("m", message, "date", "date_value"),
        column("h", handle, "id", "handle_id"),
        column("h", handle, "uncanonicalized_id", "uncanonicalized_id"),
        column("c", chat, "display_name", "chat_display_name"),
        column("c", chat, "room_name", "chat_room_name"),
        column("c", chat, "guid", "chat_guid"),
    ]
    joins: list[str] = []
    if "handle_id" in message and handle:
        joins.append("LEFT JOIN handle h ON h.ROWID = m.handle_id")
    else:
        joins.append("LEFT JOIN handle h ON 1=0")
    if join and chat:
        joins.extend(
            [
                "LEFT JOIN chat_message_join cmj ON cmj.message_id = m.ROWID",
                "LEFT JOIN chat c ON c.ROWID = cmj.chat_id",
            ]
        )
    else:
        joins.append("LEFT JOIN chat c ON 1=0")
    return ", ".join(selected), joins


def make_rows_query(
    conn: sqlite3.Connection,
    schema: dict[str, set[str]],
    *,
    contact: str | None,
    from_date: str | None,
    to_date: str | None,
    fetch_limit: int,
) -> tuple[str, list[Any]]:
    selected, joins = select_parts(schema)
    message = schema["message"]
    handle = schema["handle"]
    chat = schema["chat"]
    conditions = ["1=1"]
    params: list[Any] = []

    scale = date_scale(conn, message)
    if from_date and "date" in message:
        conditions.append("m.date >= ?")
        params.append(apple_seconds(parse_date(from_date)) * scale)
    if to_date and "date" in message:
        conditions.append("m.date < ?")
        params.append(apple_seconds(parse_date(to_date, end=True)) * scale)

    if contact:
        terms: list[str] = []
        like_value = f"%{contact}%"
        if "id" in handle:
            terms.append("h.id LIKE ?")
            params.append(like_value)
        if "uncanonicalized_id" in handle:
            terms.append("h.uncanonicalized_id LIKE ?")
            params.append(like_value)
        if "display_name" in chat:
            terms.append("c.display_name LIKE ?")
            params.append(like_value)
        if "room_name" in chat:
            terms.append("c.room_name LIKE ?")
            params.append(like_value)
        if not terms:
            raise QueryError("This Messages database has no searchable contact fields")
        conditions.append("(" + " OR ".join(terms) + ")")

    order_by = "m.date DESC, m.ROWID DESC" if "date" in message else "m.ROWID DESC"
    query = (
        f"SELECT {selected} FROM message m {' '.join(joins)} "
        f"WHERE {' AND '.join(conditions)} "
        "GROUP BY m.ROWID "
        f"ORDER BY {order_by} "
        "LIMIT ?"
    )
    params.append(fetch_limit)
    return query, params


def render_row(row: sqlite3.Row, metadata_only: bool) -> dict[str, Any]:
    plain_text = row["plain_text"]
    text: str | None = str(plain_text) if plain_text not in (None, "") else None
    source = "text" if text is not None else None
    if text is None:
        text = decode_attributed_body(row["attributed_body"])
        source = "attributedBody" if text is not None else None

    is_from_me = bool(row["is_from_me"]) if row["is_from_me"] is not None else None
    sender = row["handle_id"] or row["uncanonicalized_id"]
    if is_from_me:
        sender = "Me"

    result: dict[str, Any] = {
        "message_rowid": row["message_rowid"],
        "guid": row["guid"],
        "date": apple_date_to_iso(row["date_value"]),
        "is_from_me": is_from_me,
        "sender": sender,
        "chat": row["chat_display_name"] or row["chat_room_name"] or row["chat_guid"],
        "text_available": text is not None,
    }
    if not metadata_only:
        result["text"] = text
        result["text_source"] = source
    return result


def run_doctor(path: Path) -> int:
    result: dict[str, Any] = {
        "db_path": str(path),
        "exists": path.exists(),
        "readable": False,
        "read_only": True,
    }
    if path.exists():
        try:
            result["size_bytes"] = path.stat().st_size
        except OSError:
            result["size_bytes"] = None
    try:
        with open_read_only(path) as conn:
            schema = require_messages_schema(conn)
            result["readable"] = True
            result["tables"] = sorted(
                name
                for name in ("message", "handle", "chat", "chat_message_join")
                if schema[name]
            )
            result["message_count"] = conn.execute("SELECT COUNT(*) FROM message").fetchone()[0]
            if has_table(conn, "chat"):
                result["chat_count"] = conn.execute("SELECT COUNT(*) FROM chat").fetchone()[0]
    except QueryError as exc:
        result["error"] = str(exc)
    json_print(result)
    return 0 if result["readable"] else 2


def run_rows(args: argparse.Namespace, mode: str) -> int:
    path = db_path_from_args(args)
    with open_read_only(path) as conn:
        schema = require_messages_schema(conn)
        limit = min(max(int(args.limit), 1), MAX_LIMIT)
        if mode == "search":
            fetch_limit = min(max(int(args.max_scan), limit), MAX_SCAN)
        else:
            fetch_limit = limit
        query, params = make_rows_query(
            conn,
            schema,
            contact=args.contact,
            from_date=args.from_date,
            to_date=args.to_date,
            fetch_limit=fetch_limit,
        )
        rows = conn.execute(query, params).fetchall()

    rendered: list[dict[str, Any]] = []
    needle = args.query.casefold() if mode == "search" else None
    for row in rows:
        item = render_row(row, args.metadata_only)
        if needle is not None:
            body = item.get("text")
            if body is None or needle not in body.casefold():
                continue
        rendered.append(item)
        if len(rendered) >= limit:
            break

    json_print(
        {
            "query": mode,
            "db_path": str(path),
            "count": len(rendered),
            "results": rendered,
            **({"searched_for": args.query} if mode == "search" else {}),
        }
    )
    return 0


def run_self_test() -> int:
    samples = (
        "short message",
        (
            "A longer message with emoji 😀 and enough bytes to exercise the length "
            "prefix without relying on private database contents. "
        )
        * 4,
    )
    for sample in samples:
        payload = sample.encode("utf-8")
        if len(payload) <= 0x7F:
            length = bytes([len(payload)])
        elif len(payload) <= 0xFFFF:
            length = b"\x81" + len(payload).to_bytes(2, "little")
        else:
            length = b"\x82" + len(payload).to_bytes(4, "little")
        blob = b"prefix NSString" + b"\x01\x95\x84\x01\x2b" + length + payload
        decoded = decode_attributed_body(blob)
        if decoded != sample:
            raise QueryError("attributedBody self-test failed")
    json_print({"ok": True, "tests": ["typedstream short", "typedstream long"]})
    return 0


def add_query_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--db", help="Explicit database or read-only snapshot path")
    parser.add_argument("--limit", type=int, default=20, help="Maximum results (default: 20)")
    parser.add_argument("--contact", help="Phone, email, contact name, or chat label")
    parser.add_argument("--from", dest="from_date", help="Inclusive date, YYYY-MM-DD")
    parser.add_argument("--to", dest="to_date", help="Inclusive date, YYYY-MM-DD")
    parser.add_argument(
        "--metadata-only",
        action="store_true",
        help="Omit message bodies from the result",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser("doctor", help="Check local database access")
    doctor.add_argument("--db", help="Explicit database or read-only snapshot path")

    recent = subparsers.add_parser("recent", help="Read recent messages")
    add_query_options(recent)

    search = subparsers.add_parser("search", help="Search decoded message text")
    search.add_argument("query")
    add_query_options(search)
    search.add_argument(
        "--max-scan",
        type=int,
        default=5000,
        help="Maximum newest rows to inspect when searching (default: 5000)",
    )

    conversation = subparsers.add_parser("conversation", help="Read messages for a contact/chat")
    add_query_options(conversation)
    conversation.set_defaults(contact_required=True)

    subparsers.add_parser("self-test", help=argparse.SUPPRESS)
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "doctor":
            return run_doctor(db_path_from_args(args))
        if args.command == "self-test":
            return run_self_test()
        if args.command == "conversation" and not args.contact:
            raise QueryError("conversation requires --contact")
        if args.command == "search":
            return run_rows(args, "search")
        return run_rows(args, "recent")
    except QueryError as exc:
        json_print({"error": str(exc), "read_only": True})
        return 2
    except sqlite3.Error as exc:
        json_print({"error": f"SQLite query failed: {exc}", "read_only": True})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
