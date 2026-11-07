"""Interleaved full Runtime comparison, encrypted loopback TLS only."""
from dataclasses import replace
import json
import math
import os
from pathlib import Path
import statistics
import time
from local_app.native.protocol import clock_ns
from local_app.native.selection import release_identity
from local_app.runtime import create_runtime
from support.native_runtime import NativeRuntimeCase


class NativeRuntimeBenchmark(NativeRuntimeCase):
    def test_full_runtime_interleaved(self):
        output=os.environ.get('NJFU_BENCHMARK_EVIDENCE')
        if not output:self.skipTest('Explicit isolated benchmark evidence directory required')
        output=Path(output);output.mkdir(mode=0o700)
        release,sources=release_identity(self.root)
        selected=tuple(map(int,os.environ.get('NJFU_BENCHMARK_COUNTS','1,11,24').split(',')))
        self.assertTrue(selected and all(count in (1,11,24) for count in selected))
        samples=[]
        for block in range(2):
            counts=selected if block==0 else tuple(reversed(selected))
            variants=('python','cpp','cpp','python') if block==0 else ('cpp','python','python','cpp')
            for count in counts:
                for position,variant in enumerate(variants):
                    case=NativeRuntimeCase('runTest');case.mongo=self.mongo
                    try:case.setUp()
                    except Exception:case.doCleanups();raise
                    try:
                        if variant=='python':
                            case.runtime.close()
                            selection=case.directory/'native-selection.json'
                            selection.write_text('{"schema":1,"executor":"python"}')
                            os.chmod(selection,0o600)
                            case.runtime=create_runtime(replace(case.settings,executor='python'),client_factory=case.client_factory)
                            case.addCleanup(case.runtime.close)
                        accounts=[case.account]+[case.runtime.accounts.verify_and_save(s,'fixture-password') for s in case.students[1:count]]
                        due=math.ceil(time.time()+6);runs=[case.plan(a,due=due) for a in accounts]
                        before=clock_ns();wall=time.time();after=clock_ns();due_ns=(before+after)//2+round((due-wall)*1e9)
                        case.runtime.start()
                        rows=[case.wait(r.key,{'success','failed','skipped'},14) for r in runs]
                        self.assertTrue(all(r['status']=='success' for r in rows),[(r['status'],r.get('message')) for r in rows])
                        posts=case.fixture.posts();self.assertEqual(len(posts),count)
                        self.assertTrue(all(v==1 for v in case.fixture.post_counts.values()))
                        self.assertEqual({p['student_id'] for p in posts},set(case.students[:count]))
                        mapping=(after-before)/2e6+.05
                        by_student={p['student_id']:p for p in posts};values=[]
                        for student,row in zip(case.students,rows):
                            self.assertEqual(len(row['attempts']),1)
                            t=row['attempts'][0]['timings'];local=(t['request_started_at']-due)*1000
                            receive=(by_student[student]['received_ns']-due_ns)/1e6
                            self.assertGreaterEqual(local,-.05);self.assertGreaterEqual(receive,-mapping)
                            if variant=='cpp':self.assertIn('transport_warmed_ns',row['native_schedule'])
                            values.append({'student_index':case.students.index(student),'local_call_ms':local,
                                'fixture_receive_ms':receive,'request_ms':t['request_ms'],'timing_version':t['timing_version'],
                                'native_trace':row.get('native_result',{}),
                                'native_schedule':row.get('native_schedule',{})})
                        result={'block':block,'position':position,'variant':variant,'accounts':count,'samples':values,
                                'early':0,'missed':0,'duplicates':0,'matching_receipts':count}
                        for metric in ('local_call_ms','fixture_receive_ms','request_ms'):
                            result[metric+'_tail']=max(v[metric] for v in values)
                        samples.append(result)
                        with (output/'samples.jsonl').open('a') as stream:stream.write(json.dumps(result)+'\n')
                        print(json.dumps({k:v for k,v in result.items() if k!='samples'}),flush=True)
                    finally:case.doCleanups()
        cells=[]
        for count in selected:
            for variant in ('python','cpp'):
                rows=[r for r in samples if r['accounts']==count and r['variant']==variant]
                cell={'accounts':count,'variant':variant,'cohorts':len(rows)}
                for metric in ('local_call_ms_tail','fixture_receive_ms_tail','request_ms_tail'):
                    values=[r[metric] for r in rows];cell[metric]={'p50':statistics.median(values),'max':max(values)}
                cells.append(cell)
        self.assertEqual(release_identity(self.root),(release,sources),'Adapter changed during benchmark')
        (output/'identity.json').write_text(json.dumps({'release_sha256':release,'sources':sources},indent=2)+'\n')
        (output/'summary.json').write_text(json.dumps({'scope':'isolated full Runtime encrypted loopback TLS',
            'cohorts':len(samples),'requests':sum(r['accounts'] for r in samples),'early':0,'missed':0,'duplicates':0,'cells':cells},indent=2)+'\n')
