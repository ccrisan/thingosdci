
import datetime
import hashlib
import logging
import time

from tornado import gen
from tornado import ioloop

from thingosdci import dockerctl
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


class BuildException(Exception):
    pass


class Build:
    def __init__(self, repo_service, typ, board,
                 commit_id=None, tag=None, pr_no=None, branch=None, version=None,
                 custom_cmd=None, callback=None):

        self.type = typ
        self.repo_service = repo_service
        self.board = board

        self.commit_id = commit_id
        self.tag = tag
        self.pr_no = pr_no
        self.branch = branch
        self.version = version
        self.custom_cmd = custom_cmd

        self.container = None
        self.exit_code = None
        self.begin_time = None
        self.end_time = None

        self._callback = callback
        self._state_change_callbacks = []

        self.logger = logging.getLogger('build.{}.{}.{}.'.format(repo_service, typ, board))

    def __str__(self):
        return 'build {}/{}/{}'.format(self.repo_service, self.type, self.board)

    def get_key(self):
        if self.type == TYPE_PR:
            identifier = self.pr_no

        elif self.type == TYPE_NIGHTLY:
            identifier = self.branch

        elif self.type == TYPE_TAG:
            identifier = self.tag

        else:  # assuming TYPE_CUSTOM
            identifier = hashlib.sha1(self.custom_cmd).hexdigest()[:8]

        return '{}/{}/{}'.format(self.repo_service, identifier, self.board)

    def set_begin(self, container):
        if self.begin_time is not None:
            raise BuildException('cannot set begin time of build that has already begun')

        self.begin_time = time.time()
        self.container = container
        self.logger.debug('%s has begun', self)

        self.container.add_state_change_callback(self._on_container_state_change)

        self._run_state_change_callbacks()

    def set_end(self, exit_code):
        if self.begin_time is None:
            raise BuildException('cannot set end time of build that has not begun')

        if self.end_time is None:
            raise BuildException('cannot set end time of build that has already ended')

        self.exit_code = exit_code
        self.end_time = time.time()

        lifetime = self.end_time - self.begin_time
        how = ['successfully', 'with error'][exit_code]
        self.logger.debug('%s has ended %s (lifetime=%ss)', self, how, lifetime)

        logger.debug('%d running builds', len(_current_builds_by_board))

        self._run_state_change_callbacks()

        if self._callback:
            io_loop = ioloop.IOLoop.current()
            io_loop.spawn_callback(self._callback, self)

        if _current_builds_by_board.get(self.board) is self:
            _current_builds_by_board.pop(self.board)

        else:
            self.logger.warning('%s was not the current build for board %s', self, self.board)

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


def schedule_pr_build(repo_service, board, pr_no):
    return _schedule_build(repo_service, TYPE_PR, board, pr_no=pr_no)


def schedule_nightly_build(repo_service, board, commit_id, branch):
    version = utils.branches_format(settings.NIGHTLY_VERSION, branch, datetime.date.today())

    return _schedule_build(repo_service, TYPE_NIGHTLY, board, commit_id=commit_id, branch=branch, version=version)


def schedule_tag_build(repo_service, board, tag):
    return _schedule_build(repo_service, TYPE_TAG, board, tag=tag, version=tag)


@gen.coroutine
def run_custom_cmd(repo_service, custom_cmd):
    task = gen.Task(_schedule_build, repo_service, TYPE_CUSTOM, 'custom', custom_cmd=custom_cmd)

    result = yield task
    build = result[0]

    if build.exit_code:
        # show last 20 lines of container log
        build_log = dockerctl.get_contaniner_log(build.container.id, last_lines=20)

        delimiter = '*' * 3
        logger.error('custom build command "%s" failed:\n\n %s\n%s\n %s\n',
                     custom_cmd, delimiter, build_log, delimiter)

        raise BuildException('custom build command failed')


def _schedule_build(repo_service, typ, board,
                    commit_id=None, tag=None, pr_no=None, branch=None, version=None,
                    custom_cmd=None, callback=None):

    build = Build(repo_service, typ, board,
                  commit_id=commit_id, tag=tag, pr_no=pr_no, branch=branch, version=version,
                  custom_cmd=custom_cmd, callback=callback)

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
    while True:
        yield gen.sleep(1)

        if not _build_queue:  # empty queue
            continue

        # wait for a free slot
        if len(_current_builds_by_board) >= settings.DOCKER_MAX_PARALLEL:
            continue

        build = _build_queue.pop(0)

        logger.debug('dequeued %s (%d remaining queued builds)', build, len(_build_queue))

        if build.board in _current_builds_by_board:
            logger.debug('a build for board %s is already in progress, pusing %s back', build.board, build)
            _build_queue.append(build)
            continue

        _current_builds_by_board[build.board] = build

        logger.debug('starting %s (%d running builds)', build, len(_current_builds_by_board))

        env = {
            'TB_REPO': settings.REPO,
            'TB_BOARD': build.board,
            'TB_COMMIT': build.commit_id or '',
            'TB_TAG': build.tag or '',
            'TB_PR': build.pr_no or '',
            'TB_BRANCH': build.branch or '',
            'TB_VERSION': build.version or '',
            'TB_CUSTOM_CMD': build.custom_cmd or ''
        }

        vol = {
            settings.DL_DIR: '/mnt/dl',
            settings.CCACHE_DIR: '/mnt/ccache',
            settings.OUTPUT_DIR: '/mnt/output'
        }

        try:
            container = dockerctl.run_container(env, vol)

        except dockerctl.DockerException as e:
            logger.error('failed to start build: %s', e, exc_info=True)
            continue

        build.set_begin(container)


def init():
    io_loop = ioloop.IOLoop.current()
    io_loop.spawn_callback(_run_loop)
