
import datetime
import functools
import logging
import os
import re

from tornado import gen
from tornado import ioloop
from tornado import web

from thingosdci import building
from thingosdci import dockerctl
from thingosdci import persist
from thingosdci import settings
from thingosdci import utils


_SERVICE_CLASSES = {}
_service = None

logger = logging.getLogger(__name__)


class RepoServiceRequestHandler(web.RequestHandler):
    def __init__(self, service, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.service = service

    def get(self):
        self.set_header('Content-Type', 'text/plain')
        lines = self.get_argument('lines', None)

        if lines:
            try:
                lines = int(lines)

            except ValueError:
                lines = 1

        self.finish(dockerctl.get_container_log(self.get_argument('id'), lines))


class RepoService:
    DEF_LOG_LINES = 100
    REQUEST_HANDLER_CLASS = RepoServiceRequestHandler

    def __init__(self):
        # used for nightly builds at fixed hour
        self._last_commit_by_branch = persist.load('last-commit-by-branch', {})
        self._last_nightly_commit_by_branch = persist.load('last-nightly-commit-by-branch', {})

    def __str__(self):
        return 'reposervice'

    def make_log_url(self, build):
        return settings.WEB_BASE_URL + '/{}?id={}&lines={}'.format(self, build.container.id, self.DEF_LOG_LINES)

    def schedule_nightly_builds_for_new_commits(self):
        for branch in settings.NIGHTLY_BRANCHES:
            last_commit = self._last_commit_by_branch.get(branch)
            last_nightly_commit = self._last_nightly_commit_by_branch.get(branch)

            if last_commit and last_commit != last_nightly_commit:
                logger.debug('new commit found on branch %s', branch)
                self._schedule_nightly_build(last_commit, branch)

    def handle_pull_request_open(self, commit_id, src_repo, dst_repo, pr_no):
        logger.debug('pull request %s opened: %s -> %s (%s)', pr_no, src_repo, dst_repo, commit_id)
        if not settings.PULL_REQUESTS:
            logger.debug('pull requests ignored')
            return

        build_group = building.BuildGroup()

        for board in settings.BOARDS:
            build = building.schedule_pr_build(self, build_group, board, commit_id, pr_no)
            self._register_build(build)

        self._register_build_group(build_group)

    def handle_pull_request_update(self, commit_id, src_repo, dst_repo, pr_no):
        logger.debug('pull request %s updated: %s -> %s (%s)', pr_no, src_repo, dst_repo, commit_id)
        if not settings.PULL_REQUESTS:
            logger.debug('pull requests ignored')
            return

        build_group = building.BuildGroup()

        for board in settings.BOARDS:
            build = building.schedule_pr_build(self, build_group, board, commit_id, pr_no)
            self._register_build(build)

        self._register_build_group(build_group)

    def handle_commit(self, commit_id, branch):
        logger.debug('commit to %s (%s)', branch, commit_id)

        self._last_commit_by_branch[branch] = commit_id
        self._save_commit_info()

        if branch not in settings.NIGHTLY_BRANCHES:
            logger.debug('branch %s ignored', branch)
            return

        if settings.NIGHTLY_FIXED_HOUR is None:  # schedule build right away
            self._schedule_nightly_build(commit_id, branch)

        # else, fixed_hour_loop() will take care of it

    def handle_new_tag(self, commit_id, tag):
        logger.debug('new tag: %s (%s)', tag, commit_id)

        if not settings.RELEASE_TAG_REGEX or not re.match(settings.RELEASE_TAG_REGEX, tag):
            logger.debug('tag %s ignored', tag)
            return

        build_group = building.BuildGroup()

        for board in settings.BOARDS:
            build = building.schedule_tag_build(self, build_group, board, commit_id, tag)
            self._register_build(build)

        self._register_build_group(build_group)

    @gen.coroutine
    def handle_build_begin(self, build):
        logger.debug('handling %s begin', build)

    @gen.coroutine
    def handle_first_build_begin(self, build):
        logger.debug('handling first %s begin', build)

        if not build.group:
            return  # unlikely

        completed_builds = build.group.get_completed_builds()
        remaining_builds = build.group.get_remaining_builds()

        first_board = len(completed_builds) == 0
        if first_board:
            logger.debug('setting pending status for %s (0/%s)', build.commit_id, len(settings.BOARDS))

            yield self.set_pending(build, completed_builds, remaining_builds)
            logger.debug('status set')

    @gen.coroutine
    def handle_last_build_end(self, build):
        logger.debug('handling last %s end', build)

        completed_builds = build.group.get_completed_builds()
        failed_builds = build.group.get_failed_builds()

        if not failed_builds:
            logger.debug('setting success status for %s (%s/%s)',
                         build.commit_id, len(settings.BOARDS), len(settings.BOARDS))

            yield self.set_success(build)

            logger.debug('status set')

            group = build.group
            boards_image_files = {b.board: b.image_files for b in group.builds.values()}

            if build.type in [building.TYPE_NIGHTLY, building.TYPE_TAG]:
                yield self.handle_release(build.commit_id, build.tag, build.branch, boards_image_files, build.type)

        else:
            logger.debug('setting failed status for %s: (%s/%s)',
                         build.commit_id, len(completed_builds), len(settings.BOARDS))

            yield self.set_failed(build, failed_builds)
            logger.debug('status set')

    @gen.coroutine
    def handle_build_end(self, build):
        logger.debug('handling %s end', build)

        completed_builds = build.group.get_completed_builds()
        remaining_builds = build.group.get_remaining_builds()

        if not remaining_builds:
            return  # last build end

        logger.debug('updating pending status for %s (%s/%s)',
                     build.commit_id, len(completed_builds), len(settings.BOARDS))

        yield self.set_pending(build, completed_builds, remaining_builds)
        logger.debug('status set')

    @gen.coroutine
    def handle_release(self, commit_id, tag, branch, boards_image_files, build_type):
        logger.debug('handling release on commit=%s, tag=%s, branch=%s', commit_id, tag, branch)

        today = datetime.date.today()
        tag = tag or utils.branches_format(settings.NIGHTLY_TAG, branch, today)
        name = tag or utils.branches_format(settings.NIGHTLY_NAME, branch, today)

        release = yield self.create_release(commit_id, tag, name, build_type)

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

        logger.debug('release on commit=%s, tag=%s, branch=%s completed', commit_id, tag, branch)

    def _register_build(self, build):
        build.add_state_change_callback(functools.partial(self._on_build_state_change, build))

    def _register_build_group(self, group):
        group.add_first_build_begin_callback(self.handle_first_build_begin)
        group.add_last_build_end_callback(self.handle_last_build_end)

    def _on_build_state_change(self, build, state):
        if state == building.STATE_RUNNING:
            self.handle_build_begin(build)

        elif state == building.STATE_ENDED:
            self.handle_build_end(build)

    def _schedule_nightly_build(self, commit_id, branch):
        build_group = building.BuildGroup()

        for board in settings.BOARDS:
            build = building.schedule_nightly_build(self, build_group, board, commit_id, branch)
            self._register_build(build)

        self._register_build_group(build_group)

        self._last_nightly_commit_by_branch[branch] = commit_id
        self._save_commit_info()

    def _save_commit_info(self):
        persist.save('last-commit-by-branch', self._last_commit_by_branch)
        persist.save('last-nightly-commit-by-branch', self._last_nightly_commit_by_branch)

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
    def create_release(self, commit_id, tag, name, build_type):
        raise NotImplementedError()

    @gen.coroutine
    def upload_release_file(self, release, board, name, fmt, content):
        raise NotImplementedError()


def get_service():
    global _service

    if _service is None:
        logger.debug('creating repo service')
        service_class = _SERVICE_CLASSES[settings.REPO_SERVICE]
        _service = service_class()

    return _service


@gen.coroutine
def _fixed_hour_loop():
    last_run_day = 0
    while True:
        yield gen.sleep(60)

        day = datetime.datetime.now().day
        if day == last_run_day:  # prevents running more than once in a day
            continue

        if datetime.time().hour != settings.NIGHTLY_FIXED_HOUR:
            continue

        last_run_day = day

        logger.debug('running fixed hour nightly build check')
        _check_run_fixed_hour_task()


def _check_run_fixed_hour_task():
    service = get_service()
    service.schedule_nightly_builds_for_new_commits()


def init():
    from thingosdci.reposervices import github
    from thingosdci.reposervices import bitbucket

    _SERVICE_CLASSES['github'] = github.GitHub
    _SERVICE_CLASSES['bitbucket'] = bitbucket.BitBucket

    logger.debug('starting web server on port %s', settings.WEB_PORT)

    service_class = _SERVICE_CLASSES[settings.REPO_SERVICE]
    service = get_service()

    application = web.Application([
        ('/{}'.format(settings.REPO_SERVICE), service_class.REQUEST_HANDLER_CLASS, {'service': service}),
    ])

    application.listen(settings.WEB_PORT)

    if settings.NIGHTLY_FIXED_HOUR:
        ioloop.IOLoop.current().spawn_callback(_fixed_hour_loop)
