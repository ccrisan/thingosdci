
import datetime
import functools
import logging
import os
import re

from tornado import gen
from tornado import web

from thingosdci import building
from thingosdci import settings
from thingosdci import utils


_SERVICE_CLASSES = {}

logger = logging.getLogger(__name__)

_service = None


class RepoService(web.RequestHandler):
    def __init__(self, *args, **kwargs):
        super(RepoService, self).__init__(*args, **kwargs)

    def handle_pull_request_open(self, pr_no):
        build_group = building.BuildGroup()

        for board in settings.BOARDS:
            build = building.schedule_pr_build(self, build_group, board, pr_no)
            self._register_build(build)

    def handle_pull_request_update(self, pr_no):
        build_group = building.BuildGroup()

        for board in settings.BOARDS:
            build = building.schedule_pr_build(self, build_group, board, pr_no)
            self._register_build(build)

    def handle_push(self, commit_id, branch):
        if branch not in settings.NIGHTLY_BRANCHES:
            return

        build_group = building.BuildGroup()

        for board in settings.BOARDS:
            build = building.schedule_nightly_build(self, build_group, board, commit_id, branch)
            self._register_build(build)

    def handle_new_tag(self, tag):
        if not re.match(settings.RELEASE_TAG_REGEX, tag):
            return

        build_group = building.BuildGroup()

        for board in settings.BOARDS:
            build = building.schedule_tag_build(self, build_group, board, tag)
            self._register_build(build)

    @gen.coroutine
    def handle_build_begin(self, build):
        logger.debug('handling %s begin', build)

        if not build.group:
            return  # nothing interesting

        completed_builds = build.group.get_completed_builds()
        remaining_builds = build.group.get_remaining_builds()

        first_board = len(completed_builds) == 0
        if first_board:
            logger.debug('setting pending status for %s (0/%s)', build.commit_id, len(settings.BOARDS))

            yield self.set_pending(build, completed_builds, remaining_builds)

    @gen.coroutine
    def handle_build_end(self, build):
        logger.debug('handling %s end', build)

        completed_builds = build.group.get_completed_builds()
        remaining_builds = build.group.get_remaining_builds()
        failed_builds = build.group.get_failed_builds()

        last_board = not remaining_builds
        success = not failed_builds

        if last_board:
            if success:
                logger.debug('setting success status for %s (%s/%s)',
                             build.commit_id, len(settings.BOARDS), len(settings.BOARDS))

                yield self.set_success(build)

            else:
                logger.debug('setting failed status for %s: (%s/%s)',
                             build.commit_id, len(completed_builds), len(settings.BOARDS))

                yield self.set_failed(build, failed_builds)

        else:  # not the last build
            logger.debug('updating pending status for %s (%s/%s)',
                         build.commit_id, len(completed_builds), len(settings.BOARDS))

            yield self.set_pending(build, completed_builds, remaining_builds)

    def handle_release(self, commit_id, tag, branch, boards_image_files):
        today = datetime.date.today()
        tag = tag or utils.branches_format(settings.NIGHTLY_TAG, branch, today)
        name = utils.branches_format(settings.NIGHTLY_NAME, branch, today)

        release = yield self.create_release(commit_id, tag, branch, name)

        for board in settings.BOARDS:
            image_files = boards_image_files.get(board)
            if not image_files:
                logger.warning('no image files supplied for board %s', board)
                continue

            for fmt in settings.IMAGE_FILE_FORMATS:
                image_file = image_files.get(fmt)
                if image_file is None:
                    logger.warning('no image files supplied for board %s, format %s', board, fmt)
                    continue

                file_name = os.path.basename(image_file)
                with open(image_file, 'rb') as f:
                    body = f.read()

                logger.debug('uploading image file %s (%s bytes)', image_file, len(body))
                yield self.upload_release_file(release, board, file_name, fmt, body)
                logger.debug('image file %s uploaded', image_file)

    def _register_build(self, build):
        build.add_state_change_callback(functools.partial(self._on_build_state_change, build))

    def _on_build_state_change(self, build, state):
        if state == building.STATE_RUNNING:
            self.handle_build_begin(build)

        elif state == building.STATE_ENDED:
            self.handle_build_end(build)

    @gen.coroutine
    def set_pending(self, build, completed_builds, remaining_builds):
        raise NotImplementedError()

    @gen.coroutine
    def set_success(self, build):
        raise NotImplementedError()

    @gen.coroutine
    def set_failed(self, build, failed_builds):
        raise NotImplementedError()

    @gen.coroutine
    def create_release(self, commit_id, tag, branch, name):
        raise NotImplementedError()

    @gen.coroutine
    def upload_release_file(self, release, board, name, fmt, content):
        raise NotImplementedError()


def init():
    logger.debug('starting web server on port %s', settings.WEB_PORT)

    service = _SERVICE_CLASSES[settings.REPO_SERVICE]
    application = web.Application([
        ('/{}'.format(settings.REPO_SERVICE), service),
    ])

    application.listen(settings.WEB_PORT)


from thingosdci.reposervices import github
# from thingosdci.reposervices import bitbucket
#
_SERVICE_CLASSES['github'] = github.GitHub
# _SERVICE_CLASSES['bitbucket'] = bitbucket.BitBucket

