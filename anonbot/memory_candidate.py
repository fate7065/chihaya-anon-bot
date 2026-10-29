"""Opt-in synthetic memory trial; never replaces the production Store."""
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import re
import sqlite3

from .domain import EventConflict, Scope, ValidationError, numeric_id


KINDS = ('user_statement', 'character_daily')


def _event_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', value):
        raise ValidationError('候选记忆事件ID无效')


@dataclass(frozen=True)
class MemoryEvidence:
    event_id: str
    text: str
    speaker_id: str
    observed_at: str
    origin: str = 'synthetic'

    def __post_init__(self):
        _event_id(self.event_id)
        if (not isinstance(self.text, str) or not self.text.strip() or len(self.text) > 1000
                or self.text != self.text.strip()):
            raise ValidationError('候选记忆须为1–1000字非空文本，首尾不得有空白')
        if self.origin != 'synthetic':
            raise ValidationError('候选记忆入口只接受合成证据')
        object.__setattr__(self, 'speaker_id', numeric_id(self.speaker_id, 'speaker_id'))
        try:
            if not isinstance(self.observed_at, str) or len(self.observed_at) > 64:
                raise ValueError()
            at = datetime.fromisoformat(self.observed_at.replace('Z', '+00:00'))
            if at.tzinfo is None:
                raise ValueError()
            object.__setattr__(self, 'observed_at', at.astimezone(timezone.utc).isoformat())
        except (ValueError, OverflowError):
            raise ValidationError('证据时间须为带时区的ISO时间') from None


def namespace_for(scope, kind):
    if not isinstance(scope, Scope) or kind not in KINDS:
        raise ValidationError('候选记忆作用域或类别无效')
    return 'anontrial-v1-' + sha256((scope.key+'\n'+kind).encode('utf-8')).hexdigest()


class CandidateMemory:
    """Backend requires async prepare/get/put/search. Scope and kind are host-bound."""
    def __init__(self, backend, scope, kind):
        self.namespace = namespace_for(scope, kind)
        self.backend, self.scope, self.kind = backend, scope, kind
        self.scope_hash = sha256(scope.key.encode('utf-8')).hexdigest()
        self._ready = False

    def _author(self, evidence):
        expected = self.scope.owner_id if self.kind == 'user_statement' else self.scope.bot_id
        if evidence.speaker_id != expected:
            raise ValidationError('证据作者与绑定会话类别不一致')

    def _metadata(self, evidence):
        return dict(schema='anon-evidence-v1', scope_sha256=self.scope_hash, kind=self.kind,
                    event_id=evidence.event_id, speaker_id=evidence.speaker_id,
                    observed_at=evidence.observed_at, origin=evidence.origin)

    async def _prepare(self):
        if not self._ready:
            await self.backend.prepare(self.namespace)
            self._ready = True

    def _checked(self, record):
        if not isinstance(record, dict) or not isinstance(record.get('metadata'), dict):
            raise ValidationError('候选后端返回无效证据')
        meta = record['metadata']
        if (meta.get('schema') != 'anon-evidence-v1' or meta.get('scope_sha256') != self.scope_hash
                or meta.get('kind') != self.kind):
            raise ValidationError('候选后端返回了错误作用域或类别')
        try:
            evidence = MemoryEvidence(meta['event_id'], record['content'], meta['speaker_id'],
                                      meta['observed_at'], meta['origin'])
        except KeyError:
            raise ValidationError('候选后端证据字段缺失') from None
        self._author(evidence)
        if record.get('external_id') != 'event:'+evidence.event_id:
            raise ValidationError('候选后端证据ID不一致')
        return dict(event_id=evidence.event_id, text=evidence.text, speaker_id=evidence.speaker_id,
                    observed_at=evidence.observed_at, origin=evidence.origin, kind=self.kind)

    async def get(self, event_id):
        _event_id(event_id)
        await self._prepare()
        record = await self.backend.get(self.namespace, event_id)
        if record is None:
            return None
        checked = self._checked(record)
        if checked['event_id'] != event_id:
            raise ValidationError('候选后端返回了其他事件')
        return checked

    async def read_existing(self, event_id):
        """Read evidence without allowing namespace creation or memory ingestion."""
        _event_id(event_id)
        prepare = getattr(self.backend, 'prepare_existing', None)
        if not callable(prepare):
            raise RuntimeError('候选后端不支持只读已有Namespace')
        await prepare(self.namespace)
        record = await self.backend.get(self.namespace, event_id)
        if record is None:
            return None
        checked = self._checked(record)
        if checked['event_id'] != event_id:
            raise ValidationError('候选后端返回了其他事件')
        return checked

    async def remember(self, evidence):
        if not isinstance(evidence, MemoryEvidence):
            raise ValidationError('候选写入需要MemoryEvidence')
        self._author(evidence)
        expected = self._checked(dict(content=evidence.text, external_id='event:'+evidence.event_id,
                                      metadata=self._metadata(evidence)))
        old = await self.get(evidence.event_id)
        if old is not None:
            if old != expected:
                raise EventConflict('同一记忆事件携带不同证据，已拒绝覆盖')
            return dict(status='duplicate', evidence=old)
        stored = self._checked(await self.backend.put(self.namespace, evidence, self._metadata(evidence)))
        if stored != expected:
            raise ValidationError('候选后端写入回读与原证据不符')
        return dict(status='stored', evidence=stored)

    async def search(self, query, limit=5):
        return await self._search(query, limit, existing=False)

    async def search_existing(self, query, limit=5):
        """Query an existing namespace; never invoke namespace-creating preparation."""
        return await self._search(query, limit, existing=True)

    async def _search(self, query, limit, existing):
        if not isinstance(query, str) or not query.strip() or len(query) > 1000:
            raise ValidationError('记忆查询须为1–1000字非空文本')
        if type(limit) is not int or not 1 <= limit <= 10:
            raise ValidationError('候选查询limit须为1–10整数')
        if existing:
            prepare = getattr(self.backend, 'prepare_existing', None)
            if not callable(prepare):
                raise RuntimeError('候选后端不支持只读已有Namespace')
            await prepare(self.namespace)
        else:
            await self._prepare()
        result = await self.backend.search(self.namespace, query, limit)
        if (not isinstance(result, dict) or not isinstance(result.get('hits'), list)
                or len(result['hits']) > limit or not isinstance(result.get('diagnostics'), dict)):
            raise ValidationError('候选后端检索结果无效')
        return dict(hits=[self._checked(record) for record in result['hits']], diagnostics=result['diagnostics'])


class AMemorixBackend:
    """Public SDK only, with no model providers or automatic enrichment."""
    def __init__(self, data_dir):
        from importlib.metadata import version
        import a_memorix as sdk
        if version('a-memorix') != '2.0.0a4':
            raise ValidationError('候选后端版本与已核验版本不一致')
        self.sdk = sdk
        self.claim_path = Path(data_dir)/'adapter-claims.sqlite3'
        self.config = sdk.NamespaceConfig(
            features=sdk.NamespaceFeatureConfig(episodes=False, person_profiles=False,
                sparse_retrieval=True, relation_vectors=False, allow_metadata_only_write=True),
            relation_extraction=sdk.RelationExtractionConfig(enabled=False))
        self.engine = sdk.AMemorixEngine(data_dir=data_dir,
            host_port_factory=lambda namespace: sdk.NamespaceHostPorts())

    async def __aenter__(self):
        await self.engine.initialize()
        return self

    async def __aexit__(self, *args):
        await self.engine.shutdown()

    def _context(self, namespace, key=None):
        # The namespace already binds the complete host Scope and memory kind.
        return self.sdk.RequestContext(namespace_id=namespace,
                                       agent_id='anon-offline-trial', idempotency_key=key)

    async def prepare(self, namespace):
        try:
            info = await self.engine.get_namespace(namespace)
        except self.sdk.NamespaceNotFoundError:
            try:
                info = await self.engine.create_namespace(
                    self.sdk.CreateNamespaceRequest(namespace_id=namespace, config=self.config))
            except self.sdk.NamespaceConflictError:
                info = await self.engine.get_namespace(namespace)
        if info.config != self.config or info.status.value != 'active':
            raise ValidationError('候选Namespace已有不同配置或未启用，拒绝复用')

    async def prepare_existing(self, namespace):
        info = await self.engine.get_namespace(namespace)
        if info.config != self.config or info.status.value != 'active':
            raise ValidationError('只读核对的Namespace配置不符或未启用')

    @staticmethod
    def _record(response):
        row = response.memory
        return dict(content=row.content, external_id=row.external_id, metadata=dict(row.metadata))

    async def get(self, namespace, event):
        try:
            response = await self.engine.get_memory(self.sdk.GetMemoryRequest(
                context=self._context(namespace), external_id='event:'+event))
        except self.sdk.NotFoundError:
            return None
        return self._record(response)

    async def put(self, namespace, evidence, metadata):
        self._claim(namespace, evidence, metadata)
        key = sha256((namespace+'\n'+evidence.event_id).encode('utf-8')).hexdigest()
        try:
            await self.engine.ingest_text(self.sdk.IngestTextRequest(
                context=self._context(namespace, key), external_id='event:'+evidence.event_id,
                source_type='anon_trial', text=evidence.text, metadata=metadata,
                observed_at=datetime.fromisoformat(evidence.observed_at),
                relation_extraction=self.sdk.RelationExtractionMode.DISABLED))
        except self.sdk.NamespaceConflictError:
            raise EventConflict('上游幂等键检测到记忆证据冲突') from None
        record = await self.get(namespace, evidence.event_id)
        if record is None:
            raise ValidationError('候选写入未能回读，未确认成功')
        return record

    def _claim(self, namespace, evidence, metadata):
        """Prevent upstream text deduplication from merging distinct event evidence."""
        content_key = sha256(' '.join(evidence.text.split()).encode('utf-8')).hexdigest()
        payload = sha256(json.dumps(dict(text=evidence.text, metadata=metadata),
                                    sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()
        connection = sqlite3.connect(self.claim_path, timeout=10)
        try:
            with connection:
                connection.execute('BEGIN IMMEDIATE')
                connection.execute('''CREATE TABLE IF NOT EXISTS evidence_claims(
                    namespace TEXT NOT NULL,content_key TEXT NOT NULL,event_id TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,PRIMARY KEY(namespace,content_key),UNIQUE(namespace,event_id))''')
                rows = connection.execute('''SELECT content_key,event_id,payload_hash FROM evidence_claims
                    WHERE namespace=? AND (content_key=? OR event_id=?)''',
                    (namespace,content_key,evidence.event_id)).fetchall()
                if rows and any(row != (content_key,evidence.event_id,payload) for row in rows):
                    raise EventConflict('文本或事件已绑定其他证据，拒绝上游合并覆盖')
                connection.execute('INSERT OR IGNORE INTO evidence_claims VALUES (?,?,?,?)',
                                   (namespace,content_key,evidence.event_id,payload))
        finally:
            connection.close()

    async def search(self, namespace, query, limit):
        response = await self.engine.search_memory(self.sdk.SearchMemoryRequest(
            context=self._context(namespace), query=query, limit=limit))
        records = []
        for hit in response.hits:
            if not hit.memory_id:
                raise ValidationError('候选检索结果缺少证据ID')
            fetched = await self.engine.get_memory(self.sdk.GetMemoryRequest(
                context=self._context(namespace), memory_id=hit.memory_id))
            records.append(self._record(fetched))
        diagnostics = dict(retrieval_mode=response.retrieval_mode, degraded=response.degraded,
            retrieval_ready=response.retrieval_ready, available_channels=list(response.available_channels),
            unavailable_channels=list(response.unavailable_channels))
        return dict(hits=records, diagnostics=diagnostics)
