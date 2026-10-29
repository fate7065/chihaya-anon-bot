"""Offline executors. Live integrations must be added and verified separately."""
from .actions import ActionIntent, ExecutionResult


class MockExecutor:
    def execute(self, intent: ActionIntent) -> ExecutionResult:
        # Do not echo arbitrary payloads or simulate a platform receipt.
        return ExecutionResult('simulated', f'mock only: {intent.capability}; no external action')


class UnavailableExecutor:
    def execute(self, intent: ActionIntent) -> ExecutionResult:
        return ExecutionResult('failed', f'live executor unavailable: {intent.capability}')
