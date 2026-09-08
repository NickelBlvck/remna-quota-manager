#!/usr/bin/env python3
import sys
import json
import argparse
from database import QuotaDatabase
from nodes import resolve_monitored_nodes
from remnawave import RemnawaveAPI
from settings import load_config

def main():
    db = QuotaDatabase()
    
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("list", "stats", "unblock", "audit", "export", "check"))
    parser.add_argument("uuid", nargs="?")
    args = parser.parse_args()

    cmd = args.command

    if cmd == "check":
        from monitor import TrafficMonitor
        config = load_config()
        api = RemnawaveAPI(config["panel"]["base_url"], config["panel"]["token"])
        result = TrafficMonitor(api, db, config).evaluate()
        mode = "DRY-RUN" if result["dry_run"] else "БОЕВОЙ"
        rows = result["rows"]
        print(f"# режим: {mode} · hysteresis: {result['need']} · строк: {len(rows)}")
        print(f"{'node':<16} {'user':<22} {'cycle GB':>9} {'%':>5} {'cycle end':<12} verdict")
        print("-" * 90)
        for r in rows:
            t = r["traffic_gb"]
            tg = f"{t:.2f}" if t is not None else "-"
            pct = f"{t / float(r['limit_gb']) * 100:.0f}" if (t is not None and r["limit_gb"]) else "-"
            tag = r["verdict"]
            if r["verdict"] == "over":
                tag = f"over {r['checks']}/{r['need']}" + ("  <= NEXT PASS" if r["checks"] + 1 >= r["need"] else "")
            print(f"{r['node_name'][:16]:<16} {str(r['username'])[:22]:<22} {tg:>9} {pct:>5} "
                  f"{str(r.get('cycle_end') or '')[:12]:<12} {tag}")
        return

    if cmd == "list":
        users = db.list_limited()
        print(f"{'UUID':<36} | {'Username':<20} | {'Node':<15}")
        print("-" * 75)
        for u in users:
            print(f"{u['uuid']:<36} | {str(u['username'] or ''):<20} | {str(u['node_name'] or ''):<15}")
            
    elif cmd == "stats":
        print(f"Limited:  {len(db.list_limited())}")
        print(f"Pending:  {len(db.list_pending())}")
        print(f"Approvals waiting: {len(db.list_waiting_approvals())}")
        print(f"Whitelist: {len(db.list_whitelist())}")
        
    elif cmd == "unblock":
        if not args.uuid:
            print("Need UUID")
            return
        uuid = args.uuid.lower()
        config = load_config()
        api = RemnawaveAPI(config["panel"]["base_url"], config["panel"]["token"])
        records = [row for row in db.list_limited() if row["uuid"] == uuid and not row["dry_run"]]
        if not records:
            print("UUID is not present in local limits")
            return
        nodes = resolve_monitored_nodes(api, config)
        by_uuid = {node.get("uuid"): node for node in nodes}
        by_name = {node.get("name"): node for node in nodes}
        targets = []
        for record in records:
            node = by_uuid.get(record["node_uuid"]) or by_name.get(record.get("node_name")) or {}
            squad = node.get("full_external_squad_uuid")
            if not squad:
                print(f"❌ No full_external_squad_uuid configured for node {record['node_name']}")
                return
            targets.append(squad)
        if not api.set_user_external_squads(uuid, targets, remove_from_all=True):
            print("❌ Panel did not confirm unblock; local record kept")
            return
        db.remove_limited(uuid)
        db.audit("manual_unblock_cli", user_uuid=uuid, details={"source": "db_tool", "nodes": len(records)})
        print(f"✅ {uuid} unblocked in panel and removed from local limits")
        
    elif cmd == "audit":
        logs = db.recent_audit(10)
        for log in logs:
            print(f"[{log['created_at']}] {log['action']} - {log['username'] or '-'}")
            
    elif cmd == "export":
        print(json.dumps({"limited_users": db.list_limited()}, indent=2))

if __name__ == "__main__":
    main()
