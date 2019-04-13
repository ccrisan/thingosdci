
import json
import logging
import re

from tornado import gen
from tornado import web
from tornado import httpclient

from thingosdci import reposervices
from thingosdci import settings
from thingosdci import utils


logger = logging.getLogger(__name__)


_STATUS_CONTEXT = 'thingOS Docker CI'


class GitLabRequestHandler(reposervices.RepoServiceRequestHandler):
    def post(self):
        # verify signature
        token = self.request.headers.get('X-Gitlab-Token')
        if not token:
            logger.warning('missing token header')
            raise web.HTTPError(401)

        if token != settings.WEB_SECRET:
            logger.warning('mismatching token')
            raise web.HTTPError(401)

        data = json.loads(str(self.request.body.decode('utf8')))
        gitlab_event = self.request.headers['X-Gitlab-Event']

        if gitlab_event == 'Push Hook':
            branch = data['ref'].split('/')[-1]
            for commit in data['commits']:
                commit_id = commit['id']
                self.service.handle_commit(commit_id, branch)

        elif gitlab_event == 'Tag Push Hook':
            tag = data['ref'].split('/')[-1]
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
    BASE_URL = 'https://gitlab.com'

    def __str__(self):
        return 'gitlab'

    @gen.coroutine
    def _api_request(self, path, method='GET', body=None, extra_headers=None, timeout=settings.GITLAB_REQUEST_TIMEOUT):
        client = httpclient.AsyncHTTPClient()

        url = path
        if not (url.startswith('http://') or url.startswith('https://')):
            url = self.BASE_URL + '/api/v4' + url

        if '?' in url:
            url += '&'

        else:
            url += '?'

        headers = {
            'Content-Type': 'application/json',
            'User-Agent': settings.REPO,
            'Private-Token': settings.GITLAB_ACCESS_TOKEN
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
                return str(e.response.body or e)

        else:
            return str(e)

    @gen.coroutine
    def _set_status(self, commit_id, state, target_url, description, context):
        if not commit_id:
            return

        path = '/projects/{}/statuses/{}'.format(settings.GITLAB_PROJECT_ID, commit_id)
        body = {
            'state': state,
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
                               state='pending',
                               target_url=url,
                               description=description,
                               context=_STATUS_CONTEXT)

    @gen.coroutine
    def set_success(self, build, url, description):
        yield self._set_status(build.commit_id,
                               state='success',
                               target_url=url,
                               description=description,
                               context=_STATUS_CONTEXT)

    @gen.coroutine
    def set_failed(self, build, url, description):
        yield self._set_status(build.commit_id,
                               state='failed',
                               target_url=url,
                               description=description,
                               context=_STATUS_CONTEXT)

    @gen.coroutine
    def create_release(self, commit_id, tag, version, branch, build_type):
        logger.debug('looking for tag %s', tag)

        path = '/projects/{}/repository/tags/{}'.format(settings.GITLAB_PROJECT_ID, tag)
        tag_exists = False
        try:
            yield self._api_request(path)
            logger.debug('tag %s found', tag)
            tag_exists = True

        except httpclient.HTTPError as e:
            if e.code == 404:  # no such release, we have to create it
                logger.debug('tag %s not present', tag)

            else:
                logger.error('tag lookup failed: %s', self._api_error_message(e))
                return

        except Exception as e:
            logger.error('tag lookup failed: %s', self._api_error_message(e))
            return

        if not tag_exists:
            logger.debug('creating tag %s', tag)

            path = '/projects/{}/repository/tags'.format(settings.GITLAB_PROJECT_ID)
            try:
                yield self._api_request(path, method='POST', body={'tag_name': tag, 'ref': commit_id or branch})
                logger.debug('tag %s created', tag)

            except Exception as e:
                logger.error('tag creation failed: %s', self._api_error_message(e))
                return

    @gen.coroutine
    def upload_release_file(self, release, board, tag, version, name, fmt, content):
        logger.debug('uploading release file %s', name)

        path = '/projects/{}/uploads'.format(settings.GITLAB_PROJECT_ID)
        content_type, body = utils.encode_multipart_formdata(files={'file': (name, content)})
        headers = {
            'Content-Type': content_type,
            'Content-Length': str(len(body))
        }

        try:
            response = yield self._api_request(path, method='POST', body=body, extra_headers=headers,
                                               timeout=settings.UPLOAD_REQUEST_TIMEOUT)

        except httpclient.HTTPError as e:
            logger.error('failed to upload file %s: %s', name, self._api_error_message(e))
            return

        logger.debug('creating release %s', version)

        markdown = response['markdown']
        m = re.search(r'\[(.*)\]\((.*)\)', markdown)
        if m:
            # make link absolute
            url = self.BASE_URL + '/' + settings.REPO + m.group(2)
            link = '[{}]({})'.format(m.group(1), url)

        else:
            link = markdown

        release_exists = False
        path = '/projects/{}/repository/tags/{}'.format(settings.GITLAB_PROJECT_ID, tag)
        try:
            response = yield self._api_request(path)
            release = response.get('release')
            if release:
                release_exists = True

                # append to existing description
                description = (release.get('description') or '') + '\n\n' + link

            else:
                description = link

        except Exception as e:
            logger.error('release %s failed: %s', version, self._api_error_message(e))
            return

        path += '/release'

        if release_exists:
            try:
                yield self._api_request(path, method='PUT', body={'description': description})
                logger.debug('release %s updated', version)

            except Exception as e:
                logger.error('release %s failed: %s', version, self._api_error_message(e))
                return

        else:
            try:
                yield self._api_request(path, method='POST', body={'description': description})
                logger.debug('release %s created', version)

            except Exception as e:
                logger.error('release %s failed: %s', version, self._api_error_message(e))
                return
