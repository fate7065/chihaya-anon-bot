"""Trusted local task catalogue and bounded workers; no arbitrary plans or paths."""
from hashlib import sha256
import json
from pathlib import Path
import re
import sqlite3
import threading
import time
from uuid import uuid4

from ..domain import ValidationError
from .android_png import validate_png
from .android_read import PNG_LIMIT
from .jsonio import read_json
from .workflow import Workflow
from .workflow_commands import demo_plan
from .workflow_plan import WorkflowPlan
from .workflow_tools import WorkflowTools
from .workflow_lock import RunnerLock


class ConsoleService:
    def __init__(self, directory):
        self.root = Path(directory).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._active = {}
        self._notices = {}
        self._closed = False
        self._exporting = set()
        self._export_slots = threading.BoundedSemaphore(2)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def _path(self, task_id):
        if not isinstance(task_id, str) or not re.fullmatch(r'[0-9a-f]{32}', task_id):
            raise ValidationError('任务编号无效')
        path = self.root / task_id
        if path.resolve().parent != self.root or not (path / 'workflow.sqlite3').is_file():
            raise ValidationError('找不到此任务')
        return path

    def is_active(self, task_id):
        with self._lock:
            return task_id in self._active

    def list_tasks(self):
        tasks = []
        for path in self.root.iterdir():
            if not re.fullmatch(r'[0-9a-f]{32}', path.name):
                continue
            try:
                tasks.append(self.detail(path.name, include_results=False))
            except (ValidationError, OSError, sqlite3.Error, ValueError):
                continue
        return sorted(tasks, key=lambda task: task['created_at'], reverse=True)

    def detail(self, task_id, *, include_results=True):
        path = self._path(task_id)
        metadata = read_json(path / 'display.json')
        state = Workflow(path).status(include_results=include_results)
        with self._lock:
            return {**state, 'id': task_id, 'title': metadata['title'],
                    'created_at': metadata['created_at'], 'active': task_id in self._active,
                    'notice': self._notices.get(task_id, '')}

    def create(self, template, *, query='', live=False):
        if type(live) is not bool or not isinstance(query, str) or template not in ('demo', 'bili-research'):
            raise ValidationError('请选择支持的任务类型')
        if template == 'demo':
            if live or query:
                raise ValidationError('体验任务使用固定离线资料')
            plan = demo_plan()
            title = '离线体验 · 完整读取流程'
        else:
            plan = WorkflowPlan({'version': 1, 'workflow_id': 'console-research', 'steps': [
                {'id': 'find', 'tool': 'bilibili.search', 'args': {'query': query, 'limit': 3}},
                {'id': 'detail', 'tool': 'bilibili.info', 'args': {},
                 'bindings': {'bvid': {'step': 'find', 'path': ['items', 0, 'bvid']}}},
            ]})
            title = 'B站资料 · ' + query
        with self._lock:
            if self._closed or len(list(self.root.iterdir())) >= 100:
                raise ValidationError('任务台已关闭或任务数量达到上限')
            task_id = uuid4().hex
            path = self.root / task_id
            path.mkdir()
            Workflow(path).submit(plan, live=live)
            with (path / 'display.json').open('x', encoding='utf-8') as target:
                json.dump({'title': title, 'created_at': time.time()}, target, ensure_ascii=False)
        return self.detail(task_id)

    def create_and_start(self, template, *, query, live, max_steps):
        with self._lock:
            if self._closed or len(self._active) >= 2:
                raise ValidationError('已有两个任务运行，请稍后再创建')
            if type(max_steps) is not int or max_steps not in (1, 10):
                raise ValidationError('请选择运行步数')
            task = self.create(template, query=query, live=live)
            return self.start(task['id'], max_steps=max_steps)

    def start(self, task_id, *, max_steps=10):
        if type(max_steps) is not int or max_steps not in (1, 10):
            raise ValidationError('请选择运行一步或运行全部')
        path = self._path(task_id)
        with self._lock:
            if self._closed or task_id in self._active or task_id in self._exporting or len(self._active) >= 2:
                raise ValidationError('任务正在运行，或已有两个运行任务')
            workflow = Workflow(path)
            if workflow.status()['status'] in ('completed', 'blocked'):
                return self.detail(task_id)
            cancel = threading.Event()
            thread = threading.Thread(target=self._run, args=(task_id, workflow, cancel, max_steps), daemon=True)
            self._active[task_id] = (thread, cancel)
            self._notices.pop(task_id, None)
            thread.start()
        return self.detail(task_id)

    def _run(self, task_id, workflow, cancel, budget):
        try:
            live = workflow.status()['mode'] == 'live'
            tools = WorkflowTools(workflow.directory, workflow.plan, live=live)
            workflow.run(tools, max_steps=budget, should_stop=cancel.is_set)
        except (ValidationError, OSError, sqlite3.Error):
            with self._lock:
                self._notices[task_id] = '任务未能继续，请检查步骤状态后重试'
        finally:
            with self._lock:
                self._active.pop(task_id, None)

    def stop(self, task_id):
        path = self._path(task_id)
        with self._lock:
            if task_id in self._exporting:
                raise ValidationError('报告正在生成，请稍后操作')
            if task_id in self._active:
                self._active[task_id][1].set()
            Workflow(path).stop()
        return self.detail(task_id)

    def image(self, task_id, index):
        path = self._path(task_id)
        state = Workflow(path).status(include_results=True)
        if type(index) is not int or not 0 <= index < len(state['steps']):
            raise ValidationError('图片编号无效')
        artifact = (state['steps'][index].get('data') or {}).get('artifact')
        if type(artifact) is not dict:
            raise ValidationError('此步骤尚无图片')
        image = Path(artifact['path']).resolve()
        artifact_root = (path / 'artifacts').resolve()
        if artifact_root.parent != path or image.parent != artifact_root or image.suffix != '.png':
            raise ValidationError('图片路径无效')
        with image.open('rb') as source:
            raw = source.read(PNG_LIMIT + 1)
        if len(raw) > PNG_LIMIT or sha256(raw).hexdigest() != artifact['sha256']:
            raise ValidationError('图片已变化，请重新核对任务')
        validate_png(raw)
        return raw

    def export(self, task_id, format):
        from .task_export import build_export
        if format not in ('md', 'zip'):
            raise ValidationError('不支持此导出格式')
        path = self._path(task_id)
        with self._lock:
            if self._closed or task_id in self._active or task_id in self._exporting:
                raise ValidationError('请先停止任务再导出')
            if not self._export_slots.acquire(blocking=False):
                raise ValidationError('已有两个导出正在生成，请稍后重试')
            self._exporting.add(task_id)
        try:
            with RunnerLock(path / 'run.lock'):
                snapshot = self.detail(task_id)
                if snapshot['status'] == 'running':
                    raise ValidationError('请先停止或恢复任务，再导出')
                return build_export(snapshot, lambda index: self.image(task_id, index), format)
        finally:
            with self._lock:
                self._exporting.discard(task_id)
                self._export_slots.release()

    def close(self):
        with self._lock:
            self._closed = True
            active = list(self._active.items())
        for task_id, (thread, cancel) in active:
            cancel.set()
            Workflow(self._path(task_id)).stop()
        for _, (thread, _) in active:
            thread.join(timeout=2)
