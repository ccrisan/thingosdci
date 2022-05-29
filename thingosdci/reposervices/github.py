
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


class GitHubRequestHandler(reposervices.RepoServiceRequestHandler):
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

            commit_id = pull_request['head']['sha']
            pr_no = pull_request['number']

            if action == 'opened':
                self.service.handle_pull_request_open(commit_id, src_repo, dst_repo, pr_no)

            elif action in ['synchronize', 'edited']:
                self.service.handle_pull_request_update(commit_id, src_repo, dst_repo, pr_no)

        elif github_event == 'push':
            if data['head_commit']:
                commit_id = data['head_commit']['id']
                branch_or_tag = data['ref'].split('/')[-1]

                if data['ref'].startswith('refs/tags/'):
                    self.service.handle_new_tag(commit_id, branch_or_tag)

                else:
                    self.service.handle_commit(commit_id, branch_or_tag)


class GitHub(reposervices.RepoService):
    REQUEST_HANDLER_CLASS = GitHubRequestHandler

    def __str__(self):
        return 'github'

    @gen.coroutine
    def _api_request(self, path, method='GET', body=None, extra_headers=None, timeout=settings.GITHUB_REQUEST_TIMEOUT):
        client = httpclient.AsyncHTTPClient()

        access_token = settings.GITHUB_ACCESS_TOKEN
        url = path
        if not (url.startswith('http://') or url.startswith('https://')):
            url = 'https://api.github.com' + url

        headers = {
            'Content-Type': 'application/json',
            'User-Agent': settings.REPO,
            'Authorization': 'token {}'.format(access_token)
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
        if getattr(e, 'response', None):
            try:
                return json.loads(e.response.body.decode('utf8'))

            except Exception:
                return str(e.response.body or e)

        else:
            return str(e)

    @gen.coroutine
    def _set_status(self, commit_id, status, target_url, description, context):
        if not commit_id:
            return

        description = description[:140]  # maximum allowed by github
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
    def set_pending(self, build, url, description):
        yield self._set_status(build.commit_id,
                               status='pending',
                               target_url=url,
                               description=description,
                               context=_STATUS_CONTEXT)

    @gen.coroutine
    def set_success(self, build, url, description):
        yield self._set_status(build.commit_id,
                               status='success',
                               target_url=url,
                               description=description,
                               context=_STATUS_CONTEXT)

    @gen.coroutine
    def set_failed(self, build, url, description):
        yield self._set_status(build.commit_id,
                               status='failure',
                               target_url=url,
                               description=description,
                               context=_STATUS_CONTEXT)

    @gen.coroutine
    def create_release(self, commit_id, tag, version, branch, build_type):
        path = '/repos/{}/releases/tags/{}'.format(settings.REPO, tag)

        logger.debug('looking for release %s', version)

        try:
            response = yield self._api_request(path)
            release_id = response['id']
            logger.debug('release %s found with id %s', version, release_id)

        except httpclient.HTTPError as e:
            if e.code == 404:  # no such release, we have to create it
                logger.debug('release %s not present', version)
                release_id = None

            else:
                logger.error('release %s failed: %s', version, self._api_error_message(e))
                return

        except Exception as e:
            logger.error('release %s failed: %s', version, self._api_error_message(e))
            return

        if release_id:
            logger.debug('removing previous release %s', version)

            path = '/repos/{}/releases/{}'.format(settings.REPO, release_id)

            try:
                yield self._api_request(path, method='DELETE')
                logger.debug('previous release %s removed', version)

            except httpclient.HTTPError as e:
                logger.error('failed to remove previous release %s: %s', version, self._api_error_message(e))
                raise

            logger.debug('removing git tag %s', tag)

            custom_cmd = 'git push --delete origin {}'.format(tag)

            try:
                yield building.run_custom_cmd(self, custom_cmd)
                logger.debug('git tag %s removed', tag)

            except Exception as e:
                logger.warning('failed to remove git tag %s: %s', tag, e, exc_info=True)

        logger.debug('creating release %s', version)

        path = '/repos/{}/releases'.format(settings.REPO)
        body = {
            'tag_name': tag,
            'name': version,
            'prerelease': True,
            'draft': build_type == building.TYPE_TAG  # never automatically release a tag build
        }

        if commit_id or branch:
            body['target_commitish'] = commit_id or branch

        try:
            response = yield self._api_request(path, method='POST', body=body)
            release_id = response['id']
            logger.debug('release %s created with id %s', version, release_id)

        except httpclient.HTTPError as e:
            logger.error('release %s failed: %s', version, self._api_error_message(e))
            raise

        return response

    @gen.coroutine
    def upload_release_file(self, release, board, tag, version, name, fmt, content):
        upload_url = release['upload_url']
        ut = uritemplate.URITemplate(upload_url)
        path = ut.expand(name=name)
        content_type = mimetypes.types_map.get(fmt, 'application/octet-stream')

        try:
            yield self._api_request(path, method='POST', body=content, extra_headers={'Content-Type': content_type},
                                    timeout=settings.UPLOAD_REQUEST_TIMEOUT)

        except httpclient.HTTPError as e:
            logger.error('failed to upload file %s: %s', name, self._api_error_message(e))
            return

    @gen.coroutine
    def add_s3_release_link(self, release, board, tag, version, name, fmt, s3_url):
        link = '[{}]({})'.format(name, s3_url)
        release['body'] = (release['body'] or '') + '\n' + link

        path = '/repos/{}/releases/{}'.format(settings.REPO, release['id'])
        logger.debug('updating release %s', version)

        release_body = {
            'body': release['body']
        }
        if tag:
            release_body['tag_name'] = tag

        try:
            yield self._api_request(path, method='PATCH', body=release_body)
            logger.debug('release %s updated with S3 URL %s', version, s3_url)

        except Exception as e:
            logger.error('release %s update failed: %s', version, self._api_error_message(e))
