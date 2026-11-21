import hashlib
import json
import uuid

from . import ledger as stock
from .attachments import uploaded_frame
from .matching import cabinet_codes, matches_cabinet, physical_room, same_material
from .normalization import clean_cell, normalize_text
from .services import resolve_upload_column


def batches(conn):
    return [dict(row) for row in conn.execute("""SELECT p.id,p.filename,p.mode,p.created_at,
        EXISTS(SELECT 1 FROM upload_events u WHERE u.batch_id=p.id) AS tracked
        FROM upload_previews p WHERE p.committed=1 AND p.mode IN ('add','overwrite','count')
        ORDER BY p.created_at DESC,p.rowid DESC""")]


def batch_detail(conn, batch_id):
    groups = {}
    for row in conn.execute("""SELECT u.item_id,u.created_item,e.* FROM upload_events u
            JOIN stock_events e ON e.id=u.event_id WHERE u.batch_id=? ORDER BY e.id""", (batch_id,)):
        snapshot = json.loads(row['snapshot'])
        entry = groups.setdefault(row['item_id'], {**snapshot, 'item_id': row['item_id'], 'events': [],
            'before': stock.text(stock.number(snapshot['quantity_after']) - stock.number(json.loads(row['changes']).get('ready'))),
            'created_item': bool(row['created_item']), 'reversed': 0})
        entry['after'] = snapshot['quantity_after']
        entry['events'].append(row['id'])
        entry['reversed'] += int(bool(row['reversed_by']))
    for entry in groups.values():
        later = conn.execute("SELECT 1 FROM stock_events WHERE item_id=? AND id>? LIMIT 1",
                             (entry['item_id'], entry['events'][-1])).fetchone()
        entry['status'] = ('已撤销' if entry['reversed'] == len(entry['events']) else '需重新清点'
                           if later or entry['reversed'] else '可撤销')
    return list(groups.values())


def archive_item(conn, data, actor):
    item = stock.view(conn, int(data['item_id']))
    stock.check_version(item, data.get('version'))
    if not item['active'] or item['review_reason'] or stock.number(item['owned_quantity']) or stock.number(item['reserved_quantity']):
        raise ValueError('请先清点并处理库存、借用和预留，再停用该记录')
    if conn.execute("SELECT 1 FROM purchase_orders WHERE item_id=? AND status IN ('draft','ordered')", (item['item_id'],)).fetchone():
        raise ValueError('此记录还有未完成的采购')
    conn.execute('UPDATE stock_balances SET active=0,version=version+1 WHERE item_id=?', (item['item_id'],))
    stock.event(conn, item['item_id'], 'archive', {}, actor, stock.required(data, 'note'))
    return {'ok': True}


def undo_batch(conn, data, actor):
    rows = batch_detail(conn, data['batch_id'])
    eligible = [row for row in rows if row['status'] == '可撤销']
    if not eligible or {row['item_id'] for row in eligible} != set(data['item_ids']):
        raise stock.Conflict('可撤销范围已经变化，请重新查看上传批次')
    for row in eligible:
        ignored = set()
        for event_id in reversed(row['events']):
            item = stock.ensure_balance(conn, row['item_id'])
            inverse = stock.undo(conn, event_id, actor, item['version'], later_batch_events=ignored)
            ignored.update((event_id, inverse['id']))
        if row['created_item']:
            item = stock.ensure_balance(conn, row['item_id'])
            archive_item(conn, {'item_id': row['item_id'], 'version': item['version'], 'note': '撤销上传新增记录'}, actor)
    return {'ok': True, 'reversed_items': len(eligible), 'needs_count': [row['item_id'] for row in rows if row['status'] == '需重新清点']}


def preview_reference(conn, path, filename, category, default_lab=""):
    if not category.strip():
        raise ValueError('请选择旧表所属科目')
    frame = uploaded_frame(path)
    columns = {key: resolve_upload_column(frame.columns, key) for key in ('name','spec','unit','lab','location','quantity')}
    rows = []
    for index, record in frame.iterrows():
        values = {key: clean_cell(record.get(col)) if col else None for key, col in columns.items()}
        if not values['name']:
            continue
        values['lab'] = values['lab'] or default_lab.strip() or None
        locations = cabinet_codes(values['location']) or [None]
        for location in locations:
            rows.append({**values, **frame.attrs['sources'][int(index)], 'category': category, 'location_code': location})
    if not rows:
        raise ValueError('旧表没有可识别的耗材条目')
    token = uuid.uuid4().hex
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    conn.execute("INSERT INTO upload_previews(id,file_hash,content_hash,filename,mode,rows,errors) VALUES (?,?,'',?,'reference',?,'[]')",
                 (token, digest, filename, json.dumps(rows, ensure_ascii=False)))
    conn.commit()
    return {'preview_id': token, 'rows': rows, 'errors': []}


def save_reference(conn, data, actor):
    preview = conn.execute("SELECT * FROM upload_previews WHERE id=? AND mode='reference' AND committed=0", (data['preview_id'],)).fetchone()
    if not preview:
        raise ValueError('旧表预览不存在或已经使用')
    rows = json.loads(preview['rows'])
    category = rows[0]['category']
    if conn.execute('SELECT 1 FROM reference_files WHERE digest=? AND category=? AND active=1', (preview['file_hash'], category)).fetchone():
        raise ValueError('已保存相同参考文件')
    file_id = conn.execute('INSERT INTO reference_files(filename,category,digest,actor) VALUES (?,?,?,?)',
                           (preview['filename'], category, preview['file_hash'], actor)).lastrowid
    conn.executemany('INSERT INTO reference_positions(file_id,sheet,source_row,item_name,spec,unit,lab,location_code,original_location,original_quantity) VALUES (?,?,?,?,?,?,?,?,?,?)',
                     [(file_id, *(row[key] for key in ('source_sheet','source_row','name','spec','unit','lab','location_code','location','quantity'))) for row in rows])
    conn.execute('UPDATE upload_previews SET committed=1 WHERE id=?', (preview['id'],))
    return {'ok': True, 'filename': preview['filename'], 'positions': len(rows)}


def references(conn, category='', query='', lab='', location_code=''):
    rows = [dict(row) for row in conn.execute("""SELECT p.*,f.category,f.filename FROM reference_positions p
        JOIN reference_files f ON p.file_id=f.id WHERE f.active=1 AND (?='' OR f.category=?)
        ORDER BY f.category,f.id,p.source_row,p.id""", (category, category))]
    output = []
    for row in rows:
        if location_code and not matches_cabinet(row['location_code'], location_code):
            continue
        candidates = [dict(item) for item in conn.execute("""SELECT i.*,EXISTS(SELECT 1 FROM stock_counts c JOIN stock_events e ON e.id=c.event_id
            WHERE c.item_id=i.id AND e.action='count' AND e.reversed_by IS NULL) AS counted FROM inventory_items i
            JOIN stock_balances b ON b.item_id=i.id WHERE b.active=1 AND b.review_reason IS NULL
            AND category=? AND normalized_name=? AND unit=?""",
            (row['category'], normalize_text(row['item_name']), row['unit']))]
        matches = [item for item in candidates if row['location_code'] and
                   (item['location_code'] or '').casefold() == row['location_code'].casefold()
                   and (not row['lab'] or row['lab'] in {item['lab'], physical_room(item)})
                   and (not row['spec'] or same_material(row['item_name'], row['spec'], item['item_name'], item['spec']))]
        row['counted'] = bool(len(matches) == 1 and matches[0]['counted'])
        if row['counted']:
            row['lab'] = physical_room(matches[0])
        if lab and row['lab'] != lab:
            continue
        if normalize_text(query) not in normalize_text(' '.join(str(row[key] or '') for key in ('item_name','spec','lab','location_code'))):
            continue
        output.append(row)
    return output
