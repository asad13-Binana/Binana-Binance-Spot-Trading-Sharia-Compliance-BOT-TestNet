"""Narrow, atomic ORM metadata repair using the fresh authenticated audit.

Does not submit orders, change Trade/Order economics, or close incidents.
The caller must supply an existing database and exact evidence digests.
"""
import argparse
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import sys

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from freqtrade.persistence import Trade, Order
from freqtrade.persistence.custom_data import _CustomData


VERSION = "authenticated-retention-replay-v1"

if sys.flags.optimize:
    raise RuntimeError("Optimized Python is forbidden for this guarded repair")


def dec(value):
    return Decimal(str(value or 0))


def snapshot(connection):
    schema = [tuple(row) for row in connection.exec_driver_sql(
        "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name")]
    tables = {}
    for kind, name, _, _ in schema:
        if kind == "table":
            quoted = '"' + name.replace('"', '""') + '"'
            tables[name] = sorted(
                [dict(row) for row in connection.exec_driver_sql(
                    "SELECT * FROM " + quoted).mappings()],
                key=lambda row: repr(sorted(row.items())))
    return {"schema": schema, "tables": tables}


def run(args):
    database = Path(args.database)
    assert database.is_file() and not database.is_symlink()
    economic_bytes = Path(args.economic).read_bytes()
    retention_bytes = Path(args.retention).read_bytes()
    assert sha256(economic_bytes).hexdigest() == args.economic_sha256
    assert sha256(retention_bytes).hexdigest() == args.retention_sha256
    economic, retention = json.loads(economic_bytes), json.loads(retention_bytes)
    for key in ("quantity_mismatches", "cost_mismatches", "errors", "incomplete_pairs"):
        assert economic["summary"][key] == 0, key
    for key in ("individual_executable", "aggregate_executable", "amount_mismatches",
                "cost_mismatches", "realized_mismatches"):
        assert retention["summary"][key] == 0, key
    assert all(dec(fee) == 0 for row in economic["orders"] for fee in row["fees"].values())
    assert all(row["exchange_status"] in {"FILLED", "CANCELED", "EXPIRED"}
               for row in economic["orders"])
    audits = {row["id"]: row for row in economic["trades"]}
    retained = {row["trade_id"]: row for row in retention["retained"]}
    assert len(audits) == len(economic["trades"]) == 85
    assert len(retained) == len(retention["retained"]) == 73
    engine = create_engine(f"sqlite:///file:{database.resolve()}?mode=rw&uri=true")
    changed = []
    with Session(engine, autoflush=False) as session:
        connection = session.connection()
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        assert connection.exec_driver_sql("PRAGMA integrity_check").fetchall() == [("ok",)]
        before = snapshot(connection)
        trades = {row.id: row for row in session.scalars(select(Trade))}
        orders = list(session.scalars(select(Order)))
        assert set(trades) == set(audits)
        assert not any(t.is_open for t in trades.values())
        assert not any(o.ft_is_open for o in orders)
        filled = {(o.ft_trade_id, str(o.order_id)): o for o in orders if dec(o.filled) > 0}
        assert set(filled) == {(r["trade_id"], str(r["order_id"])) for r in economic["orders"]}
        for receipt in economic["orders"]:
            order = filled[(receipt["trade_id"], str(receipt["order_id"]))]
            recorded = receipt["canonical_order"]
            for field in ("ft_trade_id", "order_id", "ft_pair", "ft_order_side", "status"):
                assert str(getattr(order, field)) == str(recorded[field]), field
            for field in ("filled", "cost"):
                assert dec(getattr(order, field)) == dec(recorded[field]), field
            assert abs(dec(order.filled) - dec(receipt["authenticated_base"])) < dec("0.00000001")
            assert abs(dec(order.cost) - dec(receipt["authenticated_quote"])) < dec("0.00000001")
        for identifier, audit in audits.items():
            trade = trades[identifier]
            assert trade.pair == audit["pair"]
            for side in ("buy", "sell"):
                selected = [o for (tid, _), o in filled.items() if tid == identifier
                            and ("buy" if o.ft_order_side == "buy" else "sell") == side]
                assert abs(sum((dec(o.filled) for o in selected), dec(0))
                           - dec(audit["fills"].get(side + "_base"))) < dec("0.00000001")
                assert abs(sum((dec(o.cost) for o in selected), dec(0))
                           - dec(audit["fills"].get(side + "_quote"))) < dec("0.00000001")
        old_metadata = {(r["ft_trade_id"], r["cd_key"]): r
                        for r in before["tables"]["trade_custom_data"]}
        allowed_keys = set()
        for identifier, evidence in retained.items():
            trade = trades[identifier]
            assert evidence["evidence_sha256"] == args.economic_sha256
            assert evidence["pair"] == trade.pair
            assert evidence["non_executable"] is True and evidence["exchange_open_orders"] == 0
            assert evidence["scope"] == "binance-testnet|owner-testnet-account-1|binana-20260910-owner-cutover-1"
            for actual, expected in ((trade.amount, evidence["quantity"]),
                                     (trade.stake_amount, evidence["cost_basis"]),
                                     (trade.realized_profit, evidence["realized_profit"]),
                                     (trade.close_profit_abs, evidence["realized_profit"])):
                assert abs(dec(actual) - dec(expected)) < dec("0.0000001")
            metadata = {**evidence, "phase": "retained", "repair_version": VERSION}
            key = "binana_retained_dust"
            row = session.scalar(select(_CustomData).where(
                _CustomData.ft_trade_id == identifier, _CustomData.cd_key == key))
            if row is not None and json.loads(row.cd_value) == metadata:
                continue
            if row is not None:
                backup_key = "binana_retained_dust_pre_replay"
                prior = session.scalar(select(_CustomData).where(
                    _CustomData.ft_trade_id == identifier, _CustomData.cd_key == backup_key))
                if prior is None:
                    session.add(_CustomData(ft_trade_id=identifier, cd_key=backup_key,
                        cd_type=row.cd_type, cd_value=row.cd_value,
                        created_at=datetime.now(timezone.utc)))
                    allowed_keys.add((identifier, backup_key))
                row.cd_value = json.dumps(metadata)
                row.cd_type = "dict"
                row.updated_at = datetime.now(timezone.utc)
            else:
                session.add(_CustomData(ft_trade_id=identifier, cd_key=key,
                    cd_type="dict", cd_value=json.dumps(metadata),
                    created_at=datetime.now(timezone.utc)))
            allowed_keys.add((identifier, key))
            changed.append(identifier)
        session.flush()
        after = snapshot(connection)
        assert before["schema"] == after["schema"]
        for table in before["tables"]:
            if table != "trade_custom_data":
                assert before["tables"][table] == after["tables"][table], table
        new_metadata = {(r["ft_trade_id"], r["cd_key"]): r
                        for r in after["tables"]["trade_custom_data"]}
        assert set(old_metadata) <= set(new_metadata)
        for key in set(old_metadata) | set(new_metadata):
            if key not in allowed_keys:
                assert old_metadata.get(key) == new_metadata.get(key), key
        assert connection.exec_driver_sql("PRAGMA integrity_check").fetchall() == [("ok",)]
        if args.inject_failure:
            raise RuntimeError("injected failure before commit")
        session.commit()
    engine.dispose()
    with sqlite3.connect(f"file:{database.resolve()}?mode=ro", uri=True) as c:
        assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    print(json.dumps({"metadata_changes": len(changed), "trade_ids": changed,
                      "economic_fields_and_orders_unchanged": True,
                      "unrelated_metadata_unchanged": True,
                      "schema_unchanged": True, "incidents_unchanged": True,
                      "economic_sha256": args.economic_sha256,
                      "retention_sha256": args.retention_sha256}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("database", "economic", "retention", "economic_sha256", "retention_sha256"):
        parser.add_argument("--" + name.replace("_", "-"), required=True)
    parser.add_argument("--inject-failure", action="store_true")
    run(parser.parse_args())
