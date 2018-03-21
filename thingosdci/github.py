
import datetime
import hashlib
import hmac
import json
import logging
import mimetypes
import os.path
import uritemplate

from tornado import gen
from tornado import web
from tornado import httpclient

from thingosdci import cache
from thingosdci import dockerctl
from thingosdci import settings
from thingosdci import utils


logger = logging.getLogger(__name__)


_STATUS_CONTEXT = 'thingOS Docker CI'


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

        data = json.loads(str(self.request.body.decode('utf8')))

        github_event = self.request.headers['X-GitHub-Event']

        if github_event == 'pull_request':
            action = data['action']
            pull_request = data['pull_request']

            src_repo = pull_request['head']['repo']['full_name']
            dst_repo = pull_request['base']['repo']['full_name']

            commit = pull_request['head']['sha']
            pr_no = pull_request['number']

            if action == 'opened':
                logger.debug('pull request %s opened: %s -> %s (%s)', pr_no, src_repo, dst_repo, commit)
                self.handle_pull_request_open(pr_no, commit)

            elif action == 'synchronize':
                logger.debug('pull request %s updated: %s -> %s (%s)', pr_no, src_repo, dst_repo, commit)
                self.handle_pull_request_update(pr_no, commit)

        elif github_event == 'push':
            if data['head_commit']:
                commit = data['head_commit']['id']
                branch = data['ref'].split('/')[-1]

                logger.debug('push to %s (%s)', branch, commit)
                self.handle_push(branch, commit)

    def handle_pull_request_open(self, pr_no, commit):
        self.schedule_pr_build(pr_no, commit)

    def handle_pull_request_update(self, pr_no, commit):
        self.schedule_pr_build(pr_no, commit)

    def handle_push(self, branch, commit):
        if branch not in settings.BRANCHES_RELEASE:
            return

        self.schedule_branch_build(branch, commit)

    def schedule_pr_build(self, pr_no, commit):
        for board in settings.BOARDS:
            build_key = 'github/{}/{}/{}'.format(settings.REPO, pr_no, board)
            dockerctl.schedule_build(build_key, 'github', settings.REPO, settings.GIT_URL, board, commit, pr_no=pr_no)

    def schedule_branch_build(self, branch, commit):
        today = datetime.date.today()

        for board in settings.BOARDS:
            build_key = 'github/{}/{}/{}'.format(settings.REPO, branch, board)
            version = utils.branches_format(settings.BRANCHES_LATEST_VERSION, branch, today)
            dockerctl.schedule_build(build_key, 'github', settings.REPO, settings.GIT_URL, board, commit,
                                     version=version, branch=branch)


class BuildLogHandler(web.RequestHandler):
    def get(self):
        self.set_header('Content-Type', 'text/plain')
        lines = self.get_argument('lines', None)
        if lines:
            try:
                lines = int(lines)

            except ValueError:
                lines = 1

        self.finish(dockerctl.get_build_log(self.get_argument('id'), lines))


@gen.coroutine
def api_request(path, method='GET', body=None, extra_headers=None):
    client = httpclient.AsyncHTTPClient()

    access_token = settings.GITHUB_ACCESS_TOKEN
    url = path
    if not (url.startswith('http://') or url.startswith('https://')):
        url = 'https://api.github.com' + url

    if '?' in url:
        url += '&'

    else:
        url += '?'

    url += 'access_token=' + access_token

    headers = {
        'Content-Type': 'application/json',
        'User-Agent': settings.REPO
    }

    headers.update(extra_headers or {})

    if body is not None and not isinstance(body, str):
        body = json.dumps(body)

    response = yield client.fetch(url, headers=headers, method=method, body=body)
    if not response.body:
        return None

    return json.loads(response.body.decode('utf8'))


def api_error_message(e):
    if hasattr(e, 'response'):
        try:
            return json.loads(e.response.body.decode('utf8'))

        except Exception:
            return str(e)

    else:
        return str(e)


@gen.coroutine
def set_status(commit, status, target_url, description, context):
    path = '/repos/{}/statuses/{}'.format(settings.REPO, commit)
    body = {
        'state': status,
        'target_url': target_url,
        'description': description,
        'context': context
    }

    try:
        yield api_request(path, method='POST', body=body)

    except Exception as e:
        logger.error('sets status failed: %s', api_error_message(e))


@gen.coroutine
def upload_branch_build(branch, commit, version, boards_image_files):
    today = datetime.date.today()
    tag = utils.branches_format(settings.BRANCHES_LATEST_TAG, branch, today)
    path = '/repos/{}/releases/tags/{}'.format(settings.REPO, tag)

    logger.debug('looking for release %s/%s', settings.REPO, tag)

    try:
        response = yield api_request(path)
        release_id = response['id']
        logger.debug('release %s/%s found with id %s', settings.REPO, tag, release_id)

    except httpclient.HTTPError as e:
        if e.code == 404:  # no such release, we have to create it
            logger.debug('release %s/%s not present', settings.REPO, tag)
            release_id = None

        else:
            logger.error('upload branch build failed: %s', api_error_message(e))
            return

    except Exception as e:
        logger.error('upload branch build failed: %s', api_error_message(e))
        return

    if release_id:
        logger.debug('removing previous release %s/%s', settings.REPO, tag)

        path = '/repos/{}/releases/{}'.format(settings.REPO, release_id)

        try:
            yield api_request(path, method='DELETE')
            logger.debug('previous release %s/%s removed', settings.REPO, tag)

        except httpclient.HTTPError as e:
            logger.error('failed to remove previous release %s/%s: %s', settings.REPO, tag, api_error_message(e))
            raise

    logger.debug('removing git tag %s/%s', settings.REPO, tag)

    build_key = 'github/{}/remove-git-tag'.format(settings.REPO)
    board = settings.BOARDS[0]  # some dummy board
    build_cmd = 'git push --delete origin {}'.format(tag)

    try:
        yield dockerctl.run_custom_build_cmd(build_key, 'github', settings.REPO, settings.GIT_URL,
                                             board, commit, build_cmd, version)
        logger.debug('git tag %s/%s removed', settings.REPO, tag)

    except Exception as e:
        logger.warning('failed to remove git tag %s/%s: %s', settings.REPO, tag, api_error_message(e))

    logger.debug('creating release %s/%s', settings.REPO, tag)

    path = '/repos/{}/releases'.format(settings.REPO)
    body = {
        'tag_name': tag,
        'target_commitish': branch,
        'name': utils.branches_format(settings.BRANCHES_LATEST_RELEASE_NAME, branch, today),
        'prerelease': True
    }

    try:
        response = yield api_request(path, method='POST', body=body)
        release_id = response['id']
        upload_url = response['upload_url']
        logger.debug('release %s/%s created with id %s', settings.REPO, tag, release_id)

    except httpclient.HTTPError as e:
        logger.error('failed to create release %s/%s: %s', settings.REPO, tag, api_error_message(e))
        raise

    for board in settings.BOARDS:
        image_files = boards_image_files.get(board)
        if not image_files:
            logger.warning('no image files supplied for board %s', board)
            continue

        for fmt in settings.IMAGE_FILE_FORMATS:
            content_type = mimetypes.types_map.get(fmt, 'application/octet-stream')
            files = [f for f in image_files if f.endswith(fmt)]
            if len(files) != 1:
                logger.warning('no image files supplied for board %s, format %s', board, fmt)
                continue

            file = files[0]
            file = os.path.join(settings.OUTPUT_DIR, board, 'images', file)
            name = os.path.basename(file)
            with open(file) as f:
                body = f.read()

            logger.debug('uploading image file %s (%s bytes)', file, len(body))

            ut = uritemplate.URITemplate(upload_url)
            path = ut.expand(name=name)

            try:
                yield api_request(path, method='POST', body=body, extra_headers={'Content-Type': content_type})
                logger.debug('image file %s uploaded', file)

            except httpclient.HTTPError as e:
                logger.error('failed to upload file %s: %s', file, api_error_message(e))
                raise


def _make_build_boards_key(commit):
    return 'github/{}/{}/boards'.format(settings.REPO, commit)


def _make_build_boards_image_files_key(commit):
    return 'github/{}/{}/boards_image_files'.format(settings.REPO, commit)


def _make_target_url(build_info):
    return settings.WEB_BASE_URL + '/github_build_log?id={}&lines=100'.format(build_info['container_id'])


@gen.coroutine
def handle_build_begin(build_info):
    if build_info['service'] != 'github':
        return  # not ours

    if not build_info['build_key'].endswith('/{}'.format(build_info['board'])):
        return  # not an OS image build

    commit = build_info['commit']
    board = build_info['board']
    status = 'pending'

    boards_key = _make_build_boards_key(commit)
    boards = cache.get(boards_key, [])

    first_board = len(boards) == 0

    if board not in boards:
        boards.append(board)
        cache.set(boards_key, boards)

    if first_board and build_info['container_id']:
        logger.debug('setting %s status for %s/%s', status, settings.REPO, commit)
        target_url = _make_target_url(build_info)

        yield set_status(commit, status, target_url=target_url,
                         description='building OS images', context=_STATUS_CONTEXT)


@gen.coroutine
def handle_build_end(build_info, exit_code, image_files):
    if build_info['service'] != 'github':
        return  # not ours

    if not build_info['build_key'].endswith('/{}'.format(build_info['board'])):
        return  # not an OS image build

    commit = build_info['commit']
    board = build_info['board']
    status = ['success', 'error'][bool(exit_code)]
    description = ['OS images successfully built', 'failed to build OS images'][bool(exit_code)]

    boards_key = _make_build_boards_key(commit)
    boards = cache.get(boards_key, [])

    boards_image_files_key = _make_build_boards_image_files_key(commit)
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
        logger.debug('setting %s status for %s/%s', status, settings.REPO, commit)
        target_url = _make_target_url(build_info)

        yield set_status(commit, status, target_url=target_url, description=description, context=_STATUS_CONTEXT)

        branch = build_info.get('branch')
        if branch and not exit_code:
            version = build_info.get('version', branch)
            yield upload_branch_build(branch, commit, version, boards_image_files)


def handle_build_cancel(build_info):
    if build_info['service'] != 'github':
        return  # not ours

    if not build_info['build_key'].endswith('/{}'.format(build_info['board'])):
        return  # not an OS image build

    commit = build_info['commit']

    boards_key = _make_build_boards_key(commit)
    cache.delete(boards_key)

    boards_image_files_key = _make_build_boards_image_files_key(commit)
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
