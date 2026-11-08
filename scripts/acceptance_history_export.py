"""Verify real-school-model history exports using a new isolated database."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import sys
import uuid
from pathlib import Path

import httpx
from openpyxl import load_workbook

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from consumable_assistant import workflows
from consumable_assistant.chat import _fill_versions
from consumable_assistant.database import connect
from consumable_assistant.server import dispatch
from scripts.acceptance_school_lifecycle import digest
from scripts.acceptance_school_model import (
    prepare_database,
    preview,
    start_isolated_server,
    stop_server,
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    database = prepare_database(args.database)
    actor = '历史导出验证教师'
    name = '导出验证' + uuid.uuid4().hex[:8] + '氯化钠'
    with connect(database) as conn:
        def write(item, action, quantity, author=actor, timestamp='2026-11-07 17:00:00'):
            result = dispatch(conn, 'stock', _fill_versions(conn, 'stock', dict(
                item_id=item, action=action, quantity=quantity)), author)
            conn.execute('UPDATE stock_events SET created_at=? WHERE id=?', (timestamp, result['id']))
            return result['id']

        items = []
        for item_name in (name, '无关验证氯化钠'):
            item = workflows.create_item(conn, dict(item_name=item_name, spec='分析纯', unit='克',
                                                    lab='导出验证实验室', location_code='E1'))
            items.append(item)
            conn.execute("UPDATE stock_balances SET ready='1' WHERE item_id=?", (item,))
            conn.execute('UPDATE inventory_items SET quantity=1 WHERE id=?', (item,))
        event = write(items[0], 'consume', '0.1')
        write(items[0], 'inbound', '0.4')
        write(items[0], 'consume', '0.2', author='另一位验证教师')
        write(items[0], 'consume', '0.05', timestamp='2026-11-06 17:00:00')
        write(items[1], 'consume', '0.3')
    before = digest(database)
    process, base = start_isolated_server(database, 2)
    report = dict(model='deepseek-pro', endpoint='/v1/responses', results=[])
    args.report.parent.mkdir(parents=True, exist_ok=True)
    try:
        with httpx.Client(base_url=base, trust_env=False, timeout=20) as client:
            for repeat in range(3):
                case = preview(base, dict(message=
                    f'请直接导出{name}在上海时间2026年11月8日当天、操作人为{actor}、'
                    '操作类型为消耗的实际业务历史，生成Excel下载文件。必须同时使用耗材名称、操作人、'
                    '操作类型、开始日期和结束日期筛选。不要修改任何业务，也不要建立待确认计划。'))
                response = case['response']
                assert response.get('status') == 'answered', response
                exports = [tool['result'] for tool in response.get('tool_results', [])
                           if tool['tool'] == 'export_history']
                assert len(exports) == 1, response
                result = exports[0]
                assert result['row_count'] == 1, result
                download = client.get(result['url'])
                download.raise_for_status()
                content = download.content
                assert hashlib.sha256(content).hexdigest() == result['sha256']
                workbook = load_workbook(io.BytesIO(content))
                rows = list(workbook['业务历史'].values)
                assert len(rows) == 2, rows
                row = dict(zip(rows[0], rows[1], strict=True))
                assert (row['id'], row['item_id'], row['delta_quantity'], row['quantity_after']) == (
                    event, items[0], '-0.1', '0.9'), row
                assert row['item_name'] == name and row['actor'] == actor
                metadata = dict(list(workbook['导出说明'].values)[1:])
                assert metadata['row_count'] == 1 and metadata['actor'] == actor
                assert metadata['action'] == 'consume' and metadata['q'] == name
                assert metadata['from_date'] == metadata['to_date'] == '2026-11-08'
                assert digest(database) == before, 'Read-only export changed business state'
                report['results'].append(dict(repeat=repeat, run_id=case['run_id'],
                    conversation_id=case['conversation_id'], passed=True, export_id=result['id'],
                    sha256=result['sha256'], filters=result['filters'], event_id=event))
                args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2))
                print(json.dumps(report['results'][-1], ensure_ascii=False), flush=True)
        report.update(passed=3, unconfirmed_business_changes=0)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    finally:
        stop_server(process)


if __name__ == '__main__':
    raise SystemExit(main())
