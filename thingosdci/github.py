
import json
import logging
import os

from tornado import gen
from tornado import web
from tornado import httpclient

from thingosdc  i import dockerctl
from thingosdci import settings


logger = logging.getLogger(__name__)


STATUS_CONTEXT = 'ci/thingos-builder'


class EventHandler(web.RequestHandler):
    def post(self):
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
            build_id = 'github/{}/{}/{}'.format(repo, pr_no, board)
            dockerctl.schedule_build(build_id, 'github', git_url, pr_no, sha, board)


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


def start_event_server():
    application = web.Application([
        ('/github_event', EventHandler),
    ])

    application.listen(settings.WEB_PORT)
