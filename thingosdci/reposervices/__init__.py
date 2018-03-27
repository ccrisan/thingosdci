
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

        completed_boards = build.group.get_completed_boards()
        first_board = len(completed_boards == 1)
        if first_board:
            logger.debug('setting pending status for %s/%s (0/%s)',settings.REPO, build.commit, len(settings.BOARDS))

            yield self.set_pending(build, completed_boards)

    @gen.coroutine
    def handle_build_end(self, build):
        if build_info['service'] != 'github':
            return  # not ours

        if not build_info['build_key'].endswith('/{}'.format(build_info['board'])):
            return  # not an OS image build

        logger.debug('build end: %s', build_info['build_key'])

        tag_branch_pr = build_info['build_key'].split('/')[3]

        commit = build_info['commit']
        board = build_info['board']

        boards_key = _make_build_boards_key(commit)
        boards = cache.get(boards_key, [])

        boards_image_files_key = _make_build_boards_image_files_key(commit)
        boards_image_files = cache.get(boards_image_files_key, {})

        boards_exit_codes_key = _make_build_boards_exit_codes_key(commit)
        boards_exit_codes = cache.get(boards_exit_codes_key, {})

        try:
            boards.remove(board)

        except ValueError:
            logger.warning('board %s not found in pending boards list', board)

        cache.set(boards_key, boards)

        boards_image_files[board] = image_files
        cache.set(boards_image_files_key, boards_image_files)

        boards_exit_codes[board] = exit_code
        cache.set(boards_exit_codes_key, boards_exit_codes)

        last_board = len(boards_exit_codes) == len(settings.BOARDS)
        if last_board:

            # image_files_by_fmt = {}
            # board = build_info['board']
            # p = os.path.join(settings.OUTPUT_DIR, board, '.image_files')
            # if not exit_code and os.path.exists(p) and not build_info['custom_cmd']:
            #     with open(p, 'r') as f:
            #         image_files = f.readlines()
            #
            #     # raw image file name
            #     image_files = [f.strip() for f in image_files]
            #
            #     # full path to image file
            #     image_files = [os.path.join(settings.OUTPUT_DIR, board, 'images', f) for f in image_files]
            #
            #     # dictionarize by file format/extension
            #     image_files_by_fmt = {}
            #     for fmt in settings.IMAGE_FILE_FORMATS:
            #         for f in image_files:
            #             if f.endswith(fmt):
            #                 image_files_by_fmt[fmt] = f

            cache.delete(boards_key)

            failed_boards = [b for b, e in boards_exit_codes.items() if e]
            success = len(failed_boards) == 0

            target_url = _make_target_url(build_info)
            if failed_boards:
                failed_build_info = get_build_info_by_board(tag_branch_pr).get(failed_boards[0])
                if failed_build_info:
                    target_url = _make_target_url(failed_build_info)

            status = ['error', 'success'][success]
            failed_boards_str = ', '.join(failed_boards)
            description = ['failed to build OS images: {}'.format(failed_boards_str),
                'OS images successfully built ({}/{})'.format(len(boards_exit_codes),
                                                              len(settings.BOARDS))][success]

            logger.debug('setting %s status for %s/%s (%s/%s)',
                         status, settings.REPO, commit, len(boards_exit_codes), len(settings.BOARDS))

            yield set_status(commit, status, target_url=target_url, description=description, context=_STATUS_CONTEXT)

            branch = build_info.get('branch')
            if branch and success:
                version = build_info.get('version', branch)
                yield upload_branch_build(branch, commit, version, boards_image_files)

        else:
            # simply update status so that the log of a currently building process is set

            logger.debug('setting pending status for %s/%s (%s/%s)',
                         settings.REPO, commit, len(boards_exit_codes), len(settings.BOARDS))

            build_info_list = get_build_info_by_board(tag_branch_pr).values()
            running_build_info_list = [bi for bi in build_info_list if bi['status'] == 'running']
            if not running_build_info_list:
                logger.debug('no more running processes for %s/%s', settings.REPO, tag_branch_pr)
                return

            target_url = _make_target_url(running_build_info_list[0])  # just pick the first one

            yield set_status(commit,
                             status='pending',
                             target_url=target_url,
                             description='building OS images ({}/{})'.format(len(boards_exit_codes),
                                                                             len(settings.BOARDS)),
                             context=_STATUS_CONTEXT)

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
        build.add_state_change_callback(functools.partial(build))

    def _on_build_state_change(self, build, state):
        if state == building.STATE_RUNNING:
            self.handle_build_begin(build)

        elif state == building.STATE_ENDED:
            self.handle_build_end(build)

    @gen.coroutine
    def set_pending(self, build, completed_boards):
        raise NotImplementedError()

    @gen.coroutine
    def set_success(self, build, completed_boards):
        raise NotImplementedError()

    @gen.coroutine
    def set_failed(self, build, failed_boards):
        raise NotImplementedError()

    @gen.coroutine
    def create_release(self, commit_id, tag, branch, name):
        raise NotImplementedError()

    @gen.coroutine
    def upload_release_file(self, release, board, name, fmt, content):
        raise NotImplementedError()


def get_service():
    global _service

    if _service is None:
        logger.debug('creating repo service %s', settings.REPO_SERVICE)
        cls = _SERVICE_CLASSES[settings.REPO_SERVICE]
        _service = cls()

    return _service


def init():
    logger.debug('starting event server')

    application = web.Application([
        ('/{}'.format(settings.REPO_SERVICE), get_service()),
    ])

    application.listen(settings.WEB_PORT)


from thingosdci.reposervices import github
# from thingosdci.reposervices import bitbucket
#
_SERVICE_CLASSES['github'] = github.GitHub
# _SERVICE_CLASSES['bitbucket'] = bitbucket.BitBucket

