
import hashlib
import hmac
import json
import logging
import mimetypes
import uritemplate

from tornado import gen
from tornado import web
from tornado import httpclient

from thingosdci import building
from thingosdci import reposervices
from thingosdci import settings


logger = logging.getLogger(__name__)


_STATUS_CONTEXT = 'thingOS Docker CI'


class GitLabRequestHandler(reposervices.RepoServiceRequestHandler):
    def post(self):
        # verify signature
        token = self.request.headers.get('X-Gitlab-Token')
        if not token:
            logger.warning('missing token header')
            raise web.HTTPError(401)

        if token != settings.GITLAB_ACCESS_TOKEN:
            logger.warning('mismatching token')
            raise web.HTTPError(401)

        data = json.loads(str(self.request.body.decode('utf8')))
        gitlab_event = self.request.headers['X-Gitlab-Event']

        if gitlab_event == 'Push Hook':
            branch = data['refs'].split('/')[-1]
            for commit in data['commits']:
                commit_id = commit['id']
                self.service.handle_commit(commit_id, branch)

        elif gitlab_event == 'Tag Push Hook':
            tag = data['refs'].split('/')[-1]
            commit_id = data['checkout_sha']
            if commit_id:  # can be null, if tag deleted
                self.service.handle_new_tag(commit_id, tag)

        elif gitlab_event == 'Merge Request Hook':
            attrs = data['object_attributes']
            action = attrs['action']
            commit_id = attrs['last_commit']['id']
            src_repo = attrs['source']['url']
            dst_repo = attrs['target']['url']
            pr_no = attrs['iid']

            if action == 'open':
                self.service.handle_pull_request_open(commit_id, src_repo, dst_repo, pr_no)

            elif action == 'update':
                self.service.handle_pull_request_update(commit_id, src_repo, dst_repo, pr_no)


class GitLab(reposervices.RepoService):
    REQUEST_HANDLER_CLASS = GitLabRequestHandler

    def __str__(self):
        return 'gitlab'

    @gen.coroutine
    def _api_request(self, path, method='GET', body=None, extra_headers=None, timeout=settings.GITHUB_REQUEST_TIMEOUT):
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

        if body is not None and not isinstance(body, str) and headers['Content-Type'] == 'application/json':
            body = json.dumps(body)

        response = yield client.fetch(url, headers=headers, method=method, body=body,
                                      connect_timeout=timeout, request_timeout=timeout)
        if not response.body:
            return None

        return json.loads(response.body.decode('utf8'))

    @staticmethod
    def _api_error_message(e):
        if hasattr(e, 'response'):
            try:
                return json.loads(e.response.body.decode('utf8'))

            except Exception:
                return str(e.response.body)

        else:
            return str(e)

    @gen.coroutine
    def _set_status(self, commit_id, status, target_url, description, context):
        path = '/repos/{}/statuses/{}'.format(settings.REPO, commit_id)
        body = {
            'state': status,
            'target_url': target_url,
            'description': description,
            'context': context
        }

        try:
            yield self._api_request(path, method='POST', body=body)

        except Exception as e:
            logger.error('set status failed: %s', self._api_error_message(e))

    @gen.coroutine
    def set_pending(self, build, completed_builds, remaining_builds):
        running_remaining_builds = [b for b in remaining_builds if b.get_state() == building.STATE_RUNNING]
        if running_remaining_builds:
            running_build = running_remaining_builds[0]

        else:
            running_build = build

        target_url = self.make_log_url(running_build)
        description = 'building OS images ({}/{})'.format(len(completed_builds), len(settings.BOARDS))

        logger.debug('setting pending status for %s: %s', build, description)

        yield self._set_status(build.commit_id,
                               status='pending',
                               target_url=target_url,
                               description=description,
                               context=_STATUS_CONTEXT)

    @gen.coroutine
    def set_success(self, build):
        target_url = self.make_log_url(build)
        description = 'OS images successfully built ({}/{})'.format(len(settings.BOARDS), len(settings.BOARDS))

        logger.debug('setting success status for %s: %s', build, description)

        yield self._set_status(build.commit_id,
                               status='success',
                               target_url=target_url,
                               description=description,
                               context=_STATUS_CONTEXT)

    @gen.coroutine
    def set_failed(self, build, failed_builds):
        if not failed_builds:
            logger.warning('cannot set failed status with no failed builds')
            return

        target_url = self.make_log_url(failed_builds[0])
        failed_boards_str = ', '.join([b.board for b in failed_builds])
        description = 'failed to build some OS images: {}'.format(failed_boards_str)

        logger.debug('setting failed status for %s: %s', build, description)

        description = description[:140]  # maximum allowed by github
        yield self._set_status(build.commit_id,
                               status='failure',
                               target_url=target_url,
                               description=description,
                               context=_STATUS_CONTEXT)

    @gen.coroutine
    def create_release(self, commit_id, tag, name, build_type):
        path = '/repos/{}/releases/tags/{}'.format(settings.REPO, tag)

        logger.debug('looking for release %s', tag)

        try:
            response = yield self._api_request(path)
            release_id = response['id']
            logger.debug('release %s found with id %s', tag, release_id)

        except httpclient.HTTPError as e:
            if e.code == 404:  # no such release, we have to create it
                logger.debug('release %s not present', tag)
                release_id = None

            else:
                logger.error('upload branch build failed: %s', self._api_error_message(e))
                return

        except Exception as e:
            logger.error('upload branch build failed: %s', self._api_error_message(e))
            return

        if release_id:
            logger.debug('removing previous release %s', tag)

            path = '/repos/{}/releases/{}'.format(settings.REPO, release_id)

            try:
                yield self._api_request(path, method='DELETE')
                logger.debug('previous release %s removed', tag)

            except httpclient.HTTPError as e:
                logger.error('failed to remove previous release %s: %s', tag, self._api_error_message(e))
                raise

            logger.debug('removing git tag %s', tag)

            custom_cmd = 'git push --delete origin {}'.format(tag)

            try:
                yield building.run_custom_cmd(self, custom_cmd)
                logger.debug('git tag %s removed', tag)

            except Exception as e:
                logger.warning('failed to remove git tag %s: %s', tag, e, exc_info=True)

        logger.debug('creating release %s', tag)

        path = '/repos/{}/releases'.format(settings.REPO)
        body = {
            'tag_name': tag,
            'target_commitish': commit_id,
            'name': name,
            'prerelease': True,
            'draft': build_type == building.TYPE_TAG  # never automatically release a tag build
        }

        try:
            response = yield self._api_request(path, method='POST', body=body)
            release_id = response['id']
            logger.debug('release %s created with id %s', tag, release_id)

        except httpclient.HTTPError as e:
            logger.error('failed to create release %s: %s', tag, self._api_error_message(e))
            raise

        return response

    @gen.coroutine
    def upload_release_file(self, release, board, name, fmt, content):
        upload_url = release['upload_url']
        ut = uritemplate.URITemplate(upload_url)
        path = ut.expand(name=name)
        content_type = mimetypes.types_map.get(fmt, 'application/octet-stream')

        try:
            yield self._api_request(path, method='POST', body=content, extra_headers={'Content-Type': content_type},
                                    timeout=settings.GITHUB_UPLOAD_REQUEST_TIMEOUT)

        except httpclient.HTTPError as e:
            logger.error('failed to upload file %s: %s', name, self._api_error_message(e))
            raise