"""Manual bounded workflow, separate from scheduling, private chat and model APIs."""
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3

from ..domain import EventConflict, ValidationError
from .actions import ActionIntent, ActionRecord, ActionRuntime, default_registry
from .workflow_lock import RunnerLock
from .workflow_plan import WorkflowPlan


class Workflow:
    def __init__(self, directory):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / 'workflow.sqlite3'
        with self._transaction() as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if tables and tables != {'workflow_state', 'workflow_steps'}:
                raise ValidationError('use a dedicated workflow directory')
            if not tables:
                db.execute('''CREATE TABLE workflow_state(
                    id INTEGER PRIMARY KEY CHECK(id=1), version INTEGER, content TEXT,
                    fingerprint TEXT, live INTEGER, status TEXT, stop_requested INTEGER,
                    next_step INTEGER, detail TEXT)''')
                db.execute('''CREATE TABLE workflow_steps(
                    position INTEGER PRIMARY KEY, intent TEXT, status TEXT, detail TEXT)''')
            state = db.execute('SELECT * FROM workflow_state').fetchone()
            if state is not None and state['version'] != 1:
                raise ValidationError('unsupported workflow schema')

    @contextmanager
    def _transaction(self):
        db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.execute('COMMIT')
        except BaseException:
            if db.in_transaction:
                db.execute('ROLLBACK')
            raise
        finally:
            db.close()

    def _state(self):
        with self._transaction() as db:
            row = db.execute('SELECT * FROM workflow_state').fetchone()
            if row is None:
                raise ValidationError('no workflow submitted in this directory')
            return dict(row)

    @property
    def plan(self):
        return WorkflowPlan(json.loads(self._state()['content']))

    def submit(self, plan, *, live):
        if not isinstance(plan, WorkflowPlan):
            raise ValidationError('workflow requires a validated plan')
        fingerprint = plan.fingerprint(live)
        with self._transaction() as db:
            row = db.execute('SELECT * FROM workflow_state').fetchone()
            if row is not None:
                if row['fingerprint'] != fingerprint:
                    raise EventConflict('directory already contains a different plan or mode')
            else:
                db.execute('INSERT INTO workflow_state VALUES (1,1,?,?,?, ?,0,0,?)',
                           (plan._json, fingerprint, int(live), 'pending', 'submitted'))
                db.executemany('INSERT INTO workflow_steps VALUES (?,NULL,?,?)',
                               [(index, 'pending', '') for index in range(len(plan.document['steps']))])
        return self.status()

    def status(self, *, include_results=False):
        with self._transaction() as db:
            state = db.execute('SELECT * FROM workflow_state').fetchone()
            if state is None:
                raise ValidationError('no workflow submitted in this directory')
            rows = db.execute('SELECT * FROM workflow_steps ORDER BY position').fetchall()
        plan = WorkflowPlan(json.loads(state['content'])).document
        if (len(rows) != len(plan['steps']) or
                [row['position'] for row in rows] != list(range(len(rows))) or
                type(state['next_step']) is not int or not 0 <= state['next_step'] <= len(rows)):
            raise ValidationError('workflow progress records are incomplete')
        report = {'workflow_id': plan['workflow_id'], 'mode': 'live' if state['live'] else 'fixture',
                'status': state['status'], 'next_step': state['next_step'],
                'stop_requested': bool(state['stop_requested']), 'detail': state['detail'],
                'steps': [{'id': step['id'], 'tool': step['tool'], 'status': row['status'],
                           'detail': row['detail'], 'action_id': json.loads(row['intent'])['intent_id'] if row['intent'] else None}
                          for step, row in zip(plan['steps'], rows)]}
        if include_results:
            for step in report['steps']:
                step['data'] = None
            if (self.directory / 'actions.sqlite3').is_file():
                with ActionRuntime(self.directory / 'actions.sqlite3', default_registry(), [], []) as cache:
                    for step in report['steps']:
                        record = cache.get(step['action_id']) if step['action_id'] else None
                        step['data'] = record.data if record else None
        return report

    def stop(self):
        with self._transaction() as db:
            db.execute("""UPDATE workflow_state SET stop_requested=1,
                status=CASE WHEN status='running' THEN status ELSE 'paused' END, detail='stop_requested'
                WHERE status IN ('pending','running','paused')""")
        return self.status()

    def _set_state(self, status, detail):
        with self._transaction() as db:
            db.execute('UPDATE workflow_state SET status=?,detail=?', (status, detail))

    def run(self, dispatcher, *, max_steps=10, should_stop=None):
        if type(max_steps) is not int or not 1 <= max_steps <= 10:
            raise ValidationError('step budget must be 1 to 10')
        if should_stop is not None and not callable(should_stop):
            raise ValidationError('stop callback must be callable')
        with RunnerLock(self.directory / 'run.lock'):
            state = self._state()
            if type(getattr(dispatcher, 'live', None)) is not bool or dispatcher.live != bool(state['live']):
                raise ValidationError('dispatcher mode differs from stored workflow')
            if state['status'] in ('completed', 'blocked'):
                return self.status()
            plan = self.plan
            with self._transaction() as db:
                db.execute("UPDATE workflow_state SET status='running',stop_requested=0,detail='running'")
            try:
                return self._run_steps(plan, bool(state['live']), dispatcher, max_steps, should_stop)
            except KeyboardInterrupt:
                self._set_state('paused', 'interrupted')
                raise
            except Exception:
                self._set_state('blocked', 'worker_or_storage_failure')
                raise ValidationError('workflow worker failed; inspect local records') from None

    def _run_steps(self, plan, live, dispatcher, budget, should_stop):
        results = {}
        definitions = plan.document['steps']
        with self._transaction() as db:
            rows = [dict(row) for row in db.execute('SELECT * FROM workflow_steps ORDER BY position')]
        completed = self._state()['next_step']
        for index, row in enumerate(rows[:completed]):
            intent = ActionIntent(**json.loads(row['intent']))
            record = dispatcher.get(intent)
            if record is None or record.status not in ('succeeded', 'simulated'):
                self._set_state('blocked', 'completed_action_record_missing_or_invalid')
                return self.status()
            results[definitions[index]['id']] = record.data
        for _ in range(budget):
            state = self._state()
            index = state['next_step']
            if index == len(definitions):
                self._set_state('completed', 'all_steps_completed')
                return self.status()
            if state['stop_requested'] or (should_stop is not None and should_stop()):
                self._set_state('paused', 'stop_requested')
                return self.status()
            row = rows[index]
            if row['intent'] is None:
                try:
                    intent = plan.intent(index, results, live=live)
                except ValidationError:
                    self._set_state('blocked', 'binding_or_arguments_unavailable')
                    return self.status()
                with self._transaction() as db:
                    db.execute('UPDATE workflow_steps SET intent=? WHERE position=?', (intent.canonical_json, index))
            else:
                intent = ActionIntent(**json.loads(row['intent']))
            if row['status'] == 'running' and dispatcher.get(intent) is None:
                self._set_state('blocked', 'inflight_action_record_missing')
                return self.status()
            dispatcher.prepare(intent)
            with self._transaction() as db:
                db.execute("UPDATE workflow_steps SET status='running',detail='action_prepared' WHERE position=?", (index,))
            if self._state()['stop_requested'] or (should_stop is not None and should_stop()):
                self._set_state('paused', 'stop_requested')
                return self.status()
            record = dispatcher.execute(intent)
            if not isinstance(record, ActionRecord) or record.intent != intent:
                raise ValidationError('invalid workflow action record')
            data = record.data
            ok = record.status in ('succeeded', 'simulated') and not (data and data.get('status') == 'unavailable')
            with self._transaction() as db:
                db.execute('UPDATE workflow_steps SET status=?,detail=? WHERE position=?',
                           (record.status, record.detail, index))
                if ok:
                    db.execute('UPDATE workflow_state SET next_step=?', (index + 1,))
            if not ok:
                self._set_state('blocked', 'action_failed_uncertain_or_unavailable')
                return self.status()
            results[definitions[index]['id']] = data
        done = self._state()['next_step'] == len(definitions)
        self._set_state('completed' if done else 'paused', 'all_steps_completed' if done else 'step_budget_reached')
        return self.status()
