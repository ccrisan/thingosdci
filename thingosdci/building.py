
import datetime
import functools
import hashlib
import logging
import os
import time

from tornado import gen
from tornado import ioloop

from thingosdci import dockerctl
from thingosdci import loopdevmanager
from thingosdci import settings
from thingosdci import utils


logger = logging.getLogger(__name__)


STATE_PENDING = 'pending'
STATE_RUNNING = 'running'
STATE_ENDED = 'ended'

TYPE_PR = 'pr'
TYPE_NIGHTLY = 'nightly'
TYPE_TAG = 'tag'
TYPE_CUSTOM = 'custom'

_build_queue = []  # pending builds
_current_builds_by_board = {}  # a single build per board is allowed at a time
_current_build_group = None


class BuildException(Exception):
    pass


class Build:
    def __init__(self, repo_service, group, typ, board,
                 commit_id=None, tag=None, pr_no=None, branch=None, version=None,
                 custom_cmd=None, interactive=False, callback=None):

        self.repo_service = repo_service
        self.group = group
        self.type = typ
        self.board = board

        self.commit_id = commit_id
        self.tag = tag
        self.pr_no = pr_no
        self.branch = branch
        self.version = version
        self.custom_cmd = custom_cmd
        self.interactive = interactive

        self.container = None
        self.exit_code = None
        self.begin_time = None
        self.end_time = None
        self.image_files = None

        try:
            self.loop_dev = loopdevmanager.acquire_loop_dev()

        except loopdevmanager.LoopDevManagerException as e:
            logger.error('failed to acquire loop device for %s: %s', self, e)
            self.loop_dev = None

        self._callback = callback
        self._state_change_callbacks = []

        if group:
            group.add_build(self)

    def __str__(self):
        return 'build {}/{}/{}'.format(self.type, self.get_identifier(), self.board)

    def get_identifier(self):
        if self.type == TYPE_PR:
            return self.pr_no

        elif self.type == TYPE_NIGHTLY:
            return self.branch

        elif self.type == TYPE_TAG:
            return self.tag

        else:  # assuming TYPE_CUSTOM
            return 'cmd' + hashlib.sha1(self.custom_cmd.encode()).hexdigest()[:8]

    def get_key(self):
        return '{}/{}/{}'.format(self.repo_service, self.get_identifier(), self.board)

    def set_begin(self, container):
        if self.begin_time is not None:
            raise BuildException('cannot set begin time of build that has already begun')

        self.begin_time = time.time()
        self.container = container

        if container:
            logger.debug('%s has begun on %s', self, container)
            self.container.add_state_change_callback(self._on_container_state_change)

        self._run_state_change_callbacks()

    def set_end(self, exit_code):
        if self.begin_time is None:
            raise BuildException('cannot set end time of build that has not begun')

        if self.end_time is not None:
            raise BuildException('cannot set end time of build that has already ended')

        if self.loop_dev:
            try:
                loopdevmanager.release_loop_dev(self.loop_dev)

            except loopdevmanager.LoopDevManagerException as e:
                logger.error('failed to release loop device %s of %s: %s', self.loop_dev, self, e)

        self.exit_code = exit_code
        self.end_time = time.time()

        # gather image files
        if not self.custom_cmd and not exit_code:  # regular build
            p = os.path.join(settings.OUTPUT_DIR, self.board, '.image_files')
            if os.path.exists(p):
                with open(p, 'r') as f:
                    image_files = f.readlines()

                # simple image file name
                image_files = [f.strip() for f in image_files]

                # full path to image file
                image_files = [os.path.join(settings.OUTPUT_DIR, self.board, 'images', f) for f in image_files]

                # dictionarize by file format/extension
                image_files_by_fmt = {}
                for fmt in settings.IMAGE_FILE_FORMATS:
                    for f in image_files:
                        if f.endswith(fmt):
                            image_files_by_fmt[fmt] = f

                self.image_files = image_files_by_fmt

        lifetime = int(self.end_time - self.begin_time)
        how = ['successfully', 'with error'][bool(exit_code)]
        logger.debug('%s has ended %s (lifetime=%ss)', self, how, lifetime)

        if exit_code:
            # show last 20 lines of container log
            build_log = dockerctl.get_container_log(self.container.id, last_lines=20)

            delimiter = '*' * 3
            logger.error('build failed:\n\n %s\n\n%s\n %s\n', delimiter, build_log, delimiter)

        self._run_state_change_callbacks()

        if self._callback:
            io_loop = ioloop.IOLoop.current()
            io_loop.spawn_callback(self._callback, self)

        if _current_builds_by_board.get(self.board) is self:
            _current_builds_by_board.pop(self.board)

        else:
            logger.warning('%s was not the current build for board %s', self, self.board)

        logger.debug('%d running builds', len(_current_builds_by_board))

    def get_state(self):
        if self.begin_time is None:
            return STATE_PENDING

        if self.end_time is None:
            return STATE_RUNNING

        return STATE_ENDED

    def add_state_change_callback(self, callback):
        self._state_change_callbacks.append(callback)

    def _run_state_change_callbacks(self):
        io_loop = ioloop.IOLoop.current()
        state = self.get_state()

        for callback in self._state_change_callbacks:
            io_loop.spawn_callback(callback, state)

    def _on_container_state_change(self, state):
        if state == dockerctl.CONTAINER_STATE_EXITED:
            self.set_end(self.container.exit_code)


class BuildGroup:
    def __init__(self):
        self.builds = {}

        self._first_build_begun = False
        self._last_build_ended = False

        self._first_build_begin_callbacks = []
        self._last_build_end_callbacks = []

    def add_build(self, build):
        if build.board in self.builds:
            raise BuildException('board already present in build group')

        self.builds[build.board] = build
        build.add_state_change_callback(functools.partial(self._on_build_state_change, build))

    def get_completed_builds(self):
        return [build for build in self.builds.values() if build.get_state() == STATE_ENDED]

    def get_remaining_builds(self):
        return [build for build in self.builds.values() if build.get_state() in [STATE_RUNNING, STATE_PENDING]]

    def get_failed_builds(self):
        return [build for build in self.builds.values() if build.get_state() == STATE_ENDED and build.exit_code]

    def add_first_build_begin_callback(self, callback):
        self._first_build_begin_callbacks.append(callback)

    def add_last_build_end_callback(self, callback):
        self._last_build_end_callbacks.append(callback)

    def _handle_build_begin(self, build):
        if not self._first_build_begun:
            self._first_build_begun = True

            io_loop = ioloop.IOLoop.current()
            for handler in self._first_build_begin_callbacks:
                io_loop.spawn_callback(handler, build)

    def _handle_build_end(self, build):
        if not self.get_remaining_builds():
            if not self._last_build_ended:
                self._last_build_ended = True

                io_loop = ioloop.IOLoop.current()
                for handler in self._last_build_end_callbacks:
                    io_loop.spawn_callback(handler, build)

    def _on_build_state_change(self, build, state):
        if state == STATE_RUNNING:
            self._handle_build_begin(build)

        elif state == STATE_ENDED:
            self._handle_build_end(build)


def schedule_pr_build(repo_service, group, board, commit_id, pr_no):
    return _schedule_build(repo_service, group, TYPE_PR, board,
                           commit_id=commit_id, pr_no=pr_no)


def schedule_nightly_build(repo_service, group, board, commit_id, branch):
    version = utils.branches_format(settings.NIGHTLY_VERSION, branch, datetime.date.today())

    return _schedule_build(repo_service, group, TYPE_NIGHTLY, board,
                           commit_id=commit_id, branch=branch, version=version)


def schedule_tag_build(repo_service, group, board, commit_id, tag, version):
    return _schedule_build(repo_service, group, TYPE_TAG, board,
                           commit_id=commit_id, tag=tag, version=version)


@gen.coroutine
def run_custom_cmd(repo_service, custom_cmd, interactive=False, board='dummyboard'):
    future = gen.Future()

    _schedule_build(repo_service, None, TYPE_CUSTOM, board, custom_cmd=custom_cmd,
                    interactive=interactive, callback=future.set_result)

    build = yield future
    if build.exit_code:
        raise BuildException('custom build command failed')

    return build


def _schedule_build(repo_service, group, typ, board,
                    commit_id=None, tag=None, pr_no=None, branch=None, version=None,
                    custom_cmd=None, interactive=False, callback=None):

    build = Build(repo_service, group, typ, board,
                  commit_id=commit_id, tag=tag, pr_no=pr_no, branch=branch, version=version,
                  custom_cmd=custom_cmd, interactive=interactive, callback=callback)

    logger.debug('scheduling %s', build)

    # if a build with same key is pending, replace it
    index = -1
    for i, b in enumerate(_build_queue):
        if build.get_key() == b.get_key():
            logger.debug('replacing pending %s', b)
            index = i
            break

    if index >= 0:
        _build_queue.remove(_build_queue[index])

    _build_queue.append(build)

    logger.debug('%d queued builds', len(_build_queue))

    return build


@gen.coroutine
def _run_loop():
    global _current_build_group

    while True:
        yield gen.sleep(1)

        if not _build_queue:  # empty queue
            continue

        # wait for a free slot
        if len(_current_builds_by_board) >= settings.DOCKER_MAX_PARALLEL:
            continue

        # clear build group if no build is running
        if len(_current_builds_by_board) == 0:
            _current_build_group = None

        # treat the case where all queued builds correspond to currently building boards
        queued_boards = [b.board for b in _build_queue]
        if all((b in _current_builds_by_board for b in queued_boards)):
            logger.debug('all queued builds correspond to currently building boards, retrying later')
            yield gen.sleep(60)
            continue

        # treat the case where all queued builds correspond to another build group
        queued_groups = set((b.group for b in _build_queue))
        if _current_build_group and all((g is not _current_build_group for g in queued_groups)):
            logger.debug('all queued builds correspond to another build group, retrying later')
            yield gen.sleep(60)
            continue

        build = _build_queue.pop(0)

        logger.debug('dequeued %s (%d remaining queued builds)', build, len(_build_queue))

        if build.board in _current_builds_by_board:
            logger.debug('another build for board %s is currently running, pushing %s back', build.board, build)
            _build_queue.append(build)
            continue

        if _current_build_group and build.group is not _current_build_group:
            logger.debug('%s belongs to another build group, pushing back', build)
            _build_queue.append(build)
            continue

        _current_builds_by_board[build.board] = build
        _current_build_group = build.group

        logger.debug('starting %s (%d running builds)', build, len(_current_builds_by_board))

        # workaround for when running docker over ssh
        custom_cmd = build.custom_cmd or ''
        if not settings.DOCKER_COMMAND.startswith('docker'):
            custom_cmd = '"' + custom_cmd + '"'

        clone_args = ''
        if settings.GIT_CLONE_DEPTH > 0:
            clone_args += '--no-single-branch --depth {}'.format(settings.GIT_CLONE_DEPTH)

        env = {
            'TB_REPO': settings.GIT_URL,
            'TB_GIT_CLONE_ARGS': clone_args,
            'TB_BOARD': build.board,
            'TB_COMMIT': build.commit_id or '',
            'TB_TAG': build.tag or '',
            'TB_PR': build.pr_no or '',
            'TB_BRANCH': build.branch or '',
            'TB_VERSION': build.version or '',
            'TB_CUSTOM_CMD': custom_cmd,
            'TB_CLEAN_TARGET_ONLY': str(settings.CLEAN_TARGET_ONLY).lower(),
            'TB_LOOP_DEV': build.loop_dev or ''
        }

        vol = {
            settings.DL_DIR: '/mnt/dl',
            settings.CCACHE_DIR: '/mnt/ccache',
            settings.OUTPUT_DIR: '/mnt/output'
        }

        try:
            container = dockerctl.run_container(env, vol, build.interactive)

        except dockerctl.DockerException as e:
            logger.error('failed to start build: %s', e, exc_info=True)
            continue

        build.set_begin(container)
        if not container:  # container is None when running an interactive command
            build.set_end(0)


def init():
    io_loop = ioloop.IOLoop.current()
    io_loop.spawn_callback(_run_loop)
