
import json
import logging

from tornado import gen
from tornado import httpclient

from thingosdci import building
from thingosdci import reposervices
from thingosdci import settings
from thingosdci import utils


logger = logging.getLogger(__name__)


_BUILD_NAME = 'thingOS Docker CI'


class BitBucket(reposervices.RepoService):
    def __str__(self):
        return 'bitbucket'

    def post(self):
        data = json.loads(str(self.request.body.decode('utf8')))
        event = self.request.headers['X-Event-Key']

        if event == 'repo:push':
            changes = data['push']['changes']
            for change in changes:
                change_type = change['new']['type']
                name = change['new']['name']
                commit_id = change['new']['target']['hash']

                if change_type == 'tag':
                    self.handle_new_tag(commit_id, name)

                elif change_type == 'branch':
                    self.handle_commit(commit_id, name)

        elif event in ('pullrequest:created', 'pullrequest:updated'):
            pull_request = data['pullrequest']
            commit_id = pull_request['commit']['hash']
            src_repo = pull_request['source']['repository']['full_name']
            dst_repo = pull_request['destination']['repository']['full_name']
            pr_no = pull_request['id']

            if event.endswith('created'):
                self.handle_pull_request_open(commit_id, src_repo, dst_repo, pr_no)

            else:  # assuming updated
                self.handle_pull_request_update(commit_id, src_repo, dst_repo, pr_no)

    @gen.coroutine
    def _api_request(self, path, method='GET', body=None, extra_headers=None,
                     timeout=settings.BITBUCKET_REQUEST_TIMEOUT):

        client = httpclient.AsyncHTTPClient()

        url = path
        if not (url.startswith('http://') or url.startswith('https://')):
            url = 'https://api.bitbucket.org/2.0' + url

        headers = {
            'Content-Type': 'application/json',
            'User-Agent': settings.REPO
        }

        headers.update(extra_headers or {})

        if body is not None and headers['Content-Type'] == 'application/json':
            body = json.dumps(body)

        response = yield client.fetch(url, headers=headers, method=method, body=body,
                                      auth_username=settings.BITBUCKET_USERNAME,
                                      auth_password=settings.BITBUCKET_PASSWORD,
                                      connect_timeout=timeout, request_timeout=timeout)
        if not response.body:
            return None

        return json.loads(response.body.decode('utf8'))

    @staticmethod
    def _api_error_message(e):
        if hasattr(e, 'response'):
            try:
                return json.loads(e.response.body.decode('utf8'))['error']['message']

            except Exception:
                return str(e.response.body)

        else:
            return str(e)

    @gen.coroutine
    def _set_status(self, commit_id, status, url, description, name):
        path = '/repositories/{}/commit/{}/statuses/build'.format(settings.REPO, commit_id)
        body = {
            'state': status,
            'url': url,
            'description': description,
            'name': name
        }

        try:
            yield self._api_request(path, method='POST', body=body)

        except Exception as e:
            logger.error('sets status failed: %s', self._api_error_message(e))

    @gen.coroutine
    def set_pending(self, build, completed_builds, remaining_builds):
        running_remaining_builds = [b for b in remaining_builds if b.get_state() == building.STATE_RUNNING]
        if running_remaining_builds:
            running_build = running_remaining_builds[0]

        else:
            running_build = build

        url = self.make_log_url(running_build)
        description = 'building OS images ({}/{})'.format(len(completed_builds), len(settings.BOARDS))

        yield self._set_status(build.commit_id,
                               status='INPROGRESS',
                               url=url,
                               description=description,
                               name=_BUILD_NAME)

    @gen.coroutine
    def set_success(self, build):
        url = self.make_log_url(build)
        description = 'OS images successfully built ({}/{})'.format(len(settings.BOARDS), len(settings.BOARDS))

        yield self._set_status(build.commit_id,
                               status='SUCCESS',
                               url=url,
                               description=description,
                               name=_BUILD_NAME)

    @gen.coroutine
    def set_failed(self, build, failed_builds):
        if not failed_builds:
            logger.warning('cannot set failed status with no failed builds')
            return

        url = self.make_log_url(failed_builds[0])
        failed_boards_str = ', '.join([b.board for b in failed_builds])
        description = 'failed to build some OS images: {}'.format(failed_boards_str)

        yield self._set_status(build.commit_id,
                               status='FAILED',
                               url=url,
                               description=description,
                               name=_BUILD_NAME)

    @gen.coroutine
    def create_release(self, commit_id, tag, name, build_type):
        logger.debug('creating tag %s', tag)

        path = '/repositories/{}/refs/tags'.format(settings.REPO)
        body = {
            'name': tag,
            'target': {
                'hash': commit_id
            }
        }

        try:
            yield self._api_request(path, method='POST', body=body)
            logger.debug('tag created')

        except Exception as e:
            logger.error('tag creation failed: %s', self._api_error_message(e))

    @gen.coroutine
    def upload_release_file(self, release, board, name, fmt, content):
        path = '/repositories/{}/downloads'.format(settings.REPO)

        content_type, body = utils.encode_multipart_formdata(files={'files': (name, content)})
        headers = {
            'Content-Type': content_type,
            'Content-Length': str(len(body))
        }

        try:
            yield self._api_request(path, method='POST', body=body, extra_headers=headers,
                                    timeout=settings.BITBUCKET_UPLOAD_REQUEST_TIMEOUT)

        except httpclient.HTTPError as e:
            logger.error('failed to upload file %s: %s', name, self._api_error_message(e))
            raise