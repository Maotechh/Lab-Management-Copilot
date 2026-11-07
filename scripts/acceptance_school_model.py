"""Real campus-model acceptance against an explicitly isolated local instance.

No production URL is accepted. Previews run concurrently; confirmations happen
only after a global business-data digest proves previews made no changes.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from consumable_assistant import ledger as stock
from consumable_assistant import workflows as flow
from consumable_assistant.database import connect, init_db

ROOT = Path(__file__).resolve().parents[1]
APP_PORT = int(os.environ.get('LAB_ACCEPTANCE_PORT', '8001'))
DSH_HEALTH_URL = os.environ.get('LAB_DSH_URL', 'http://127.0.0.1:8767').rstrip('/') + '/health'

TABLES = ('inventory_items', 'stock_balances', 'stock_events', 'loans', 'schedules',
          'reservations', 'purchase_orders', 'preparations', 'lesson_templates')


def digest(path):
    with connect(path) as conn:
        data = {name: [tuple(row) for row in conn.execute(f'SELECT * FROM {name} ORDER BY 1')] for name in TABLES}
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def fixtures(path):
    cases = []
    stamp = uuid.uuid4().hex[:6]
    for operation in ('consume', 'inbound', 'borrow', 'return', 'count', 'threshold',
                      'state', 'draft', 'order', 'transfer', 'preparation', 'metadata'):
        for quantity in range(2, 7):
            name = f'验证{stamp}{operation}{quantity}烧杯'
            with connect(path) as conn:
                item = flow.create_item(conn, dict(item_name=name, spec='100mL', unit='个', lab='验证实验室', location_code='A1'))
                conn.execute("UPDATE stock_balances SET ready='100' WHERE item_id=?", (item,))
                conn.execute('UPDATE inventory_items SET quantity=100 WHERE id=?', (item,))
                if operation == 'return':
                    stock.operate(conn, dict(item_id=item, version=1, action='borrow', quantity='10', borrower='验证教师', purpose='验证实验', destination='验证教室', due_at='2026-12-01T12:00:00+08:00'), 'Fixture')
                if operation == 'state':
                    conn.execute("UPDATE stock_balances SET ready='90',cleaning='10' WHERE item_id=?", (item,))
                conn.commit()
            reference = f'验证实验室A1的100mL{name}'
            texts = {
                'consume': f'{reference}实际消耗{quantity}个，不归还。',
                'inbound': f'{reference}实际新增入库{quantity}个。',
                'borrow': f'{reference}借用{quantity}个，借用人验证教师，用途验证实验，放在验证教室，2026年12月1日12点归还。',
                'return': f'{reference}的验证教师原借用记录，部分归还{quantity}个，状态待清洗，请查询并关联原借用记录。',
                'count': f'{reference}清点实际数量为{quantity}个，原因是实物清点。',
                'threshold': f'{reference}预警阈值改为{quantity}个。',
                'state': f'{reference}已有待清洗物品中{quantity}个已经清洗完成，转为可用状态。',
                'draft': f'{reference}建立采购草案{quantity}个，尚未订购。',
                'order': f'{reference}登记已订购{quantity}个，负责人验证教师，预计2026年12月1日12点到货，尚未到货。',
                'transfer': f'{reference}转移{quantity}个可用物品到同一验证实验室的B1柜，实验室不变。',
                'preparation': f'建立完整实验准备，名称{name}实验，课程验证化学，地点验证教室，2026年12月2日9点至12点，{quantity}人，每组1人，{reference}每人消耗30个，另加备用1个。数量不足时预留已有数量并保存采购草案。',
                'metadata': f'{reference}备注改为验证备注{quantity}。',
            }
            cases.append(dict(operation=operation, quantity=quantity, item_id=item, name=name,
                              message=texts[operation] + '请先查询实际记录，再生成一项完整待确认计划。'))
    return cases


def preview(base, case):
    with httpx.Client(base_url=base, trust_env=False, timeout=20) as client:
        headers = {'X-Actor': 'Acceptance-teacher', 'X-Device-ID': 'acceptance-desktop'}
        response = client.post('/api/chat/conversations', headers=headers)
        response.raise_for_status()
        conversation = response.json()['conversation_id']
        response = client.post(f'/api/chat/conversations/{conversation}/messages', headers=headers,
                               json={'message_id': uuid.uuid4().hex, 'text': case['message']})
        response.raise_for_status()
        run_id = response.json()['run_id']
        deadline = time.monotonic() + 330
        while time.monotonic() < deadline:
            run = client.get('/api/chat/runs/' + run_id).json()
            if run['status'] not in ('queued', 'running'):
                result = run.get('result') or {}
                return {**case, 'conversation_id': conversation, 'run_id': run_id, 'response': result}
            time.sleep(0.5)
        return {**case, 'run_id': run_id, 'response': {'status': 'failed', 'reply': 'Acceptance timeout'}}


def check_plan(case):
    response = case['response']
    if response.get('status') != 'pending_confirmation':
        return False
    operation = response['preview']['operation']
    details = operation['details']
    kind, quantity = case['operation'], str(case['quantity'])
    if kind in ('consume', 'inbound', 'borrow', 'return', 'count', 'threshold', 'state'):
        return operation['kind'] == 'stock' and details.get('item_id') == case['item_id'] and details.get('action') == kind and stock.text(details.get('quantity')) == quantity and (kind != 'return' or details.get('return_state') == 'cleaning')
    if kind in ('draft', 'order'):
        return operation['kind'] == 'purchase' and details.get('action') == kind and details.get('item_id') == case['item_id'] and stock.text(details.get('quantity')) == quantity
    if kind == 'preparation':
        result = response['preview']['business_result']
        line = result['lines'][0]
        return operation['kind'] == 'preparation' and result['people'] == case['quantity'] and line['required_quantity'] == str(case['quantity'] * 30 + 1) and line['reserved_quantity'] == str(min(100, case['quantity'] * 30 + 1)) and line['unmet_quantity'] == str(max(0, case['quantity'] * 30 - 99))
    if kind == 'transfer':
        return operation['kind'] == kind and details.get('target_location') == 'B1' and details.get('target_lab') == '验证实验室' and stock.text(details.get('quantity')) == quantity
    return operation['kind'] == 'metadata' and details.get('item_id') == case['item_id'] and details.get('remark') == '验证备注' + quantity


def verify_quantity(path, case):
    with connect(path) as conn:
        current = stock.view(conn, case['item_id'])
        n, kind = case['quantity'], case['operation']
        expected = {'consume': 100-n, 'inbound': 100+n, 'borrow': 100-n, 'return': 90,
                    'count': n, 'threshold': 100, 'state': 90+n, 'draft': 100, 'order': 100,
                    'transfer': 100-n, 'preparation': 100, 'metadata': 100}[kind]
        assert current['ready'] == str(expected), (kind, current['ready'], expected)
        if kind == 'return':
            assert current['cleaning'] == str(n) and current['borrowed_quantity'] == str(10-n)
        if kind == 'preparation':
            assert current['reserved_quantity'] == str(min(100, n * 30 + 1))
        if kind in ('draft', 'order'):
            order = conn.execute('SELECT * FROM purchase_orders WHERE item_id=?', (case['item_id'],)).fetchone()
            assert order['status'] == ('draft' if kind == 'draft' else 'ordered') and order['received'] == '0'


def prepare_database(path: Path) -> Path:
    target = path.resolve()
    acceptance_root = (ROOT / 'data' / 'acceptance').resolve()
    try:
        target.relative_to(acceptance_root)
    except ValueError as exc:
        raise ValueError('The acceptance database must be under data/acceptance') from exc
    if target.suffix != '.db':
        raise ValueError('The acceptance database must use the .db extension')
    if target.exists():
        raise ValueError('Choose a new acceptance database path; existing files are never modified')
    target.parent.mkdir(parents=True, exist_ok=True)
    with connect(target) as conn:
        init_db(conn)
    return target


def start_isolated_server(database: Path, expected_items: int) -> tuple[subprocess.Popen, str]:
    with socket.socket() as probe:
        try:
            probe.bind(('127.0.0.1', APP_PORT))
        except OSError as exc:
            raise RuntimeError(f'Port {APP_PORT} is busy; no acceptance requests were sent') from exc

    env = os.environ.copy()
    env.update({
        'CONSUMABLE_PROFILE': 'production',
        'CONSUMABLE_DB_PATH': str(database),
        'CONSUMABLE_AUTO_SEED': '0',
        'LAB_DSH_URL': DSH_HEALTH_URL.removesuffix('/health'),
        'CONSUMABLE_CHAT_UPLOAD_DIR': str(database.parent / f'{database.stem}-uploads'),
        'CONSUMABLE_PHOTO_DIR': str(database.parent / f'{database.stem}-photos'),
    })
    process = subprocess.Popen(
        [sys.executable, '-m', 'uvicorn', 'consumable_assistant.server:app',
         '--host', '127.0.0.1', '--port', str(APP_PORT), '--workers', '1'],
        cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    base = f'http://127.0.0.1:{APP_PORT}'
    deadline = time.monotonic() + 45
    try:
        with httpx.Client(base_url=base, trust_env=False, timeout=2) as client:
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError('The isolated application process exited before it became ready')
                try:
                    config = client.get('/api/config')
                    stats = client.get('/api/stats')
                    if config.status_code == 200 and stats.status_code == 200:
                        if config.json().get('profile') != 'production':
                            raise RuntimeError('The application did not start in production profile')
                        if stats.json().get('item_count') != expected_items:
                            raise RuntimeError('The application is not using the fixture database')
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.25)
            else:
                raise RuntimeError('The isolated application did not become ready')
        with httpx.Client(trust_env=False, timeout=5) as client:
            response = client.get(DSH_HEALTH_URL)
            response.raise_for_status()
    except Exception:
        stop_server(process)
        raise
    return process, base


def stop_server(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--repeat', type=int, default=1, choices=range(1, 4))
    args = parser.parse_args()
    database = prepare_database(args.database)
    cases = []
    for _ in range(args.repeat):
        cases.extend(fixtures(database))
    original = digest(database)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    process, base = start_isolated_server(database, len(cases))
    results = []
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            pending = {pool.submit(preview, base, case): case for case in cases}
            for future in concurrent.futures.as_completed(pending):
                case = future.result()
                try:
                    case['plan_correct'] = check_plan(case)
                except (KeyError, IndexError, TypeError, ValueError):
                    case['plan_correct'] = False
                results.append(case)
                args.report.write_text(json.dumps({'results': results}, ensure_ascii=False, indent=2))
                print(json.dumps({key: case[key] for key in ('operation','quantity','plan_correct','run_id')}), flush=True)
        assert digest(database) == original, 'Unconfirmed business data changed'
        with httpx.Client(base_url=base, trust_env=False, timeout=20) as client:
            for case in results:
                if not case['plan_correct']:
                    continue
                task = case['response']
                payload = {'request_id': uuid.uuid4().hex, 'plan_hash': task['plan_hash']}
                headers = {'X-Actor': 'Acceptance-confirmer', 'X-Device-ID': 'acceptance-phone'}
                route = '/api/chat/tasks/' + task['task_id'] + '/confirm'
                response = client.post(route, headers=headers, json=payload)
                case['confirmed'] = response.status_code == 200
                if case['confirmed']:
                    assert client.post(route, headers=headers, json=payload).json() == response.json()
                    verify_quantity(database, case)
        passed = sum(case['plan_correct'] and case.get('confirmed', False) for case in results)
        combinations = {(case['operation'], case['quantity']) for case in results}
        repeated_scenarios_passed = all(
            sum(case['operation'] == operation and case['quantity'] == quantity
                and case['plan_correct'] and case.get('confirmed', False) for case in results) == args.repeat
            for operation, quantity in combinations
        )
        report = {'school_model': 'deepseek-pro', 'scenarios': len(results), 'passed': passed,
                  'rate': passed / len(results), 'required_scenarios_passed_repeatedly': repeated_scenarios_passed,
                  'unconfirmed_business_changes': 0, 'wrong_executed_quantities': 0, 'results': results}
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(json.dumps({key:value for key,value in report.items() if key != 'results'}))
        return 0 if report['rate'] >= .95 and repeated_scenarios_passed else 1
    finally:
        stop_server(process)


if __name__ == '__main__':
    raise SystemExit(main())
