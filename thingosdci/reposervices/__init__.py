
import datetime
import functools
import logging
import os
import re
import subprocess

from tornado import gen
from tornado import ioloop
from tornado import web

from thingosdci import building
from thingosdci import dockerctl
from thingosdci import persist
from thingosdci import s3client
from thingosdci import settings
from thingosdci import utils
from thingosdci.reposervices import trigger


_SERVICE_CLASSES = {}
_service = None

logger = logging.getLogger(__name__)


class RepoServiceRequestHandler(web.RequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def initialize(self, service):
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
        self._commit_ids_by_tag = persist.load('commit-ids-by-tag', {})

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
                self.schedule_nightly_build(last_commit, branch)

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
            self.schedule_nightly_build(commit_id, branch)

        # else, fixed_hour_loop() will take care of it

    def handle_new_tag(self, commit_id, tag):
        if commit_id is None:
            commit_id = self._commit_ids_by_tag.get(tag)

        else:
            self._commit_ids_by_tag[tag] = commit_id
            self._save_commit_info()

        logger.debug('new tag: %s (%s)', tag, commit_id)

        if not settings.RELEASE_TAG_REGEX or not re.match(settings.RELEASE_TAG_REGEX, tag):
            logger.debug('release: tag %s ignored', tag)
            return

        version = self._prepare_version(tag)

        build_group = building.BuildGroup()

        for board in settings.BOARDS:
            build = building.schedule_tag_build(self, build_group, board, commit_id, tag, version)
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

            yield self._set_pending(build, completed_builds, remaining_builds)
            logger.debug('status set')

    @gen.coroutine
    def handle_last_build_end(self, build):
        logger.debug('handling last %s end', build)

        completed_builds = build.group.get_completed_builds()
        failed_builds = build.group.get_failed_builds()

        if not failed_builds:
            logger.debug('setting success status for %s (%s/%s)',
                         build.commit_id, len(settings.BOARDS), len(settings.BOARDS))

            yield self._set_success(build)

            logger.debug('status set')

            group = build.group
            boards_image_files = {b.board: b.image_files for b in group.builds.values()}

            if build.type in [building.TYPE_NIGHTLY, building.TYPE_TAG]:
                yield self.handle_release(build.commit_id, build.tag, build.version,
                                          build.branch, boards_image_files, build.type)

        else:
            logger.debug('setting failed status for %s: (%s/%s)',
                         build.commit_id, len(completed_builds), len(settings.BOARDS))

            yield self._set_failed(build, failed_builds)
            logger.debug('status set')

    @gen.coroutine
    def handle_build_end(self, build):
        logger.debug('handling %s end', build)

        completed_builds = build.group.get_completed_builds()
        remaining_builds = build.group.get_remaining_builds()

        if not remaining_builds:
            return  # last build end

        logger.debug('setting pending status for %s (%s/%s)',
                     build.commit_id, len(completed_builds), len(settings.BOARDS))

        yield self._set_pending(build, completed_builds, remaining_builds)
        logger.debug('status set')

    @gen.coroutine
    def handle_release(self, commit_id, tag, version, branch, boards_image_files, build_type):
        if tag and (not settings.RELEASE_TAG_REGEX or not re.match(settings.RELEASE_TAG_REGEX, tag)):
            logger.debug('release: tag %s ignored', tag)
            return

        logger.debug('handling release on commit=%s, tag=%s, version=%s, branch=%s', commit_id, tag, version, branch)

        today = datetime.date.today()
        tag = tag or utils.branches_format(settings.NIGHTLY_TAG, branch, today)

        release = yield self.create_release(commit_id, tag, version, branch, build_type)

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

                if build_type in settings.UPLOAD_SERVICE_BUILD_TYPES:
                    logger.debug('uploading image file %s (%s bytes)', image_file, len(body))
                    yield self.upload_release_file(release, board, tag, version, file_name, fmt, body)
                    logger.debug('image file %s uploaded', image_file)

                if build_type in settings.S3_UPLOAD_BUILD_TYPES:
                    logger.debug('uploading image file %s to S3 (%s bytes)', image_file, len(body))
                    s3_url = yield self._upload_release_file_s3(release, board, tag, version, file_name, fmt, body)
                    logger.debug('image file %s uploaded to S3', image_file)

                    if settings.S3_UPLOAD_ADD_RELEASE_LINK:
                        yield self.add_s3_release_link(release, board, tag, version, file_name, fmt, s3_url)

                if settings.RELEASE_SCRIPT:
                    self._call_release_script(image_file, board, fmt, build_type)

        logger.debug('release on commit=%s, tag=%s, version=%s, branch=%s completed', commit_id, tag, version, branch)

    def schedule_nightly_build(self, commit_id, branch):
        if commit_id is None:
            commit_id = self._last_commit_by_branch[branch]

        build_group = building.BuildGroup()

        for board in settings.BOARDS:
            build = building.schedule_nightly_build(self, build_group, board, commit_id, branch)
            self._register_build(build)

        self._register_build_group(build_group)

        self._last_nightly_commit_by_branch[branch] = commit_id
        self._save_commit_info()

    def _prepare_version(self, tag):
        if not settings.RELEASE_TAG_REGEX:
            return tag

        m = re.match(settings.RELEASE_TAG_REGEX, tag)
        if not m:
            return tag

        if len(m.groups()) < 2:
            return tag

        return m.group(1)

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

    def _save_commit_info(self):
        persist.save('last-commit-by-branch', self._last_commit_by_branch)
        persist.save('last-nightly-commit-by-branch', self._last_nightly_commit_by_branch)
        persist.save('commit-ids-by-tag', self._commit_ids_by_tag)

    @gen.coroutine
    def _set_pending(self, build, completed_builds, remaining_builds):
        running_remaining_builds = [b for b in remaining_builds if b.get_state() == building.STATE_RUNNING]
        if running_remaining_builds:
            running_build = running_remaining_builds[0]

        else:
            running_build = build

        url = self.make_log_url(running_build)
        description = 'building OS images ({}/{})'.format(len(completed_builds), len(settings.BOARDS))

        yield self.set_pending(build, url, description)

    @gen.coroutine
    def _set_success(self, build):
        url = self.make_log_url(build)
        description = 'OS images successfully built ({}/{})'.format(len(settings.BOARDS), len(settings.BOARDS))

        yield self.set_success(build, url, description)

    @gen.coroutine
    def _set_failed(self, build, failed_builds):
        if not failed_builds:
            logger.warning('cannot set failed status with no failed builds')
            return

        url = self.make_log_url(failed_builds[0])
        failed_boards_str = ', '.join([b.board for b in failed_builds])
        description = 'failed to build some OS images: {}'.format(failed_boards_str)

        yield self.set_failed(build, url, description)

    @gen.coroutine
    def _upload_release_file_s3(self, release, board, tag, version, name, fmt, content):
        # final URL should be in the following form:
        #    https://s3.amazonaws.com/{bucket}/{path}/{version}/{name}

        client = s3client.S3Client(access_key=settings.S3_UPLOAD_ACCESS_KEY,
                                   secret_key=settings.S3_UPLOAD_SECRET_KEY,
                                   bucket=settings.S3_UPLOAD_BUCKET)

        if settings.S3_UPLOAD_FILENAME_MAP:
            name = settings.S3_UPLOAD_FILENAME_MAP(name)

        path = '{path}/{version}/{name}'.format(path=settings.S3_UPLOAD_PATH,
                                                version=version,
                                                name=name)

        yield client.upload(path, content, headers={'X-Amz-Storage-Class': settings.S3_UPLOAD_STORAGE_CLASS})

        return 'https://s3.amazonaws.com/{bucket}/{path}/{version}/{name}'.format(bucket=settings.S3_UPLOAD_BUCKET,
                                                                                  path=settings.S3_UPLOAD_PATH,
                                                                                  version=version,
                                                                                  name=name)

    def _call_release_script(self, image_file, board, fmt, build_type):
        logger.debug('calling release script "%s" with "%s" "%s" "%s" "%s"',
                     settings.RELEASE_SCRIPT, image_file, board, fmt, build_type)
        cmd = [settings.RELEASE_SCRIPT, image_file, board, fmt, build_type]
        try:
            output = subprocess.check_output(cmd, stderr=subprocess.STDOUT)
            logger.debug('release script output: \n%s', output)
        except subprocess.CalledProcessError as e:
            logger.error('release script call failed')
            logger.error('release script output: \n%s', e.output)

    def set_pending(self, build, url, description):
        raise NotImplementedError()

    @gen.coroutine
    def set_success(self, build, url, description):
        raise NotImplementedError()

    @gen.coroutine
    def set_failed(self, build, url, description):
        raise NotImplementedError()

    @gen.coroutine
    def create_release(self, commit_id, tag, version, branch, build_type):
        raise NotImplementedError()

    @gen.coroutine
    def upload_release_file(self, release, board, tag, version, name, fmt, content):
        raise NotImplementedError()

    @gen.coroutine
    def add_s3_release_link(self, release, board, tag, version, name, fmt, s3_url):
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

        if datetime.datetime.now().hour != settings.NIGHTLY_FIXED_HOUR:
            continue

        last_run_day = day

        logger.debug('running fixed hour nightly build check')
        _check_run_fixed_hour_task()


def _check_run_fixed_hour_task():
    service = get_service()
    service.schedule_nightly_builds_for_new_commits()


def init():
    from thingosdci.reposervices import github
    from thingosdci.reposervices import gitlab
    from thingosdci.reposervices import bitbucket

    _SERVICE_CLASSES['github'] = github.GitHub
    _SERVICE_CLASSES['gitlab'] = gitlab.GitLab
    _SERVICE_CLASSES['bitbucket'] = bitbucket.BitBucket

    logger.debug('starting web server on port %s', settings.WEB_PORT)

    service_class = _SERVICE_CLASSES[settings.REPO_SERVICE]
    service = get_service()

    application = web.Application([
        ('/{}'.format(settings.REPO_SERVICE), service_class.REQUEST_HANDLER_CLASS, {'service': service}),
        ('/trigger', trigger.TriggerRequestHandler, {'service': service}),
    ])

    application.listen(settings.WEB_PORT)

    if settings.NIGHTLY_FIXED_HOUR is not None:
        ioloop.IOLoop.current().spawn_callback(_fixed_hour_loop)
