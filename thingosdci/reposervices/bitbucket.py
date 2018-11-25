
import json
import logging

from tornado import gen
from tornado import httpclient

from thingosdci import reposervices
from thingosdci import settings
from thingosdci import utils


logger = logging.getLogger(__name__)


_BUILD_NAME = 'thingOS Docker CI'


class BitBucketRequestHandler(reposervices.RepoServiceRequestHandler):
    def post(self):
        data = json.loads(str(self.request.body.decode('utf8')))
        event = self.request.headers['X-Event-Key']

        if event == 'repo:push':
            changes = data['push']['changes']
            for change in changes:
                if 'new' not in change:
                    continue

                change_type = change['new']['type']
                name = change['new']['name']
                commit_id = change['new']['target']['hash']

                if change_type == 'tag':
                    self.service.handle_new_tag(commit_id, name)

                elif change_type == 'branch':
                    self.service.handle_commit(commit_id, name)

        elif event in ('pullrequest:created', 'pullrequest:updated'):
            pull_request = data['pullrequest']
            commit_id = pull_request['source']['commit']['hash']
            src_repo = pull_request['source']['repository']['full_name']
            dst_repo = pull_request['destination']['repository']['full_name']
            pr_no = pull_request['id']

            if event.endswith('created'):
                self.service.handle_pull_request_open(commit_id, src_repo, dst_repo, pr_no)

            else:  # assuming updated
                self.service.handle_pull_request_update(commit_id, src_repo, dst_repo, pr_no)


class BitBucket(reposervices.RepoService):
    REQUEST_HANDLER_CLASS = BitBucketRequestHandler

    def __str__(self):
        return 'bitbucket'

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
        if not commit_id:
            return

        path = '/repositories/{}/commit/{}/statuses/build'.format(settings.REPO, commit_id)
        body = {
            'state': status,
            'url': url,
            'description': description,
            'name': name,
            'key': commit_id
        }

        try:
            yield self._api_request(path, method='POST', body=body)

        except Exception as e:
            logger.error('set status failed: %s', self._api_error_message(e))
            raise

    @gen.coroutine
    def set_pending(self, build, url, description):
        yield self._set_status(build.commit_id,
                               status='INPROGRESS',
                               url=url,
                               description=description,
                               name=_BUILD_NAME)

    @gen.coroutine
    def set_success(self, build, url, description):
        yield self._set_status(build.commit_id,
                               status='SUCCESSFUL',
                               url=url,
                               description=description,
                               name=_BUILD_NAME)

    @gen.coroutine
    def set_failed(self, build, url, description):
        yield self._set_status(build.commit_id,
                               status='FAILED',
                               url=url,
                               description=description,
                               name=_BUILD_NAME)

    @gen.coroutine
    def create_release(self, commit_id, tag, version, branch, build_type):
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
            msg = self._api_error_message(e)
            if msg.count('already exists'):
                logger.debug('tag already exists')

            else:
                logger.error('tag creation failed: %s', msg)

    @gen.coroutine
    def upload_release_file(self, release, board, tag, version, name, fmt, content):
        path = '/repositories/{}/downloads'.format(settings.REPO)

        content_type, body = utils.encode_multipart_formdata(files={'files': (name, content)})
        headers = {
            'Content-Type': content_type,
            'Content-Length': str(len(body))
        }

        try:
            yield self._api_request(path, method='POST', body=body, extra_headers=headers,
                                    timeout=settings.UPLOAD_REQUEST_TIMEOUT)

        except httpclient.HTTPError as e:
            logger.error('failed to upload file %s: %s', name, self._api_error_message(e))
            return
