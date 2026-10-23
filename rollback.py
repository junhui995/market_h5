# -*- coding: utf-8 -*-
"""
rollback.py —— 依写入审计日志（write_audit_*.jsonl）撤销脏数据（不改变审计文件本身）。

用法（服务器上）：
  python3 rollback.py /path/write_audit_20261023.jsonl --lines 3 --dry-run   # 预览
  python3 rollback.py /path/write_audit_20261023.jsonl --lines 3 --yes       # 执行
  python3 rollback.py /path/...  --lines 3-5,7 --yes         # 反推区间/离散行
  python3 rollback.py --last 1 --yes                        # 用目录里最新审计文件的最后一行

连接参数亦可经 MH5_DB_HOST/PORT/USER/PASSWORD/NAME 环境变量注入（优先命令行参数）。

反推规则（与 server.py 写路径一一对应）：
  · act.action == "insert" → DELETE FROM `table` WHERE id = act.id [LIMIT 1]
    （删除用 id + creator_id 双条件防耗心误删行内 id 变化的极端情况）
  · act.action == "update" → UPDATE `table` SET before列=before值 ... WHERE id = act.id
    （before 中缺失的列说明写路径更新前即 NULL，恢复为 NULL）
安全：默认 dry-run；--yes 才落库；删除行前会 SELECT 校验行仍在（防重复回滚）。
"""
import os, sys, json, glob, argparse, re

sys.stdout.reconfigure(encoding="utf-8")


def newest_audit_file(dir_="audit"):
    files = sorted(glob.glob(os.path.join(dir_, "write_audit_*.jsonl")))
    if not files:
        raise SystemExit("no audit files under %s" % dir_)
    return files[-1]


def parse_lines(spec, n_total):
    """'3-5,7' -> [3,4,5,7]（1-based 行号）；校验范围"""
    out = set()
    for part in spec.split(","):
        if "-" in part:
            a, b = part.split("-")
            out.update(range(int(a), int(b) + 1))
        else:
            out.add(int(part))
    res = [i for i in sorted(out) if 1 <= i <= n_total]
    if not res:
        raise SystemExit("--lines 超出审计文件范围（共 %d 行）" % n_total)
    return res


def build_sql(act):
    table = act["table"]
    rid = act["id"]
    if act["action"] == "insert":
        extra = ""
        row = act.get("row") or {}
        if row.get("creator_id"):
            extra = " AND creator_id=%s" % int(row["creator_id"])
        return ("DELETE", "DELETE FROM `%s` WHERE id=%s%s LIMIT 1" % (table, rid, extra))
    elif act["action"] == "update":
        before = act.get("before") or {}
        if not before:
            return (None, "SKIP: update act without before-snapshot（无法反推）")
        sets, params = [], []
        for k in before:
            sets.append("`%s`=%%s" % k)
            params.append(before[k])
        return ("UPDATE", "UPDATE `%s` SET %s WHERE id=%s LIMIT 1"
                % (table, ", ".join(sets), rid), params)
    return (None, "SKIP: unknown action %r" % act.get("action"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("audit_file", nargs="?", help="write_audit_*.jsonl 路径")
    ap.add_argument("--lines", help="1-based 行号，如 3 或 3-5,7")
    ap.add_argument("--last", type=int, default=0, help="取文件最后 N 行")
    ap.add_argument("--dir", default=os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                  "audit"))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--yes", action="store_true")
    ap.add_argument("--host", default=os.getenv("MH5_DB_HOST", ""))
    ap.add_argument("--port", type=int, default=int(os.getenv("MH5_DB_PORT", "3306")))
    ap.add_argument("--user", default=os.getenv("MH5_DB_USER", ""))
    ap.add_argument("--password", default=os.getenv("MH5_DB_PASSWORD", ""))
    ap.add_argument("--db", default=os.getenv("MH5_DB_NAME", "huafu"))
    a = ap.parse_args()

    path = a.audit_file or newest_audit_file(a.dir)
    entries = [json.loads(l) for l in open(path, encoding="utf-8").read().splitlines()
               if l.strip()]
    print("audit file: %s（%d 条）" % (path, len(entries)))
    if a.last:
        picks = list(range(len(entries) - a.last + 1, len(entries) + 1))
    elif a.lines:
        picks = parse_lines(a.lines, len(entries))
    else:
        raise SystemExit("需 --lines 或 --last 指定要回滚的审计行")

    rows = [i for i in picks]
    print("selected entry lines: %s" % rows)
    for i in rows:
        e = entries[i - 1]
        print("\n--- entry#%d ts=%s req=%s uid=%s name=%s" %
              (i, e.get("ts"), e.get("req"), e.get("uid"), e.get("name")))
        for act in e.get("acts") or []:
            kind_payload = build_sql(act)
            if kind_payload[0] == "DELETE":
                print("  [%s] %s" % (act["action"], kind_payload[1]))
            elif kind_payload[0] == "UPDATE":
                print("  [%s] %s || params=%s"
                      % (act["action"], kind_payload[1], kind_payload[2]))
            else:
                print("  %s" % kind_payload[1])
    if a.dry_run or not a.yes:
        print("\n[dry-run] 未落库。确认无误后追加 --yes 执行。")
        return

    try:
        import pymysql
    except ImportError:
        # 服务器部署自带依赖在 /opt/market_h5/lib（与 systemd 的 PYTHONPATH 一致）
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
        import pymysql
    if not (a.host and a.user and a.password):
        raise SystemExit("缺少 DB 连接参数（--host/--user/--password 或 MH5_DB_* 环境变量）")
    conn = pymysql.connect(host=a.host, port=a.port, user=a.user, password=a.password,
                           db=a.db, autocommit=False, charset="utf8mb4",
                           cursorclass=pymysql.cursors.DictCursor)
    ok = skip = fail = 0
    try:
        with conn.cursor() as cur:
            for i in rows:
                e = entries[i - 1]
                for act in e.get("acts") or []:
                    kind_payload = build_sql(act)
                    if kind_payload[0] not in ("DELETE", "UPDATE"):
                        skip += 1
                        continue
                    table_, rid = act["table"], act["id"]
                    try:
                        if kind_payload[0] == "DELETE":
                            cur.execute("SELECT 1 c FROM `%s` WHERE id=%s" % (table_, rid))
                            if not cur.fetchone():
                                print("  entry#%d %s id=%s 已不存在，跳过" % (i, table_, rid))
                                skip += 1
                                continue
                            cur.execute(kind_payload[1])
                        else:
                            sql, params = kind_payload[1], kind_payload[2]
                            cur.execute(sql, params)
                        ok += 1
                    except Exception as ex:
                        fail += 1
                        print("  !! entry#%d %s id=%s FAILED: %s"
                              % (i, table_, rid, str(ex)[:200]))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    print("\n[ROLLBACK DONE] ok=%d skip=%d fail=%d（审计文件已保留，本次撤销记录见 journalctl）"
          % (ok, skip, fail))


if __name__ == "__main__":
    main()
