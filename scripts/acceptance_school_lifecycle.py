"""Additional real-school-model lifecycle checks in a new fixture database.

Never accepts a remote application URL. Every preview is checked before any
confirmation, including complete business-state digests and exact quantities.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import sys
import uuid
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from consumable_assistant import ledger as stock
from consumable_assistant import preparations
from consumable_assistant import workflows as flow
from consumable_assistant.chat import _fill_versions
from consumable_assistant.database import connect
from consumable_assistant.server import dispatch
from scripts.acceptance_school_model import (
    prepare_database,
    preview,
    start_isolated_server,
    stop_server,
)

SCENARIOS = (
    'purchase_place', 'purchase_receive', 'purchase_cancel',
    'preparation_revise', 'preparation_replenish', 'preparation_cancel',
    'execute_consume', 'execute_borrow', 'cancel_schedule',
    'return_damaged', 'return_lost', 'undo',
    'alert_acknowledge', 'alert_snooze', 'alert_remind',
    'location_details', 'archive_location', 'create_item', 'template', 'mixed',
)
TABLES = (
    'inventory_items', 'item_aliases', 'inventory_transactions',
    'stock_balances', 'stock_events', 'stock_counts', 'loans', 'schedules',
    'reservations', 'purchase_orders', 'preparations', 'preparation_revisions',
    'preparation_schedules', 'preparation_purchases', 'lesson_templates',
    'alert_states', 'location_details', 'notification_events', 'push_deliveries',
)


def digest(database):
    with connect(database) as conn:
        data = {name: [tuple(row) for row in conn.execute(f'SELECT * FROM {name} ORDER BY 1')]
                for name in TABLES}
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def fixture(conn, scenario, repeat):
    name = f'流程验证{uuid.uuid4().hex[:8]}烧杯'
    location = f'L{repeat}-{scenario}'
    item = flow.create_item(conn, dict(item_name=name, spec='100mL', unit='个',
                                      lab='流程验证实验室', location_code=location))
    amount = '5' if scenario.startswith('preparation_') else '10'
    if scenario == 'archive_location':
        amount = '0'
    conn.execute('UPDATE stock_balances SET ready=? WHERE item_id=?', (amount, item))
    conn.execute('UPDATE inventory_items SET quantity=? WHERE id=?', (amount, item))
    stock.alerts_changed(conn, item)
    reference = f'流程验证实验室{location}柜的100mL{name}'
    case = dict(operation=scenario, quantity=0, item_id=item, name=name, location=location, repeat=repeat)

    def write(kind, data):
        return dispatch(conn, kind, _fill_versions(conn, kind, data), 'Fixture')

    def expect(kind, details, text, **extra):
        case.update(expected_kind=kind, expected_details=details, message=text +
                    '请先查询实际记录，再生成一项完整待确认计划。不要直接执行。', **extra)

    if scenario.startswith('purchase_'):
        order = write('purchase', {'action': 'draft', 'item_id': item, 'quantity': '8'})['id']
        if scenario != 'purchase_place':
            write('purchase', dict(action='place', order_id=order, owner='验证教师',
                                   expected_at='2026-12-01T12:00:00+08:00'))
        if scenario == 'purchase_cancel':
            write('purchase', dict(action='receive', order_id=order, quantity='3'))
        action = scenario.removeprefix('purchase_')
        text = {
            'place': f'{reference}现有八个采购草案现在登记为已经订购，负责人验证教师，预计2026年12月1日12点到货。尚未到货。',
            'receive': f'{reference}现有八个采购订单本次只到货三个，请关联原订单登记部分到货入库，其余五个尚未到货。',
            'cancel': f'{reference}现有八个采购订单已经到货三个，现在取消尚未到货的部分，原因是课程人数减少，保留三个到货及入库记录。',
        }[action]
        details = dict(action=action, order_id=order)
        if action == 'receive':
            details['quantity'] = '3'
        expect('purchase', details, text, order_id=order)
    elif scenario.startswith('preparation_'):
        preparation = write('preparation', dict(
            name=name + '实验', course='验证化学', venue='验证教室',
            starts_at='2026-12-02T09:00:00+08:00', ends_at='2026-12-02T12:00:00+08:00',
            people=5, group_size=2,
            lines=[dict(item_ids=[item], action='borrow', per_group='2', spare='1')]))
        if scenario == 'preparation_replenish':
            order = preparation['purchase_draft_ids'][0]
            write('purchase', dict(action='place', order_id=order, owner='验证教师',
                                   expected_at='2026-12-01T12:00:00+08:00'))
            write('purchase', dict(action='receive', order_id=order, quantity='1'))
        action = scenario.removeprefix('preparation_')
        text = {
            'revise': f'把已有的{name}实验准备从五个人改为三个人，仍然每组两人，每组借两个{reference}，备用一个，其他信息不变。原因是参加人数减少，修改原准备并重新计算。',
            'replenish': f'已有{name}实验准备原来需要七个{reference}，预留五个，差两个；关联订单现在已到货一个。为原准备补充预留这个到货物品，原因是部分到货。不要建立新的实验准备。',
            'cancel': f'取消已有{name}实验准备，原因是课程取消。解除有关预留，并取消尚未订购的采购草案。',
        }[action]
        details = dict(action=action, preparation_id=preparation['id'])
        if action == 'revise':
            details['people'] = 3
        expect('preparation_update', details, text, preparation_id=preparation['id'])
    elif scenario.startswith('execute_') or scenario == 'cancel_schedule':
        action = 'borrow' if scenario == 'execute_borrow' else 'consume'
        schedule = write('schedule', dict(name=name + '实验', course='验证化学', venue='验证教室',
            starts_at='2026-12-02T09:00:00+08:00', ends_at='2026-12-02T12:00:00+08:00',
            lines=[dict(item_id=item, action=action, quantity='4')]))['id']
        if scenario == 'cancel_schedule':
            expect('cancel_schedule', dict(schedule_id=schedule),
                   f'取消现有{name}实验预留，原因是课程取消，不取用{reference}。', schedule_id=schedule)
        else:
            expect('execute_schedule', dict(schedule_id=schedule),
                   f'现有{name}实验已经准备取用四个{reference}，现在登记执行原实验安排，' +
                   ('实际借用，借用人为验证教师，按照原安排归还。' if action == 'borrow' else '实际消耗，不归还。'),
                   schedule_id=schedule)
    elif scenario.startswith('return_'):
        loan = write('stock', dict(item_id=item, action='borrow', quantity='4',
            borrower='验证教师', purpose='验证实验', destination='验证教室',
            due_at='2026-12-01T12:00:00+08:00'))['loan_id']
        state = scenario.removeprefix('return_')
        expect('stock', dict(item_id=item, action='return', loan_id=loan, quantity='2', return_state=state),
               f'{reference}的验证教师原来借了四个，其中两个已经' +
               ('破损' if state == 'damaged' else '遗失') +
               '，请关联原借用记录处理这两个，剩下两个仍然借出，不增加可用库存。', loan_id=loan)
    elif scenario == 'undo':
        event = write('stock', dict(item_id=item, action='consume', quantity='3', note='验证误登记'))['id']
        expect('undo', dict(event_id=event), f'撤销{reference}最近一笔消耗三个的错误记录。尚未发生后续业务操作。', event_id=event)
    elif scenario.startswith('alert_'):
        write('stock', dict(item_id=item, action='threshold', quantity='20'))
        event = conn.execute('SELECT event_id FROM alert_states WHERE item_id=?', (item,)).fetchone()[0]
        action = scenario.removeprefix('alert_')
        if action == 'remind':
            write('alert', dict(item_id=item, event_id=event, action='acknowledge'))
        details = dict(item_id=item, event_id=event, action=action)
        if action == 'snooze':
            details['until'] = '2026-12-01T12:00:00+08:00'
        text = {'acknowledge': '将当前低库存提醒标记为已经知悉。',
                'snooze': '暂缓当前低库存提醒，截止2026年12月1日12点。',
                'remind': '恢复低库存提醒，取消已知悉状态，继续提醒。'}[action]
        expect('alert', details, reference + text)
    elif scenario == 'location_details':
        expect('location_details', dict(lab='流程验证实验室', location_code=location, description='玻璃仪器专用柜'),
               f'把{reference}所在柜子的位置说明改为玻璃仪器专用柜。')
    elif scenario == 'archive_location':
        expect('archive_location', dict(lab='流程验证实验室', location_code=location),
               f'停用{reference}所在的空柜，原因是柜子停止使用。该柜数量为零。')
    elif scenario == 'create_item':
        new_name = name + '新规格'
        expect('create_item', dict(item_name=new_name, spec='250mL', unit='个',
                                  lab='流程验证实验室', location_code=location),
               f'在流程验证实验室{location}柜新增耗材资料，名称{new_name}，规格250mL，单位个，目前数量为零。不要登记入库。', new_name=new_name)
    elif scenario == 'template':
        expect('template', dict(name=name + '模板', course='验证化学'),
               f'保存新的课程模板，名称{name}模板，课程验证化学，每次固定消耗两个{reference}，不实际预留或取用。')
    else:
        expect('mixed', {}, f'{reference}本次实际消耗两个，并且建立八个的采购草案，尚未订购。合为一项混合计划，只确认一次。')
    conn.commit()
    return case


def check_plan(case):
    result = case['response']
    if result.get('status') != 'pending_confirmation':
        return False
    operation = result['preview']['operation']
    if operation['kind'] == 'mixed' and case['expected_kind'] != 'mixed':
        steps = operation['details'].get('operations', [])
        if len(steps) == 1:
            operation = {'kind': steps[0]['kind'], 'details': steps[0]['payload']}
    if operation['kind'] != case['expected_kind']:
        return False
    details = operation['details']
    for key, value in case['expected_details'].items():
        actual = details.get(key)
        if key == 'quantity':
            actual = stock.text(actual)
        if actual != value:
            return False
    if case['operation'] == 'mixed':
        steps = details.get('operations', [])
        return len(steps) == 2 and all(any(
            row['kind'] == kind and row['payload'].get('action') == action and
            row['payload'].get('item_id') == case['item_id'] and
            stock.text(row['payload'].get('quantity')) == quantity
            for row in steps) for kind, action, quantity in [('stock', 'consume', '2'), ('purchase', 'draft', '8')])
    if case['operation'] == 'template':
        lines = details.get('lines', [])
        return len(lines) == 1 and lines[0]['item_id'] == case['item_id'] and lines[0]['action'] == 'consume' and stock.text(lines[0].get('fixed')) == '2'
    return True


def verify(database, case):
    kind, item = case['operation'], case['item_id']
    with connect(database) as conn:
        current = stock.view(conn, item)
        ready = {'purchase_receive': '13', 'purchase_cancel': '13',
                 'preparation_revise': '5', 'preparation_replenish': '6', 'preparation_cancel': '5',
                 'execute_consume': '6', 'execute_borrow': '6', 'return_damaged': '6',
                 'return_lost': '6', 'archive_location': '0', 'mixed': '8'}.get(kind, '10')
        assert current['ready'] == ready, (kind, current)
        if kind.startswith('purchase_'):
            order = dict(conn.execute('SELECT * FROM purchase_orders WHERE id=?', (case['order_id'],)).fetchone())
            assert order['status'] == ('cancelled' if kind == 'purchase_cancel' else 'ordered')
            assert order['received'] == ('0' if kind == 'purchase_place' else '3')
            assert order['quantity'] == '8'
        elif kind.startswith('preparation_'):
            result = preparations.view(conn, case['preparation_id'])
            line = result['lines'][0]
            assert len(conn.execute('SELECT * FROM preparation_revisions WHERE preparation_id=?', (result['id'],)).fetchall()) == 1
            if kind == 'preparation_cancel':
                assert result['status'] == 'cancelled' and current['reserved_quantity'] == '0'
                assert all(row['status'] == 'cancelled' for row in result['purchases'] + result['schedules'])
            else:
                assert line['required_quantity'] == ('5' if kind == 'preparation_revise' else '7')
                assert line['reserved_quantity'] == ready
                assert line['unmet_quantity'] == ('0' if kind == 'preparation_revise' else '1')
                assert current['reserved_quantity'] == ready
        elif kind.startswith('execute_') or kind == 'cancel_schedule':
            status = conn.execute('SELECT status FROM schedules WHERE id=?', (case['schedule_id'],)).fetchone()[0]
            assert status == ('cancelled' if kind == 'cancel_schedule' else 'executed')
            assert current['reserved_quantity'] == '0'
            assert current['borrowed_quantity'] == ('4' if kind == 'execute_borrow' else '0')
            assert current['owned_quantity'] == ('6' if kind == 'execute_consume' else '10')
        elif kind.startswith('return_'):
            assert current['borrowed_quantity'] == '2' and current['owned_quantity'] == '8'
        elif kind == 'undo':
            assert conn.execute('SELECT reversed_by FROM stock_events WHERE id=?', (case['event_id'],)).fetchone()[0]
        elif kind.startswith('alert_'):
            alert = conn.execute('SELECT * FROM alert_states WHERE item_id=?', (item,)).fetchone()
            assert alert['acknowledged'] == int(kind == 'alert_acknowledge')
            assert alert['snooze_until'] == (stock.moment('2026-12-01T12:00:00+08:00') if kind == 'alert_snooze' else None)
        elif kind == 'location_details':
            assert conn.execute('SELECT description FROM location_details WHERE lab=? AND location_code=?',
                                ('流程验证实验室', case['location'])).fetchone()[0] == '玻璃仪器专用柜'
        elif kind == 'archive_location':
            assert current['active'] == 0
        elif kind == 'create_item':
            created = conn.execute('SELECT id FROM inventory_items WHERE item_name=?', (case['new_name'],)).fetchone()[0]
            assert stock.view(conn, created)['owned_quantity'] == '0'
        elif kind == 'template':
            lines = json.loads(conn.execute('SELECT lines FROM lesson_templates WHERE name=?', (case['name'] + '模板',)).fetchone()[0])
            assert len(lines) == 1 and stock.text(lines[0]['fixed']) == '2'
            assert current['reserved_quantity'] == '0'
        elif kind == 'mixed':
            order = conn.execute('SELECT * FROM purchase_orders WHERE item_id=?', (item,)).fetchone()
            assert order['status'] == 'draft' and order['quantity'] == '8' and order['received'] == '0'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--repeat', type=int, default=3, choices=range(1, 4))
    args = parser.parse_args()
    database = prepare_database(args.database)
    with connect(database) as conn:
        cases = [fixture(conn, scenario, repeat) for scenario in SCENARIOS for repeat in range(args.repeat)]
    args.report.parent.mkdir(parents=True, exist_ok=True)
    process, base = start_isolated_server(database, len(cases))
    report = dict(school_model='deepseek-pro', endpoint='/v1/responses', repeat=args.repeat,
                  scenarios=len(cases), results=[], unconfirmed_business_changes=0, wrong_executed_quantities=0)
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool, httpx.Client(
            base_url=base, trust_env=False, timeout=20,
            headers={'X-Actor': 'Lifecycle-confirmer', 'X-Device-ID': 'lifecycle-phone'},
        ) as client:
            for offset in range(0, len(cases), 3):
                before = digest(database)
                batch = list(pool.map(lambda case: preview(base, case), cases[offset:offset + 3]))
                assert digest(database) == before, 'Unconfirmed business state changed'
                for case in batch:
                    case['plan_correct'] = check_plan(case)
                    if case['plan_correct']:
                        task = case['response']
                        payload = dict(request_id=uuid.uuid4().hex, plan_hash=task['plan_hash'])
                        route = '/api/chat/tasks/' + task['task_id'] + '/confirm'
                        response = client.post(route, json=payload)
                        response.raise_for_status()
                        after = digest(database)
                        assert client.post(route, json=payload).json() == response.json()
                        assert digest(database) == after, 'Repeated confirmation changed business state'
                        verify(database, case)
                        case['confirmed'] = True
                    report['results'].append(case)
                    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2))
                    print(json.dumps({key: case[key] for key in ('operation', 'repeat', 'plan_correct', 'run_id')}), flush=True)
        report['passed'] = sum(case['plan_correct'] and case.get('confirmed', False) for case in report['results'])
        report['all_required_repeats_passed'] = report['passed'] == len(cases)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(json.dumps({key: value for key, value in report.items() if key != 'results'}))
        return 0 if report['all_required_repeats_passed'] else 1
    finally:
        stop_server(process)


if __name__ == '__main__':
    raise SystemExit(main())
