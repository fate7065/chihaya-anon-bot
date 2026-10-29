"""Trusted local dispatcher: six read tools, one exact grant per frozen action."""
from ..domain import ValidationError
from .actions import ActionRuntime, Grant, default_registry
from .android_executor import AndroidReadExecutor
from .android_read import AdbClient
from .bili_executor import BiliReadExecutor


class WorkflowTools:
    def __init__(self, directory, plan, *, live=False, adb=None):
        if type(live) is not bool:
            raise ValidationError('invalid workflow tool mode')
        self.path = directory / 'actions.sqlite3'
        has_android = any(step['tool'].startswith('android.') for step in plan.document['steps'])
        if live and has_android and adb is None:
            raise ValidationError('live Android steps require an explicit local ADB path')
        client = AdbClient(adb) if live and has_android else None
        self.live = live
        self.android = AndroidReadExecutor(client, live=live if has_android else False,
                                           artifact_dir=directory / 'artifacts')

    def _runtime(self, intent):
        return ActionRuntime(self.path, default_registry(), [Grant.for_intent(intent)], [])

    def prepare(self, intent):
        with self._runtime(intent) as runtime:
            return runtime.submit(intent)

    def get(self, intent):
        if not self.path.exists():
            return None
        with self._runtime(intent) as runtime:
            return runtime.get(intent.intent_id)

    def execute(self, intent):
        with self._runtime(intent) as runtime:
            executor = BiliReadExecutor(live=self.live) if intent.capability.startswith('bilibili.') else self.android
            return runtime.execute(intent.intent_id, executor)
