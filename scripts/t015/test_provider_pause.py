import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import run_five

class PauseTest(unittest.TestCase):
    def test_phase_preserves_goal(self):
        s = run_five.Scenario.__new__(run_five.Scenario)
        s.name, s.team, s.sample, s.fixture = 'newsroom', {'team_id':'team'}, Path('.'), None
        s.fresh_software = s.sequential = False
        s.stage_seconds, s.grace_seconds, s.idle_seconds = 1, 0, 45
        s.assignment_ids, s.model = [], SimpleNamespace(paused_reason='HTTP 402')
        s.send = lambda *a, **kw: {'message_id':'source'}
        status = {'turns':[], 'active':[], 'goal':{'id':'goal','status':'active'}, 'terminal':None,
                  'latest_event':0, 'counts':dict.fromkeys(('artifact_versions','task_submissions','task_reviews'),0)}
        s.collect_status = lambda *a: status
        events, dumps = [], []
        s.log = lambda event, **kw: events.append((event,kw))
        s.dump = lambda client, name: dumps.append(name)
        s.post = lambda *a, **kw: self.fail('paused goal must not be terminated')
        client = SimpleNamespace(portal=SimpleNamespace(call=lambda fn,*args: fn(*args)))
        with tempfile.TemporaryDirectory() as tmp, patch.object(run_five,'prompt_text',return_value='task'):
            s.directory = Path(tmp)
            self.assertEqual(s.phase(client,1), 'provider_paused')
        self.assertEqual(dumps,['task1'])
        self.assertEqual(tuple(events[-1][1][x] for x in ('team_id','source_message_id','goal_id')), ('team','source','goal'))

    def test_run_stops_before_next_phase(self):
        s = run_five.Scenario.__new__(run_five.Scenario)
        s.fixture, s.sample, s.token, s.model = None, Path('.'), 'token', SimpleNamespace(identity={})
        s.name, s.app = 'newsroom', object()
        s.dump = lambda *a: None
        s.log = lambda *a,**kw: None
        s.post = lambda *a,**kw: SimpleNamespace(is_success=True,json=lambda: {'team_id':'team','lead_member_id':'lead','members':[{'member_id':'lead','role_id':'lead'}]})
        phases=[]
        s.phase=lambda client,phase: phases.append(phase) or 'provider_paused'
        class Client:
            def __init__(self,*a,**kw): pass
            def __enter__(self): return self
            def __exit__(self,*a): pass
        with tempfile.TemporaryDirectory() as tmp, patch.object(run_five,'TestClient',Client), patch.object(Path,'read_text',return_value='roster'):
            s.directory=Path(tmp)
            self.assertEqual(s.run(),'provider_paused')
        self.assertEqual(phases,[1])

    def test_bootstrap_pause_is_reported_as_blocked(self):
        scenario = run_five.Scenario.__new__(run_five.Scenario)
        scenario.fixture = None
        scenario.sample = Path('.')
        scenario.app = object()
        scenario.model = SimpleNamespace(paused_reason='HTTP 402')
        scenario.post = lambda *a, **kw: SimpleNamespace(is_success=False, status_code=503)
        events = []
        scenario.log = lambda event, **kw: events.append(event)

        class Client:
            def __init__(self, *a, **kw): pass
            def __enter__(self): return self
            def __exit__(self, *a): pass

        with patch.object(run_five, 'TestClient', Client), patch.object(Path, 'read_text', return_value='roster'):
            self.assertEqual(scenario.run(), 'provider_paused')
        self.assertEqual(events, ['provider_paused_during_bootstrap'])

if __name__ == '__main__': unittest.main()
