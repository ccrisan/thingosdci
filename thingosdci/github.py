
import datetime
import hashlib
import hmac
import json
import logging
import os.path

from tornado import gen
from tornado import web
from tornado import httpclient

from thingosdci import cache
from thingosdci import dockerctl
from thingosdci import settings
from thingosdci import utils


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

        data = json.loads(str(self.request.body))

        github_event = self.request.headers['X-GitHub-Event']
        action = data['action']

        if github_event == 'pull_request':
            pull_request = data['pull_request']

            src_repo = pull_request['head']['repo']['full_name']
            dst_repo = pull_request['base']['repo']['full_name']
            git_url = pull_request['base']['repo']['git_url']

            commit = pull_request['head']['sha']
            pr_no = pull_request['number']

            if action == 'opened':
                logger.debug('pull request %s opened: %s -> %s (%s)', pr_no, src_repo, dst_repo, commit)
                self.handle_pull_request_open(git_url, src_repo, dst_repo, pr_no, commit)

            elif action == 'synchronize':
                logger.debug('pull request %s updated: %s -> %s (%s)', pr_no, src_repo, dst_repo, commit)
                self.handle_pull_request_update(git_url, src_repo, dst_repo, pr_no, commit)

        elif github_event == 'push':
            repo = data['repository']['full_name']
            git_url = data['repository']['git_url']

            commit = data['head_commit']['id']
            branch = data['ref'].split('/')[-1]

            logger.debug('push to %s (%s)', branch, commit)
            self.handle_push(git_url, repo, branch, commit)

    def handle_pull_request_open(self, git_url, src_repo, dst_repo, pr_no, commit):
        self.schedule_pr_build(git_url, dst_repo, pr_no, commit)

    def handle_pull_request_update(self, git_url, src_repo, dst_repo, pr_no, commit):
        self.schedule_pr_build(git_url, dst_repo, pr_no, commit)

    def handle_push(self, git_url, repo, branch, commit):
        if branch not in settings.BRANCHES_RELEASE:
            return

        self.schedule_branch_build(git_url, repo, branch, commit)

    def schedule_pr_build(self, git_url, repo, pr_no, commit):
        for board in settings.BOARDS:
            build_key = 'github/{}/{}/{}'.format(repo, pr_no, board)
            dockerctl.schedule_build(build_key, 'github', repo, git_url, board, commit, pr_no=pr_no)

    def schedule_branch_build(self, git_url, repo, branch, commit):
        today = datetime.date.today()

        for board in settings.BOARDS:
            build_key = 'github/{}/{}/{}'.format(repo, branch, board)
            version = utils.branches_format(settings.BRANCHES_LATEST_VERSION, branch, today)
            dockerctl.schedule_build(build_key, 'github', repo, git_url, board, commit, version=version, branch=branch)


class BuildLogHandler(web.RequestHandler):
    def get(self):
        self.set_header('Content-Type', 'text/plain')
        self.finish(dockerctl.get_build_log(self.get_argument('id')))


@gen.coroutine
def api_request(repo, path, method='GET', body=None, extra_headers=None):
    client = httpclient.AsyncHTTPClient()

    access_token = settings.GITHUB_ACCESS_TOKEN
    url = 'https://api.github.com' + path

    if '?' in url:
        url += '&'

    else:
        url += '?'

    url += 'access_token=' + access_token

    headers = {
        'Content-Type': 'application/json',
        'User-Agent': repo
    }

    headers.update(extra_headers or {})

    if not isinstance(body, str):
        body = json.dumps(body)

    yield client.fetch(url, headers=headers, method=method, body=body)


def api_error_message(e):
    if hasattr(e, 'response'):
        try:
            return json.loads(e.response.body)

        except Exception:
            return str(e)

    else:
        return str(e)


@gen.coroutine
def set_status(repo, commit, status, target_url, description, context):
    path = '/repos/{}/statuses/{}'.format(repo, commit)
    body = {
        'state': status,
        'target_url': target_url,
        'description': description,
        'context': context
    }

    try:
        yield api_request(repo, path, method='POST', body=body)

    except Exception as e:
        logger.error('sets status failed: %s', api_error_message(e))


@gen.coroutine
def upload_branch_build(repo, branch, version, commit, boards_image_files):
    today = datetime.date.today()
    tag = utils.branches_format(settings.BRANCHES_LATEST_TAG, branch, today)
    path = '/repos/{}/releases/tags/{}'.format(repo, tag)

    logger.debug('looking for release %s/%s', repo, tag)

    try:
        response = yield api_request(repo, path)
        release_id = response['id']
        logger.debug('release %s/%s found with id %s', repo, tag, release_id)

    except httpclient.HTTPError as e:
        if e.code == 404:  # no such release, we have to create it
            logger.debug('release %s/%s not present', repo, tag)
            release_id = None

        else:
            logger.error('upload branch build failed: %s', api_error_message(e))
            return

    except Exception as e:
        logger.error('upload branch build failed: %s', api_error_message(e))
        return

    if release_id:
        logger.debug('removing previous release %s/%s', repo, tag)

        path = '/repos/{}/releases/{}'.format(repo, release_id)

        try:
            yield api_request(repo, path, method='DELETE')
            logger.debug('previous release %s/%s removed', repo, tag)

        except httpclient.HTTPError as e:
            logger.error('failed to remove previous release %s/%s: %s', repo, tag, api_error_message(e))
            raise

    # TODO remove git tag

    logger.debug('creating release %s/%s', repo, tag)

    path = '/repos/{}/releases'.format(repo)
    body = {
        'tag_name': tag,
        'target_commitish': branch,
        'name': utils.branches_format(settings.BRANCHES_LATEST_RELEASE_NAME, today),
        'prerelease': True
    }

    try:
        response = yield api_request(repo, path, method='POST', body=body)
        release_id = response['id']
        logger.debug('release %s/%s created', repo, tag)

    except httpclient.HTTPError as e:
        logger.error('failed to create release %s/%s: %s', repo, tag, api_error_message(e))
        raise

    for board in settings.BOARDS:
        image_files = boards_image_files.get(board)
        if not image_files:
            logger.warning('no image files supplied for board %s', board)
            continue

        for fmt in settings.IMAGE_FILE_FORMATS:
            content_type = 'TODO'  # TODO
            files = [f for f in image_files if f.endswith(fmt)]
            if len(files) != 1:
                logger.warning('no image files supplied for board %s, format %s', board, fmt)
                continue

            file = files[0]
            name = os.path.basename(file)
            with open(file) as f:
                body = f.read()

            logger.debug('uploading image file %s (%s bytes)', file, len(body))

            path = '/repos/{}/releases/{}/assets?name={}'.format(repo, release_id, name)

            try:
                yield api_request(repo, path, method='POST', body=body, extra_headers={'Content-Type': content_type})
                logger.debug('image file %s uploaded', file)

            except httpclient.HTTPError as e:
                logger.error('failed to upload file %s: %s', file, api_error_message(e))
                raise


def _make_build_boards_key(repo, commit):
    return 'github/{}/{}/boards'.format(repo, commit)


def _make_build_boards_image_files_key(repo, commit):
    return 'github/{}/{}/boards_image_files'.format(repo, commit)


def _make_target_url(build_info):
    return settings.WEB_BASE_URL + '/github_build_log?id=' + build_info['container_id']


@gen.coroutine
def handle_build_begin(build_info):
    if build_info['service'] != 'github':
        return  # not ours

    repo = build_info['repo']
    commit = build_info['commit']
    board = build_info['board']
    status = 'pending'

    boards_key = _make_build_boards_key(repo, commit)
    boards = cache.get(boards_key, [])

    first_board = len(boards) == 0

    if board not in boards:
        boards.append(board)
        cache.set(boards_key, boards)

    if first_board:
        logger.debug('setting %s status for %s/%s', status, repo, commit)
        target_url = _make_target_url(build_info)

        yield set_status(repo, commit, status, target_url=target_url, description='', context=_STATUS_CONTEXT)


@gen.coroutine
def handle_build_end(build_info, exit_code, image_files):
    if build_info['service'] != 'github':
        return  # not ours

    repo = build_info['repo']
    commit = build_info['commit']
    board = build_info['board']
    status = ['success', 'error'][bool(exit_code)]

    boards_key = _make_build_boards_key(repo, commit)
    boards = cache.get(boards_key, [])

    boards_image_files_key = _make_build_boards_image_files_key(repo, commit)
    boards_image_files = cache.get(boards_image_files_key, {})

    last_board = len(boards) == 1

    try:
        boards.remove(board)

    except ValueError:
        logger.warning('board %s not found in pending boards list', board)

    cache.set(boards_key, boards)

    boards_image_files[board] = image_files
    cache.set(boards_image_files_key, boards_image_files)

    if last_board:
        logger.debug('setting %s status for %s/%s', status, repo, commit)
        target_url = _make_target_url(build_info)

        yield set_status(repo, commit, status, target_url=target_url, description='', context=_STATUS_CONTEXT)

        branch = build_info.get('branch')
        if branch:
            version = build_info.get('version', branch)
            yield upload_branch_build(repo, branch, version, commit, boards_image_files)


def handle_build_cancel(build_info):
    if build_info['service'] != 'github':
        return  # not ours

    repo = build_info['repo']
    commit = build_info['commit']

    boards_key = _make_build_boards_key(repo, commit)
    cache.delete(boards_key)

    boards_image_files_key = _make_build_boards_image_files_key(repo, commit)
    cache.delete(boards_image_files_key)


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
