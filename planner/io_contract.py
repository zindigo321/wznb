"""Version 1 window-table contract; strict UTC seconds and decimal identifiers."""
import csv
import json
import re
from datetime import datetime, timezone
from pathlib import Path

SCORES = {'high': 5, 'medium': 3, 'low': 2}


def parse_utc(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ', value):
        raise ValueError('UTC must be YYYY-MM-DDTHH:MM:SSZ, integer seconds')
    return datetime.strptime(value, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)


def integer(value, field, minimum=0):
    if isinstance(value, bool) or not re.fullmatch(r'\d+', str(value)):
        raise ValueError(f'{field}: expected decimal integer')
    result = int(value)
    if result < minimum:
        raise ValueError(f'{field}: must be >= {minimum}')
    return result


def table(path, required, key):
    with Path(path).open(encoding='utf-8-sig', newline='') as f:
        reader = csv.DictReader(f)
        if reader.fieldnames and len(set(reader.fieldnames)) != len(reader.fieldnames):
            raise ValueError(f'{path.name}: duplicate column names')
        if not required.issubset(reader.fieldnames or []):
            raise ValueError(f'{path.name}: missing columns {sorted(required-set(reader.fieldnames or []))}')
        rows = list(reader)
    seen = set()
    for line, row in enumerate(rows, 2):
        if None in row or any(row.get(k) is None or row[k].strip() == '' for k in required):
            raise ValueError(f'{path.name}:{line}: missing value or extra cells')
        if row[key] in seen:
            raise ValueError(f'{path.name}:{line}: duplicate {key}')
        seen.add(row[key])
    return rows


def load_tables(directory, config):
    """Read all tables before scheduling. Short windows are retained but yield no candidates."""
    directory = Path(directory)
    meta = json.loads((directory/'metadata.json').read_text(encoding='utf-8'))
    if any(type(meta.get(k)) is not int or meta[k] != 1 for k in ('schema_version','time_precision_s')):
        raise ValueError('metadata: require schema_version=1 and time_precision_s=1')
    if not isinstance(meta.get('data_version'), str) or not meta['data_version'].strip():
        raise ValueError('metadata: data_version required')
    if not isinstance(meta.get('filters_applied'), list) or not all(isinstance(x, str) and x.strip() for x in meta['filters_applied']):
        raise ValueError('metadata: filters_applied must be a list of explicit descriptions')
    start, end = parse_utc(meta['horizon_start_utc']), parse_utc(meta['horizon_end_utc'])
    if end <= start:
        raise ValueError('metadata: end must follow start')
    day = parse_utc(config['start_utc']) if config.get('start_utc') else start
    horizon = config['horizon_seconds']
    if day < start or (day-start).total_seconds()+horizon > (end-start).total_seconds():
        raise ValueError('requested horizon outside supplied window data horizon')
    records = []
    for r in table(directory/'targets.csv', {'target_id','priority','full_score'}, 'target_id'):
        n = integer(r['target_id'], 'target_id', 1)
        if r['priority'] not in SCORES or integer(r['full_score'], 'full_score', 1) != SCORES[r['priority']]:
            raise ValueError('target priority/full_score inconsistent with smoke contract')
        records.append(dict(satnum=n, priority=r['priority'], full_score=SCORES[r['priority']]))
    if not records or len({r['satnum'] for r in records}) != len(records):
        raise ValueError('targets empty or duplicate normalized target_id')
    stations = [dict(station_id=integer(r['station_id'], 'station_id', 1))
                for r in table(directory/'stations.csv', {'station_id'}, 'station_id')]
    sids = {r['station_id'] for r in stations}
    if len(sids) != len(stations) or not set(config['station_ids']).issubset(sids):
        raise ValueError('duplicate or unknown configured station')
    ids = {r['satnum'] for r in records}
    windows = []
    for r in table(directory/'windows.csv', {'window_id','target_id','station_id','start_utc','end_utc'}, 'window_id'):
        n, sid = integer(r['target_id'], 'target_id', 1), integer(r['station_id'], 'station_id', 1)
        a, b = parse_utc(r['start_utc']), parse_utc(r['end_utc'])
        if n not in ids or sid not in sids:
            raise ValueError('window references unknown target/station')
        if b <= a or a < start or b > end:
            raise ValueError('invalid window ordering/horizon')
        if sid not in config['station_ids']:
            continue
        x, y = max(0, int((a-day).total_seconds())), min(horizon, int((b-day).total_seconds()))
        if y > x:
            from .common import utc_at
            windows.append(dict(window_id=r['window_id'], satnum=n, station_id=sid, start_s=x, end_s=y,
                                start_utc=utc_at(day,x), end_utc=utc_at(day,y), duration_s=y-x))
    return records, [s for s in stations if s['station_id'] in config['station_ids']], windows, day, meta


def export_tables(directory, records, stations, windows, day, horizon, metadata):
    from .common import csv_write, json_write, utc_at
    directory = Path(directory)
    csv_write(directory/'targets.csv', [dict(target_id=r['satnum'], **{k:r[k] for k in ('priority','full_score')}) for r in records],
              ['target_id','priority','full_score'])
    csv_write(directory/'stations.csv', stations, ['station_id'])
    csv_write(directory/'windows.csv', [dict(w, target_id=w['satnum']) for w in windows],
              ['window_id','target_id','station_id','start_utc','end_utc'])
    json_write(directory/'metadata.json', dict(metadata, schema_version=1, time_precision_s=1,
               horizon_start_utc=utc_at(day,0), horizon_end_utc=utc_at(day,horizon)))


def read_standard_tasks(path, records, windows, day):
    """Normalize required task columns; never use a submitted score for evaluation."""
    from .common import utc_at
    targets = {r['satnum']:r for r in records}
    result = []
    for r in table(Path(path), {'task_id','target_id','station_id','start_utc','end_utc','duration_s','status'}, 'task_id'):
        n, sid = integer(r['target_id'], 'target_id',1), integer(r['station_id'], 'station_id',1)
        a, b = int((parse_utc(r['start_utc'])-day).total_seconds()), int((parse_utc(r['end_utc'])-day).total_seconds())
        if n not in targets:
            raise ValueError('task references unknown target')
        target = targets[n]
        matches = [w for w in windows if w['satnum']==n and w['station_id']==sid and w['start_s']<=a and b<=w['end_s']]
        points = target['full_score']*(3 if b-a==120 else 5) if b-a in (120,240) else 0
        if r['status']=='interrupted':
            points=0
        task = dict(task_id=r['task_id'], candidate_id=r.get('candidate_id',r['task_id']),
                    window_id=r.get('window_id') or (matches[0]['window_id'] if matches else '<none>'),
                    satnum=n, station_id=sid, priority=target['priority'], start_s=a, end_s=b,
                    duration_s=integer(r['duration_s'],'duration_s'), start_utc=utc_at(day,a), end_utc=utc_at(day,b),
                    points_int=points, score=points/5, status=r['status'])
        for key in ('planned_end_s','planned_duration_s'):
            if r.get(key): task[key]=integer(r[key],key)
        if r.get('planned_end_utc'): task['planned_end_utc']=r['planned_end_utc']
        result.append(task)
    return result


def read_complete_tasks(path):
    """Strict full-task schema for no-fault v1; preserve raw rows and claimed scores."""
    from decimal import Decimal, InvalidOperation
    from .common import TASK_FIELDS
    path = Path(path)
    with path.open(encoding='utf-8-sig', newline='') as f:
        reader = csv.DictReader(f)
        fields = reader.fieldnames
        if (not fields or len(fields) != len(set(fields)) or
                set(fields) not in (set(TASK_FIELDS), set(TASK_FIELDS)|{'target_id'})):
            raise ValueError('complete task CSV requires unique, exact full-task columns')
        raw = list(reader)
    tasks = []
    for source in raw:
        if None in source or any(v is None or v == '' for v in source.values()):
            raise ValueError('complete task CSV: missing value or extra cells')
        row = {k: source[k] for k in TASK_FIELDS}
        for key in ('satnum','station_id','start_s','end_s','duration_s','points_int'):
            row[key] = integer(row[key], key, 1 if key in ('satnum','station_id') else 0)
        if 'target_id' in source and integer(source['target_id'],'target_id',1) != row['satnum']:
            raise ValueError('target_id/satnum mismatch')
        parse_utc(row['start_utc']); parse_utc(row['end_utc'])
        try:
            score = Decimal(row['score'])
        except InvalidOperation as error:
            raise ValueError('invalid decimal score') from error
        if not score.is_finite() or score != Decimal(row['points_int'])/5:
            raise ValueError('claimed score/points mismatch')
        row['score'] = float(score)
        tasks.append(row)
    return fields, raw, tasks
