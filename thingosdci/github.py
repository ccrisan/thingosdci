
import hashlib
import hmac
import json
import logging

from tornado import gen
from tornado import web
from tornado import httpclient

from thingosdci import cache
from thingosdci import dockerctl
from thingosdci import settings


logger = logging.getLogger(__name__)


_STATUS_CONTEXT = 'ci/thingos-builder'


class EventHandler(web.RequestHandler):
    def post(self):
        # verify signature
        remote_signature = self.request.headers.get('X-Hub-Signature')
        if not remote_signature:
            logger.warning('missing signature header')
            raise web.HTTPError(401)

        remote_signature = remote_signature[5:]  # skip "sha1="

        local_signature = hmac.new(settings.WEB_SECRET.encode('utf8'),
                                   msg=self.request.body, digestmod=hashlib.sha1)
        local_signature = local_signature.hexdigest()

        if not hmac.compare_digest(local_signature, remote_signature):
            logger.warning('mismatching signature')
            raise web.HTTPError(401)

        data = json.loads(self.request.body)

        github_event = self.request.headers['X-GitHub-Event']
        action = data['action']

        if github_event == 'pull_request':
            pull_request = data['pull_request']

            src_repo = pull_request['head']['repo']['full_name']
            dst_repo = pull_request['base']['repo']['full_name']
            git_url = pull_request['base']['repo']['git_url']

            sha = pull_request['head']['sha']
            pr_no = pull_request['number']

            if action == 'opened':
                logger.debug('pull request %s opened: %s -> %s (%s)', pr_no, src_repo, dst_repo, sha)
                self.handle_pull_request_open(git_url, src_repo, dst_repo, pr_no, sha)

            elif action == 'synchronize':
                logger.debug('pull request %s updated: %s -> %s (%s)', pr_no, src_repo, dst_repo, sha)
                self.handle_pull_request_update(git_url, src_repo, dst_repo, pr_no, sha)

    def handle_pull_request_open(self, git_url, src_repo, dst_repo, pr_no, sha):
        self.schedule_pr_build(git_url, dst_repo, pr_no, sha)

    def handle_pull_request_update(self, git_url, src_repo, dst_repo, pr_no, sha):
        self.schedule_pr_build(git_url, dst_repo, pr_no, sha)

    def schedule_pr_build(self, git_url, repo, pr_no, sha):
        for board in settings.BOARDS:
            build_key = 'github/{}/{}/{}'.format(repo, pr_no, board)
            dockerctl.schedule_build(build_key, 'github', repo, git_url, pr_no, sha, board)


class BuildLogHandler(web.RequestHandler):
    def get(self):
        self.set_header('Content-Type', 'text/plain')
        self.finish(dockerctl.get_build_log(self.get_argument('id')))


@gen.coroutine
def set_status(repo, sha, status, target_url, description, context):
    client = httpclient.AsyncHTTPClient()

    access_token = settings.GITHUB_ACCESS_TOKEN
    url = 'https://api.github.com/repos/%s/statuses/%s?access_token=%s' % (repo, sha, access_token)
    headers = {
        'Content-Type': 'application/json',
        'User-Agent': repo
    }
    body = {
        'state': status,
        'target_url': target_url,
        'description': description,
        'context': context
    }
    body = json.dumps(body)

    try:
        yield client.fetch(url, headers=headers, body=body, method='POST')

    except Exception as e:
        if hasattr(e, 'response'):
            try:
                message = json.loads(e.response.body)

            except Exception:
                message = str(e)

        else:
            message = str(e)

        logger.error('sets status failed: %s', message, exc_info=True)


def _make_build_boards_key(repo, sha):
    return 'github/{}/{}/boards'.format(repo, sha)


def _make_target_url(build_info):
    return settings.WEB_BASE_URL + '/github_build_log?id=' + build_info['container_id']


@gen.coroutine
def handle_build_begin(build_info):
    if build_info['service'] != 'github':
        return  # not ours

    repo = build_info['repo']
    sha = build_info['version']
    board = build_info['board']
    status = 'pending'

    boards_key = _make_build_boards_key(repo, sha)
    boards = cache.get(boards_key, [])

    first_board = len(boards) == 0

    if board not in boards:
        boards.append(board)
        cache.set(boards_key, boards)

    if first_board:
        logger.debug('setting %s status for %s/%s', status, repo, sha)
        target_url = _make_target_url(build_info)

        yield set_status(repo, sha, status, target_url=target_url, description='', context=_STATUS_CONTEXT)


@gen.coroutine
def handle_build_end(build_info, exit_code, image_files):
    if build_info['service'] != 'github':
        return  # not ours

    repo = build_info['repo']
    sha = build_info['version']
    board = build_info['board']
    status = ['success', 'error'][bool(exit_code)]

    boards_key = _make_build_boards_key(repo, sha)
    boards = cache.get(boards_key, [])

    last_board = len(boards) == 1

    try:
        boards.remove(board)

    except ValueError:
        logger.warning('board %s not found in pending boards list', board)

    cache.set(boards_key, boards)

    if last_board:
        logger.debug('setting %s status for %s/%s', status, repo, sha)
        target_url = _make_target_url(build_info)

        yield set_status(repo, sha, status, target_url=target_url, description='', context=_STATUS_CONTEXT)


def handle_build_cancel(build_info):
    if build_info['service'] != 'github':
        return  # not ours

    repo = build_info['repo']
    sha = build_info['version']

    boards_key = _make_build_boards_key(repo, sha)
    cache.delete(boards_key)


def init():
    logger.debug('starting event server')

    application = web.Application([
        ('/github_event', EventHandler),
        ('/github_build_log', BuildLogHandler),
    ])

    application.listen(settings.WEB_PORT)

    logger.debug('registering build hooks')
    dockerctl.add_build_begin_handler(handle_build_begin)
    dockerctl.add_build_end_handler(handle_build_end)
    dockerctl.add_build_cancel_handler(handle_build_cancel)
