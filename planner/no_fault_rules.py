"""Independent no-fault rules, without planner policy or scheduler imports."""
from collections import Counter, defaultdict
from decimal import Decimal
from .io_contract import parse_utc
WEIGHTS = {'high':5, 'medium':3, 'low':2}
MEMBER_FIELDS = ('candidate_id','window_id','satnum','station_id','priority','start_s','end_s',
                 'duration_s','points_int','score','start_utc','end_utc')

def require(value, message):
    if not value:
        raise ValueError(message)

def decision(value, horizon):
    require(type(value) is int and 0 <= value <= horizon, 'decision-s must be integer in [0,horizon]')
    return value

def status_at(row, t):
    return 'completed' if row['end_s'] <= t else 'locked' if row['start_s'] < t < row['end_s'] else 'new_success'

def points(context, row):
    n = row['satnum']
    require(n in context['priorities'] and row['station_id'] in context['stations'], 'unknown target/station')
    require(all(type(row[k]) is int for k in ('satnum','station_id','start_s','end_s','duration_s','points_int')), 'integer task fields required')
    a = (parse_utc(row['start_utc'])-context['day']).total_seconds()
    b = (parse_utc(row['end_utc'])-context['day']).total_seconds()
    require(a == row['start_s'] and b == row['end_s'] and b-a == row['duration_s'], 'UTC/seconds/duration mismatch')
    require(0 <= a < b <= context['horizon'] and b-a in (120,240), 'duration/horizon invalid')
    p = context['priorities'][n]
    require(row['priority'] == p, 'authoritative priority mismatch')
    value = WEIGHTS[p] * (3 if b-a == 120 else 5)
    require(row['points_int'] == value and Decimal(str(row['score'])) == Decimal(value)/5, 'authoritative points/score mismatch')
    w = context['windows'].get(row['window_id'])
    require(w is not None and w['satnum']==n and w['station_id']==row['station_id'] and
            w['start_s'] <= a < b <= w['end_s'], 'single-window membership invalid')
    require(isinstance(row['task_id'],str) and row['task_id'] and row['candidate_id'], 'task/candidate ID required')
    return value

def member(row, pool):
    other = pool.get(row['candidate_id'])
    return other is not None and all(row[k] == other[k] for k in MEMBER_FIELDS)

def plan(context, rows):
    total = sum(points(context,r) for r in rows)
    require(len({r['satnum'] for r in rows}) == len(rows), 'duplicate target')
    require(len({r['task_id'] for r in rows}) == len(rows), 'duplicate task ID')
    occupied = defaultdict(list)
    for row in rows:
        occupied[row['station_id']].append(row)
    for tasks in occupied.values():
        ordered = sorted(tasks,key=lambda r:(r['start_s'],r['end_s']))
        for a,b in zip(ordered,ordered[1:]):
            require(b['start_s'] >= a['end_s']+30, 'occupancy/30-second switch conflict')
    return (total,len(rows))

def validate_source(context, initial, candidates):
    pool = {}
    for row in candidates:
        points(context,row)
        require(row['status']=='success' and row['candidate_id'] not in pool, 'candidate status/duplicate ID')
        require(row['task_id']==row['candidate_id'], 'candidate task ID must equal candidate ID')
        pool[row['candidate_id']] = row
    plan(context,initial)
    for row in initial:
        require(row['status']=='success' and member(row,pool), 'original success/candidate membership')
    return pool

def signature(row):
    # Task IDs and every full-task field are part of frozen history.
    return tuple(sorted(row.items()))

def native_report(context, original, candidates, t, tasks, require_guard=True):
    """Rebuild history and original-pool membership; never accepts a prepared ticket."""
    decision(t,context['horizon'])
    pool = validate_source(context,original,candidates)
    retained = [dict(r,status=status_at(r,t)) for r in original]
    fixed = [r for r in retained if r['status'] in ('completed','locked')]
    require(Counter(map(signature,fixed)) ==
            Counter(signature(r) for r in tasks if r['status'] in ('completed','locked')),
            'complete fixed history differs from original+t')
    fixed_ids = {r['satnum'] for r in fixed}
    for row in tasks:
        if row['status'] in ('completed','locked'):
            continue
        require(row['status']=='new_success' and row['start_s']>=t and row['satnum'] not in fixed_ids and
                member(row,pool), 'future task not in original candidate pool/time')
    current, reference = plan(context,tasks), plan(context,retained)
    if require_guard:
        require(current >= reference, 'selected J degraded')
        if current == reference:
            require(tasks == retained, 'equal J must retain exact original rows/order')
    return dict(status='PASS',points_int=current[0],task_score=current[0]/5,coverage=current[1],J=list(current),
                J_retained=list(reference),by_priority_coverage=dict(Counter(context['priorities'][r['satnum']] for r in tasks)),
                success_count=len(tasks),interrupted_count=0,violation_count=0,violations=[],
                history_counts=dict(Counter(r['status'] for r in tasks)),evaluation_scope='native_no_fault_supplied_windows',
                solver_calls=0,history_rebuilt_from_original=True,member_source='complete original candidate pool',
                monotonicity='On normal completion: J, points and score never decrease; coverage may decrease only when points increase.')
