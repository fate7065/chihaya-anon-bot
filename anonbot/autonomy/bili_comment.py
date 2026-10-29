"""Account/video preflight, one comment POST, then independent full receipt check."""
from .actions import ExecutionResult
from .bili_comment_protocol import BiliCommentError, comment_text, positive_id, validate_comment


def _preflight(transport, account_id, bvid, expected_aid=None):
    account_id = positive_id(account_id)
    login = transport.request('nav', {})
    if login.get('isLogin') is not True or positive_id(login.get('mid')) != account_id:
        raise BiliCommentError('comment_account_mismatch')
    video = transport.request('video', {'bvid': bvid})
    aid = positive_id(video.get('aid'))
    if video.get('bvid') != bvid or (expected_aid is not None and aid != positive_id(expected_aid)):
        raise BiliCommentError('comment_video_mismatch')
    title = video.get('title')
    if not isinstance(title, str) or len(title) > 1000:
        raise BiliCommentError('invalid_video_title')
    return {'aid': int(aid), 'title': title}


def prepare_comment(transport, account_id, bvid, message):
    message = comment_text(message)
    video = _preflight(transport, account_id, bvid)
    payload = validate_comment({'bvid': bvid, 'aid': video['aid'], 'message': message})
    return {'proposal': payload, 'account_id': account_id, 'title': video['title'],
            'simulation': transport.simulation, 'content_trust': 'untrusted_external'}


def verify_receipt(data, rpid, aid, account_id, message):
    root = data.get('root')
    if type(root) is not dict or type(root.get('content')) is not dict:
        raise BiliCommentError('comment_receipt_missing')
    if (positive_id(root.get('rpid')) != rpid or positive_id(root.get('oid')) != str(aid)
            or positive_id(root.get('mid')) != account_id or type(root.get('type')) is not int
            or root['type'] != 1 or type(root.get('root')) is not int or root['root'] != 0
            or type(root.get('parent')) is not int or root['parent'] != 0
            or root['content'].get('message') != message):
        raise BiliCommentError('comment_receipt_mismatch')
    if 'member' in root:
        member = root['member']
        if type(member) is not dict or positive_id(member.get('mid')) != account_id:
            raise BiliCommentError('comment_receipt_author_mismatch')


class BiliCommentExecutor:
    def __init__(self, transport, *, live, account_id):
        if type(live) is not bool or transport.simulation is not (not live):
            raise BiliCommentError('comment_transport_mode_mismatch')
        self.transport, self.live = transport, live
        self.account_id = positive_id(account_id)

    def execute(self, intent):
        data = {'simulation': not self.live, 'write_attempted': False, 'post_accepted': False,
                'receipt_verified': False, 'public_visibility_verified': False,
                'verification_scope': 'authenticated_root_comment_readback'}
        try:
            mode = 'live' if self.live else 'fixture'
            if (intent.actor_id != 'local-cli' or intent.scope_id != f'bilibili-comment:{mode}:{self.account_id}'
                    or intent.account_id != self.account_id or intent.capability != 'bilibili.comment'):
                raise BiliCommentError('comment_scope_mismatch')
            payload = validate_comment(intent.payload)
            if intent.target != payload['bvid']:
                raise BiliCommentError('comment_target_mismatch')
            _preflight(self.transport, self.account_id, payload['bvid'], payload['aid'])
            data.update(bvid=payload['bvid'], aid=payload['aid'], account_id=self.account_id)
            data['write_attempted'] = True
            posted = self.transport.request('comment', {'aid': payload['aid'], 'message': payload['message']})
            rpid = positive_id(posted.get('rpid'))
            data['rpid'] = rpid
            if ('rpid_str' in posted and positive_id(posted['rpid_str']) != rpid
                    or posted.get('need_captcha', False) is not False):
                raise BiliCommentError('comment_post_receipt_not_confirmed')
            data['post_accepted'] = True
            receipt = self.transport.request('receipt', {'aid': payload['aid'], 'rpid': rpid})
            verify_receipt(receipt, rpid, payload['aid'], self.account_id, payload['message'])
            data['receipt_verified'] = True
            return ExecutionResult('succeeded' if self.live else 'simulated', 'comment_readback_verified', data)
        except BiliCommentError as exc:
            data.update(reason=exc.code, platform_code=exc.platform_code)
            return ExecutionResult('unknown' if data['write_attempted'] else 'failed',
                                   'comment_requires_manual_check' if data['write_attempted'] else 'comment_preflight_failed', data)
