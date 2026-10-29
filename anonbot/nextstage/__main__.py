"""Run all first slices on new synthetic data; no production or account connection."""
import argparse
import asyncio
from dataclasses import asdict
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ..domain import Scope, ValidationError
from ..autonomy.android_control import FixtureControlClient
from ..autonomy.benchmark import load_benchmark
from .common import Journal, canonical, fingerprint
from .memory import MemoryInbox
from .semantic import SemanticRecall
from .reflection import ReflectionNotes
from .state import CompanionState
from .routine import Routine
from .proactive import ProactiveContact
from .vision import visual_proposal
from .publishing import VideoDrafts
from .review import ReviewDesk
from .recovery import backup_database, restore_database, forget_inventory, erase_candidate_rows


SCOPE=Scope('90001','anon','10001')
MODULES=('memory-intake','memory-semantic','memory-reflection','companion-state','character-routine',
         'proactive-contact','screen-vision','bili-publishing','evaluation-review','data-recovery')


def _write(path,value):
    with path.open('x',encoding='utf-8') as stream:stream.write(json.dumps(value,ensure_ascii=False,indent=2)+'\n')


async def run_demo(directory):
    directory=Path(directory)
    if directory.exists():raise ValidationError('演示须使用全新目录')
    directory.mkdir(parents=True,exist_ok=False)
    _write(directory/'workspace.json',dict(schema='nextstage-v1',origin='synthetic',scope=asdict(SCOPE)))
    results={};checks=[];attempts=[];error=None
    def check(name,condition):
        checks.append(dict(check=name,passed=bool(condition)))
        if not condition:raise ValidationError('演示检查未通过：'+name)
    def deny_network(*args,**kwargs):
        attempts.append('blocked');raise RuntimeError('独立演示禁止网络请求')
    try:
        with patch('socket.socket.connect',deny_network),patch('socket.socket.connect_ex',deny_network),patch('socket.getaddrinfo',deny_network):
            journal=Journal(directory/'nextstage.sqlite3');inbox=MemoryInbox(journal)
            old_text='我喜欢乌龙茶';new_text='我现在不喝乌龙茶，改喝柠檬水'
            def extract(text,ttl):return canonical([dict(slot='drink',start=0,end=len(text),quote=text,ttl_seconds=ttl)])
            provider=SimpleNamespace(simulation=True,generate=lambda messages:extract(old_text,3600))
            old=inbox.extract(provider,SCOPE,'source-old',old_text,10)[0]
            check('candidate_not_automatically_approved',inbox.active(SCOPE,11)==[])
            inbox.review(SCOPE,old,'approve',12)
            notes=ReflectionNotes(inbox);notes.propose(SCOPE,'note1',[(old,'乌龙茶')],20)
            check('reflection_has_approved_source',notes.read(SCOPE,'note1',21)['valid'])
            provider.generate=lambda messages:extract(new_text,10)
            new=inbox.extract(provider,SCOPE,'source-new',new_text,30)[0]
            conflict=False
            try:inbox.review(SCOPE,new,'approve',31)
            except ValidationError:conflict=True
            check('conflict_requires_resolution',conflict)
            inbox.review(SCOPE,new,'approve',31,supersedes=old)
            check('explicit_correction_active',inbox.active(SCOPE,32)[0]['candidate_id']==new)
            results['memory-intake']=dict(active=inbox.active(SCOPE,32),expired_at_40=inbox.active(SCOPE,40))
            check('expiry_no_old_resurrection',results['memory-intake']['expired_at_40']==[])
            results['memory-reflection']=notes.read(SCOPE,'note1',32)
            check('stale_reflection_not_reused',not results['memory-reflection']['valid'])
            async def fixture_embed(texts):return [[1.,0.] for _ in texts]
            fixture_embed.simulation=True
            results['memory-semantic']=await SemanticRecall(inbox,fixture_embed).search(SCOPE,'现在爱喝什么',32)
            check('vector_port_returns_reviewed_evidence',results['memory-semantic']['hits'][0]['candidate_id']==new)
            results['memory-semantic']['provider']='synthetic_fixture_not_semantic_quality_test'
            state=CompanionState(journal)
            state.record(SCOPE,'positive','positive',10,'source-emotion')
            state.record(SCOPE,'conversation','conversation',15,'source-chat')
            state.record(SCOPE,'consent','consent-on',20,'source-consent')
            results['companion-state']=state.snapshot(SCOPE,3610)
            check('emotion_half_life',abs(results['companion-state']['valence']-0.2)<1e-9)
            routine=Routine([dict(start_minute=12*60,end_minute=13*60,activity='虚构排练',proactive_allowed=False)],offset_minutes=0)
            results['character-routine']=routine.at(12*3600)
            check('fictional_routine_labeled',results['character-routine']['fictional'])
            contact=ProactiveContact(state);now=16*3600
            preview=contact.evaluate(SCOPE,now,1,1,routine,channel_capable=True)
            claim=contact.claim(SCOPE,'contact1',preview['fingerprint'],now,1,1,routine,channel_capable=True)
            results['proactive-contact']=dict(preview=preview,claim=claim,after_claim=contact.evaluate(SCOPE,now+4000,1,1,routine,channel_capable=True))
            check('contact_candidate_never_sent',claim['sent'] is False)
            check('contact_daily_cap',not results['proactive-contact']['after_claim']['eligible'])
            device=FixtureControlClient(directory/'device.sqlite3');frame=device.preview('demo-emulator','com.example.demo')
            observation=dict(frame=frame.metadata(),target=dict(resource_id='com.example.demo:id/next',label='Next',bounds=[1,2,30,40],confidence=0.95))
            results['screen-vision']=visual_proposal(frame,observation,10,11)
            check('vision_never_clicks',not results['screen-vision']['executed'])
            _write(directory/'vision-proposal.json',results['screen-vision'])
            media=directory/'media';media.mkdir();clip=media/'synthetic-container.mp4'
            clip.write_bytes(b'\x00\x00\x00\x18ftypmp42'+b'\0'*12)
            drafts=VideoDrafts(journal)
            draft=drafts.prepare(SCOPE,'draft1',clip,media,dict(title='合成投稿预览',description='仅容器头测试，不是可播放成片',tags=['MyGO'],category=1,copyright=1,source=''))
            results['bili-publishing']=dict(draft=draft,preflight=drafts.verify(SCOPE,'draft1'))
            check('draft_not_uploaded',draft['uploaded'] is False)
            _write(directory/'video-draft.json',results['bili-publishing'])
            desk=ReviewDesk(journal)
            sample=Path(__file__).resolve().parents[2]/'samples'/'group_benchmark.completion.jsonl'
            dataset=load_benchmark(sample)
            group_cases=[dict(case_id=c.case_id,conversation_id=c.conversation_id,split=c.split,origin=c.origin,
                              content=canonical(asdict(c.snapshot))) for c in dataset.cases]
            desk.prepare(SCOPE,'group-100',group_cases,'group')
            examples=['之前说喜欢的饮料是什么','我现在已经不喝这个了','那件事说的是哪一次','这条资料只允许留一天','不要再记我刚才的事']
            memory_cases=[dict(case_id=f'memory-{i}',conversation_id=f'memory-conversation-{i}',
                split='dev' if i<25 else 'test',origin='synthetic',content=f'合成场景{i}：{examples[i%5]}') for i in range(50)]
            desk.prepare(SCOPE,'memory-50',memory_cases,'memory')
            group_pack=desk.export(SCOPE,'group-100');memory_pack=desk.export(SCOPE,'memory-50')
            _write(directory/'group-review.json',group_pack);_write(directory/'memory-review.json',memory_pack)
            results['evaluation-review']=dict(group_cases=len(group_pack['cases']),memory_cases=len(memory_pack['cases']),human_reviewed=0,
                sample_sha256=dataset.sha256,all_synthetic=True)
            check('review_work_packages_100_and_50',len(group_pack['cases'])==100 and len(memory_pack['cases'])==50)
            results['data-recovery']=dict(backup=backup_database(journal.path,directory/'backup'),
                restore=restore_database(directory/'backup',directory/'restored.sqlite3'))
            restored=Journal(directory/'restored.sqlite3')
            check('restored_scope_state_matches',CompanionState(restored).snapshot(SCOPE,now)==state.snapshot(SCOPE,now))
            plan=forget_inventory(journal,SCOPE);_write(directory/'forget-preview.json',plan)
            receipt=erase_candidate_rows(journal,SCOPE,'forget1',fingerprint(plan))
            results['data-recovery']['candidate_row_erasure']=receipt
            check('erasure_limit_kept_explicit',not receipt['physical_erasure_complete'])
            check('candidate_rows_removed',not forget_inventory(journal,SCOPE)['records'])
            check('all_ten_modules_exercised',set(results)==set(MODULES))
    except Exception as exc:error=dict(type=type(exc).__name__,message=str(exc))
    summary=dict(complete=error is None and not attempts and len(results)==10 and all(c['passed'] for c in checks),
        stage='first_slices',origin='synthetic',started_module_count=len(results),production_complete=False,
        real_platform_actions=0,model_api_calls=0,embedding_api_calls=0,network_attempts=len(attempts),
        checks=checks,results=results,error=error)
    _write(directory/'summary.json',summary)
    rows=['# 未启动功能首轮演示','',f'已运行模块：{len(results)}/10；首轮演示完成：{summary["complete"]}',
          '','这是可运行首个切片，未启用正式聊天、实际模型、账号写操作或设备点击；不能视为全部生产功能完成。','']
    rows.extend('- '+name+'：首个切片已运行' for name in results)
    if error:rows+=['',f'失败：{error["type"]}；详见summary.json。']
    (directory/'报告.md').write_text('\n'.join(rows)+'\n',encoding='utf-8')
    return summary


def main(argv=None):
    parser=argparse.ArgumentParser(description='计划中未启动功能的独立首轮入口')
    sub=parser.add_subparsers(dest='command',required=True)
    demo=sub.add_parser('demo');demo.add_argument('--directory',type=Path,required=True)
    for name in ('review-export','label','forget-preview','forget'):
        item=sub.add_parser(name);item.add_argument('--directory',type=Path,required=True)
        if name in ('review-export','label'):item.add_argument('--batch',required=True)
        if name=='label':
            for field in ('case','hash','label','reviewer'):item.add_argument('--'+field,required=True)
            item.add_argument('--origin',choices=('synthetic','human_declared'),default='human_declared')
        if name=='forget':item.add_argument('--fingerprint',required=True);item.add_argument('--operation',required=True)
    args=parser.parse_args(argv)
    try:
        if args.command=='demo':result=asyncio.run(run_demo(args.directory))
        else:
            marker=args.directory/'workspace.json'
            if not marker.is_file() or marker.stat().st_size>4096:raise ValidationError('不是已有合成工作目录')
            if json.loads(marker.read_text(encoding='utf-8'))!=dict(schema='nextstage-v1',origin='synthetic',scope=asdict(SCOPE)):
                raise ValidationError('工作目录标记不匹配')
            journal=Journal(args.directory/'nextstage.sqlite3')
            if args.command=='review-export':result=ReviewDesk(journal).export(SCOPE,args.batch)
            elif args.command=='label':result=dict(saved=ReviewDesk(journal).label(SCOPE,args.batch,args.case,args.hash,args.label,args.reviewer,args.origin))
            elif args.command=='forget-preview':result=forget_inventory(journal,SCOPE)
            else:result=erase_candidate_rows(journal,SCOPE,args.operation,args.fingerprint)
    except (ValidationError,OSError,ValueError,sqlite3.Error) as exc:
        print(json.dumps(dict(complete=False,error=type(exc).__name__),ensure_ascii=True));return 1
    print(json.dumps(result,ensure_ascii=True));return 0 if result.get('complete',True) else 2


if __name__=='__main__':raise SystemExit(main())
