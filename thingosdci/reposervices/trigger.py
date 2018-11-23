
import logging

from tornado import web


logger = logging.getLogger(__name__)


class TriggerRequestHandler(web.RequestHandler):
    def initialize(self, service):
        self.service = service

    def post(self):
        typ = self.get_argument('type')

        if typ == 'nightly':
            branch = self.get_argument('branch')
            self.schedule_nightly_build(branch)

        elif typ == 'tag':
            tag = self.get_argument('tag')
            self.schedule_tag_build(tag)

        else:
            raise web.HTTPError(400, 'unknown type {}'.format(typ))

    def schedule_nightly_build(self, branch):
        self.service.schedule_nightly_build(commit_id=None, branch=branch)

    def schedule_tag_build(self, tag):
        self.service.handle_new_tag(commit_id=None, tag=tag)
