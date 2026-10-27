"""Independent source-based validation. No scheduler imports."""
from .common import *

class IndependentValidator:
    def __init__(self, config, day, sample, windows, clock):
        self.clock = clock
        self.day = day
        self.config = config
        self.ts = load.timescale(builtin=True)
        raw = ROOT / config['input_dir']
        lines = (raw / '轨道.txt').read_bytes().decode('utf-8', errors='strict').splitlines()
        selected = {n for group in sample['selected'].values() for n in group}
        self.targets = {}
        self.priority = {}
        self.value = {}
        for index in range(0,len(lines),3):
            if index % 300 == 0:
                clock.check()
            model = Satrec.twoline2rv(lines[index+1], lines[index+2])
            if model.satnum in selected:
                self.targets[model.satnum] = EarthSatellite(lines[index+1], lines[index+2], lines[index][2:], self.ts)
        for p, filename, value in PRIORITIES:
            for token in (raw / filename).read_bytes().decode('utf-8',errors='strict').strip().split(','):
                n = int(token)
                if n in selected:
                    self.priority[n]=p; self.value[n]=value
        self.sites = {}
        for line in (raw / '站址.txt').read_bytes().decode('utf-8',errors='strict').splitlines():
            pieces = re.split('[:：]',line,maxsplit=1)
            if len(pieces)==2 and pieces[0].strip().isdigit():
                sid=int(pieces[0]); lon, lat, height = map(float,pieces[1].split(','))
                if sid in config['station_ids']:
                    self.sites[sid] = wgs84.latlon(lat, (lon+180)%360-180, elevation_m=height)
        self.windows = {w['window_id']:w for w in windows}

    @classmethod
    def from_tables(cls, config, day, records, stations, windows, clock):
        validator = cls.__new__(cls)
        validator.config, validator.day, validator.clock = config, day, clock
        validator.targets = {r['satnum']: None for r in records}
        validator.priority = {r['satnum']: r['priority'] for r in records}
        validator.value = {r['satnum']: {'high':5,'medium':3,'low':2}[r['priority']] for r in records}
        validator.sites = {r['station_id']: None for r in stations}
        validator.windows = {w['window_id']:w for w in windows}
        validator.ts = None
        return validator

    def check(self, tasks, interrupted=(), fault=None, expected_history=(), label='plan', original_plan=None):
        if fault is not None:
            if not isinstance(fault,dict) or any(type(fault.get(k)) is not int for k in ('station_id','start_s','end_s')):
                raise ValueError('fault requires integer station_id, start_s and end_s')
            if fault['station_id'] not in self.sites or not 0 <= fault['start_s'] < fault['end_s'] <= self.config['horizon_seconds']:
                raise ValueError('invalid fault station or interval')
            for key,second in (('start_utc',fault['start_s']),('end_utc',fault['end_s'])):
                if key in fault and fault[key] != utc_at(self.day,second):
                    raise ValueError('fault UTC/relative seconds mismatch')
        violations=[]
        details=[]
        valid=[]
        counts={c:0 for c in CHECK_CATEGORIES}
        checked={c:0 for c in CHECK_CATEGORIES}

        def fail(category, task, reason, **evidence):
            counts[category]+=1
            violations.append(dict(category=category,task_id=task.get('task_id','<missing>'),reason=reason,**evidence))

        rows=[(r,False) for r in tasks]+[(r,True) for r in interrupted]
        required=set(TASK_FIELDS)
        for row, fragment in rows:
            self.clock.check()
            checked['invalid_data']+=1
            missing=sorted(required-set(row))
            if missing:
                fail('invalid_data',row,'missing fields',fields=missing); continue
            numeric=('satnum','station_id','start_s','end_s','duration_s','points_int')
            if any(not isinstance(row[k],int) or isinstance(row[k],bool) for k in numeric):
                fail('invalid_data',row,'non-integer or nonfinite integer fields'); continue
            n,sid,a,b=row['satnum'],row['station_id'],row['start_s'],row['end_s']
            if n not in self.targets or sid not in self.sites or not isinstance(row['score'],(int,float)) or not math.isfinite(row['score']):
                fail('invalid_data',row,'unknown target/station or nonfinite score',satnum=n,station_id=sid); continue
            allowed_status={'success','completed','locked','new_success'} if not fragment else {'interrupted'}
            if row['status'] not in allowed_status or row['priority']!=self.priority[n] or row['duration_s']!=b-a or b<=a:
                fail('invalid_data',row,'invalid status, priority, duration or ordering',status=row['status'],start=a,end=b)
            if fragment:
                expected=0
                if fault is None or sid!=fault['station_id'] or b!=fault['start_s'] or not a<=fault['start_s']:
                    fail('invalid_data',row,'fragment inconsistent with fault boundary')
                if row.get('planned_duration_s') not in (120,240) or row.get('planned_end_s',b)<=b:
                    fail('invalid_data',row,'fragment missing original complete task metadata')
                elif row['planned_end_s']-a!=row['planned_duration_s'] or row.get('planned_end_utc')!=utc_at(self.day,row['planned_end_s']):
                    fail('invalid_data',row,'fragment original duration/UTC inconsistent')
            else:
                if b-a not in (120,240):
                    fail('invalid_data',row,'success duration must be120/240',actual=b-a)
                expected=self.value[n]*(3 if b-a==120 else 5)
            if row['points_int']!=expected or abs(row['score']-expected/5)>1e-12:
                fail('invalid_data',row,'score inconsistent with true priority/duration',expected_points=expected,actual_points=row['points_int'])
            if row['start_utc']!=utc_at(self.day,a) or row['end_utc']!=utc_at(self.day,b):
                fail('invalid_data',row,'UTC seconds mismatch')
            checked['window_or_horizon']+=1
            if a<0 or b>self.config['horizon_seconds'] or b<=a:
                fail('window_or_horizon',row,'outside configured horizon',start=a,end=b)
            w=self.windows.get(row['window_id'])
            if w is None or w['satnum']!=n or w['station_id']!=sid or a<w['start_s'] or b>w['end_s']:
                fail('window_or_horizon',row,'not contained in matching single legal window',window_id=row['window_id'],start=a,end=b,
                     window_start=None if w is None else w['start_s'],window_end=None if w is None else w['end_s'])
            checked['fault_occupancy']+=1
            if fault and sid==fault['station_id'] and a<fault['end_s'] and b>fault['start_s']:
                fail('fault_occupancy',row,'actual occupancy intersects failed station interval',fault_start=fault['start_s'],fault_end=fault['end_s'],start=a,end=b)
            checked['elevation']+=int(self.ts is not None)
            # guard malicious enormous intervals, but still make illegal-data evidence
            if b-a>self.config['horizon_seconds'] or b-a<=0:
                fail('invalid_data',row,'unbounded/invalid interval for propagation'); continue
            secs=np.arange(a,b+1,dtype=int)
            evidence=[]
            lo,hi=math.inf,-math.inf
            if self.ts is not None:
                for offset in range(0,len(secs),2000):
                    self.clock.check()
                    ss=secs[offset:offset+2000]
                    t=self.ts.utc(self.day.year,self.day.month,self.day.day,self.day.hour,self.day.minute,self.day.second+ss)
                    sat=self.targets[n].at(t)
                    alt=np.asarray((sat-self.sites[sid].at(t)).altaz()[0].degrees)
                    msg=np.asarray(sat.message,dtype=object)
                    if msg.ndim==0:
                        msg=np.full(alt.shape,msg.item(),dtype=object)
                    bad_propagation=np.array([x is not None for x in msg]) | ~np.isfinite(alt)
                    for ix in np.flatnonzero(bad_propagation):
                        fail('invalid_data',row,'SGP4 error/nonfinite real propagation',second=int(ss[ix]),message=str(msg[ix]))
                    bad=np.flatnonzero(bad_propagation | (alt<10) | (alt>85))
                    evidence.extend(dict(second=int(ss[ix]),utc=utc_at(self.day,ss[ix]),
                                         elevation=None if not np.isfinite(alt[ix]) else float(alt[ix]),message=str(msg[ix])) for ix in bad)
                    finite=alt[np.isfinite(alt)]
                    if len(finite):
                        lo=min(lo,float(finite.min())); hi=max(hi,float(finite.max()))
            if evidence:
                fail('elevation',row,'real per-second elevation outside10..85/error',invalid_seconds=evidence)
            details.append(dict(task_id=row['task_id'],kind='interrupted_actual_occupancy' if fragment else 'success',
                                satnum=n,station_id=sid,start_s=a,end_s=b,
                                checked_second_count=len(secs) if self.ts is not None else 0,endpoints_included=True,propagated_from_raw_TLE=self.ts is not None,
                                min_elevation=None if lo==math.inf else lo,max_elevation=None if hi==-math.inf else hi,
                                invalid_second_count=len(evidence)))
            valid.append((row,fragment))
        success=[r for r,fragment in valid if not fragment]
        seen={}
        for r in success:
            checked['duplicate_success']+=1
            if r['satnum'] in seen:
                fail('duplicate_success',r,'same target successful more than once across all stations',other_task=seen[r['satnum']])
            seen[r['satnum']]=r['task_id']
        for sid in self.sites:
            occ=sorted([r for r,_ in valid if r['station_id']==sid],key=lambda r:(r['start_s'],r['end_s']))
            for index,r in enumerate(occ):
                for q in occ[index+1:]:
                    if q['start_s']>=r['end_s']+30:
                        break
                    checked['overlap']+=1; checked['switch_gap']+=1
                    if q['start_s']<r['end_s'] and r['start_s']<q['end_s']:
                        fail('overlap',q,'whole occupied intervals overlap',other_task=r['task_id'],station_id=sid,
                             other_end=r['end_s'],start=q['start_s'])
                    elif q['satnum']!=r['satnum'] and q['start_s']-r['end_s']<30:
                        fail('switch_gap',q,'different targets require >=30 seconds',other_task=r['task_id'],gap_s=q['start_s']-r['end_s'])
            # Normal-plan resource checks include pair comparisons beyond conflicts too.
            checked['overlap']+=max(0,len(occ)-1); checked['switch_gap']+=max(0,len(occ)-1)
        # Reconstruct history independently from the caller's immutable initial plan.
        # expected_history is retained for source compatibility, deliberately never trusted.
        expected_success, expected_fragments = [], []
        if fault:
            if original_plan is None:
                fail('invalid_data', {}, 'fault evaluation requires immutable original_plan')
            else:
                event_start = fault['start_s']
                for original in original_plan:
                    r = dict(original)
                    if r['end_s'] <= event_start:
                        r['status'] = 'completed'
                        expected_success.append(r)
                    elif r['start_s'] < event_start < r['end_s']:
                        if r['station_id'] != fault['station_id']:
                            r['status'] = 'locked'
                            expected_success.append(r)
                        else:
                            r.update(status='interrupted', planned_end_s=r['end_s'],
                                     planned_duration_s=r['duration_s'], planned_end_utc=r['end_utc'],
                                     end_s=event_start, duration_s=event_start-r['start_s'],
                                     end_utc=utc_at(self.day,event_start), points_int=0, score=0.0)
                            expected_fragments.append(r)
                history_fields = TASK_FIELDS + ['planned_end_s','planned_duration_s','planned_end_utc']
                def signature(r):
                    return json.dumps({k:r.get(k) for k in history_fields}, sort_keys=True, allow_nan=True)
                wanted = collections.Counter(signature(r) for r in expected_success)
                actual_history = [r for r in tasks if r.get('status') in ('completed','locked') or
                                  (isinstance(r.get('start_s'),int) and r['start_s'] < event_start)]
                actual = collections.Counter(signature(r) for r in actual_history)
                if actual != wanted:
                    fail('invalid_data', {}, 'history set differs from independently derived initial plan',
                         missing=sum((wanted-actual).values()), extra=sum((actual-wanted).values()))
                wanted_fragments = collections.Counter(signature(r) for r in expected_fragments)
                actual_fragments = collections.Counter(signature(r) for r in interrupted)
                if wanted_fragments != actual_fragments:
                    fail('invalid_data', {}, 'interrupted history set differs from initial plan',
                         missing=sum((wanted_fragments-actual_fragments).values()),
                         extra=sum((actual_fragments-wanted_fragments).values()))
                retained = {signature(r) for r in expected_success}
                for r in tasks:
                    if signature(r) not in retained and (r.get('status') != 'new_success' or
                            not isinstance(r.get('start_s'),int) or r['start_s'] < event_start):
                        fail('invalid_data',r,'new success must start at/after fault and use new_success')
        elif interrupted or any(r.get('status') != 'success' for r in tasks):
            fail('invalid_data', {}, 'initial plans cannot claim fault history')
        task_ids = [r.get('task_id') for r in tasks] + [r.get('task_id') for r in interrupted]
        if len(set(task_ids)) != len(task_ids):
            fail('invalid_data', {}, 'duplicate task_id')
        # Independent integer scoring from authoritative priority and actual elapsed time.
        points = sum(self.value[r['satnum']] * (3 if r['end_s']-r['start_s']==120 else 5)
                     for r in success if r['end_s']-r['start_s'] in (120,240))
        coverage = len({r['satnum'] for r in success})
        by_priority = dict(collections.Counter(self.priority[n] for n in {r['satnum'] for r in success}))
        report=dict(label=label,status='FAIL' if violations else 'PASS',
                    points_int=points,task_score=points/5,coverage=coverage,by_priority_coverage=by_priority,
                    evaluation_scope='raw_TLE_per_second' if self.ts is not None else 'supplied_windows_only',
                    nonempty=bool(tasks),success_count=len(success),interrupted_count=len(interrupted),
                    violation_count=len(violations),violations_by_category=counts,checks_by_category=checked,
                    violations=violations,task_geometry_details=details,
                    independent_module='IndependentValidator; authoritative input priority/time; no planner checks called',
                    occupancy_convention='[start,end); geometry includes both endpoints and every internal integer second')
        return report

    @staticmethod
    def check_no_fault(context, original_plan, candidates, decision_s, tasks, *, require_guard=True):
        """Native history evaluation from authority + original plan, never prepared policy state."""
        from .no_fault_rules import native_report
        return native_report(context, original_plan, candidates, decision_s, tasks, require_guard)
