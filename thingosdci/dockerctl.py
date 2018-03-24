
import datetime
import functools
import hashlib
import logging
import os
import re
import shlex
import subprocess
import time

from tornado import gen
from tornado import ioloop

from thingosdci import cache
from thingosdci import settings


_BUILD_QUEUE_NAME = 'docker-build-queue'
_BUILD_KEYS_NAME = 'docker-build-keys'
_CONTAINER_NAME_PREFIX = 'thingosdci-{repo}-'

logger = logging.getLogger(__name__)

_busy = 0
_build_begin_handlers = []
_build_end_handlers = []
_build_cancel_handlers = []
_last_callback_id = 1
_callbacks = {}


class DockerException(Exception):
    pass


@gen.coroutine
def _run_loop():
    global _busy

    while True:
        build_key = cache.pop(_BUILD_QUEUE_NAME)
        if not build_key:  # empty queue
            yield gen.sleep(1)
            continue

        cache_key = _make_build_info_cache_key(build_key)

        build_info = cache.get(cache_key)
        if not build_info:
            logger.warning('cannot find cached build info for build id "%s"', build_key)
            yield gen.sleep(1)
            continue

        # wait for a free slot
        while _busy >= settings.DOCKER_MAX_PARALLEL:
            yield gen.sleep(1)

        logger.debug('starting build %s', build_info['build_key'])

        ssh_private_key_file = settings.DOCKER_COPY_SSH_PRIVATE_KEY
        if ssh_private_key_file is True:
            ssh_private_key_file = os.path.join(os.getenv('HOME'), '.ssh', 'id_rsa')

        name = _CONTAINER_NAME_PREFIX + hashlib.sha1(str(int(time.time() * 1000)).encode()).hexdigest()[:8]
        name = name.format(repo=re.sub('[^a-z0-9]', '-', settings.REPO, re.IGNORECASE))

        cmd = [
            'run', '-td', '--privileged',
            '--name', name,
            '-e', 'TB_REPO={}'.format(build_info['git_url']),
            '-e', 'TB_BOARD={}'.format(build_info['board']),
            '-e', 'TB_COMMIT={}'.format(build_info['commit']),
            '-e', 'TB_BRANCH={}'.format(build_info.get('branch', '') or ''),
            '-e', 'TB_VERSION={}'.format(build_info.get('version', '') or ''),
            '-e', 'TB_PR={}'.format(build_info.get('pr_no', '') or ''),
            '-e', 'TB_BUILD_CMD={}'.format(build_info['build_cmd'] or ''),
            '-v', '{}:/mnt/dl'.format(settings.DL_DIR),
            '-v', '{}:/mnt/ccache'.format(settings.CCACHE_DIR),
            '-v', '{}:/mnt/output'.format(settings.OUTPUT_DIR)
        ]

        if ssh_private_key_file:
            cmd.append('-v')
            cmd.append('{}:/root/.ssh/id_rsa'.format(ssh_private_key_file))

        cmd += [
            '--cap-add=SYS_ADMIN',
            '--cap-add=MKNOD',
            settings.DOCKER_IMAGE_NAME
        ]

        io_loop = ioloop.IOLoop.current()

        # notify listeners that build has begun
        for handler in _build_begin_handlers:
            io_loop.spawn_callback(functools.partial(handler, build_info))

        # run docker container
        try:
            container_id = _docker_run_container(cmd)
            status = 'running'
            logger.debug('build %s started with container id %s', build_info['build_key'], container_id)

            _busy += 1
            logger.debug('busy: %d', _busy)

        except Exception as e:
            logger.error('failed to run container: %s', e, exc_info=True)
            container_id = None
            status = 'error'

        # update build info
        build_info['container_id'] = container_id
        build_info['begin_time'] = time.time()
        build_info['status'] = status
        cache.set(cache_key, build_info)


@gen.coroutine
def _status_loop():
    global _busy

    io_loop = ioloop.IOLoop.current()

    while True:
        # fetch build info
        build_keys = set(cache.get(_BUILD_KEYS_NAME, []))
        build_info_list = []
        for build_key in build_keys:
            build_info = cache.get(_make_build_info_cache_key(build_key))
            if build_info:
                build_info_list.append(build_info)

        build_info_by_container_id = {bi['container_id']: bi for bi in build_info_list if bi['container_id']}

        try:
            containers = _docker_list_containers()

        except Exception as e:
            logger.error('failed to list docker containers: %s', e, exc_info=True)
            containers = []

        for container in containers:
            container_id = container['id']
            exit_code = container['exit_code']

            build_info = build_info_by_container_id.get(container_id)
            if build_info:
                build_key = build_info['build_key']

                if not container['running']:
                    build_info['end_time'] = time.time()
                    lifetime = build_info['end_time'] - build_info['begin_time']

                    if exit_code:
                        logger.error('build %s exited (lifetime=%ss, exit code %s)', build_key, lifetime, exit_code)

                    else:
                        logger.debug('build %s exited (lifetime=%ss, exit code %s)', build_key, lifetime, exit_code)

                    _busy -= 1

                    logger.debug('busy: %d', _busy)

                    cache.delete(_make_build_info_cache_key(build_key))
                    build_keys.remove(build_key)
                    cache.set(_BUILD_KEYS_NAME, list(build_keys))

                    image_files_by_fmt = {}
                    board = build_info['board']
                    p = os.path.join(settings.OUTPUT_DIR, board, '.image_files')
                    if not exit_code and os.path.exists(p) and not build_info['build_cmd']:
                        with open(p, 'r') as f:
                            image_files = f.readlines()

                        # raw image file name
                        image_files = [f.strip() for f in image_files]

                        # full path to image file
                        image_files = [os.path.join(settings.OUTPUT_DIR, board, 'images', f) for f in image_files]

                        # dictionarize by file format/extension
                        image_files_by_fmt = {}
                        for fmt in settings.IMAGE_FILE_FORMATS:
                            for f in image_files:
                                if f.endswith(fmt):
                                    image_files_by_fmt[fmt] = f

                    for handler in _build_end_handlers:
                        io_loop.spawn_callback(functools.partial(handler, build_info, exit_code, image_files_by_fmt))

                    if build_info['callback_id']:
                        callback = _callbacks.pop(build_info['callback_id'], None)
                        if callback:
                            io_loop.spawn_callback(functools.partial(callback, build_info, exit_code))

            else:
                logger.warning('no build info associated to container %s', container_id)

            # remove the container if it's not running anymore
            if not container['running']:
                logger.debug('removing container %s', container_id)
                try:
                    _docker_remove_container(container_id)

                except Exception as e:
                    logger.error('failed to remove container %s: %s', container_id, e, exc_info=True)

        yield gen.sleep(1)


@gen.coroutine
def _cleanup_loop():
    while True:
        # remove old containers

        try:
            containers = _docker_list_containers()

        except Exception as e:
            logger.error('failed to list docker containers: %s', e, exc_info=True)
            containers = []

        old_containers = [c for c in containers if c['age'] > settings.DOCKER_CONTAINER_MAX_AGE]

        for container in old_containers:
            container_id = container['id']
            age = container['age']

            if container['running']:
                logger.warning('container %s still running after %s seconds', container_id, age)
                logger.debug('killing container %s', container_id)

                try:
                    _docker_kill_container(container_id)

                except Exception as e:
                    logger.error('failed to kill container %s: %s', container_id, e, exc_info=True)

        # remove old logs

        for file in os.listdir(settings.BUILD_LOGS_DIR):
            path = os.path.join(settings.BUILD_LOGS_DIR, file)
            s = os.stat(path)
            age = time.time() - s.st_mtime
            if age > settings.DOCKER_LOGS_MAX_AGE:
                logger.debug('removing old log %s', path)
                try:
                    os.remove(path)

                except Exception as e:
                    logger.error('failed to remove old log %s: %s', path, e)

        yield gen.sleep(900)


def _make_build_log_path(container_id):
    return os.path.join(settings.BUILD_LOGS_DIR, 'build-{}.log'.format(container_id))


def _make_build_info_cache_key(build_key):
    return 'build/{}'.format(build_key)


def _docker_run_container(cmd):
    return _docker_cmd(cmd).strip()


def _docker_remove_container(container_id):
    # save log before removal
    log_path = _make_build_log_path(container_id)
    log = _docker_cmd(['logs', container_id])

    with open(log_path, 'w') as f:
        f.write(log)

    _docker_cmd(['container', 'rm', container_id])


def _docker_kill_container(container_id):
    _docker_cmd(['container', 'kill', container_id])


def _docker_list_containers():
    name_prefix = _CONTAINER_NAME_PREFIX.format(repo=re.sub('[^a-z0-9]', '-', settings.REPO, re.IGNORECASE))
    containers = []
    s = _docker_cmd(['container', 'ls', '-a', '--no-trunc', '--format',
                     '{{.ID}}|{{.Names}}|{{.CreatedAt}}|{{.Status}}'])

    lines = s.split('\n')
    lines = [l.strip() for l in lines if l.strip()]

    now = datetime.datetime.now()

    for line in lines:
        parts = line.split('|')

        container_id = parts[0]
        name = parts[1]
        created_at = parts[2]
        running = parts[3].startswith('Up')
        exit_code = None

        if not name.startswith(name_prefix):
            continue  # not ours

        if not running:  # determine exit code
            try:
                exit_code = int(_docker_cmd(['wait', container_id]).strip())

            except Exception as e:
                logger.error('failed to retrieve exit code of container %s: %s', container_id, e,
                             exc_info=True)

                exit_code = 1

        created_at = ' '.join(created_at.split()[:2])
        created_at = datetime.datetime.strptime(created_at, '%Y-%m-%d %H:%M:%S')

        containers.append({
            'id': container_id,
            'name': name,
            'created_at': created_at,
            'age': int((now - created_at).total_seconds()),
            'running': running,
            'exit_code': exit_code
        })

    return containers


def _docker_cmd(cmd):
    if isinstance(cmd, str):
        cmd = shlex.split(cmd)

    docker_base_cmd = shlex.split(settings.DOCKER_COMMAND)

    cmd = docker_base_cmd + cmd
    # logger.debug('executing "%s"', cmd)

    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, universal_newlines=True)
    stdout, stderr = p.communicate()

    if p.returncode:
        raise DockerException(stderr)

    return stdout


def schedule_build(build_key, service, repo, git_url, board, commit, version=None, pr_no=None, branch=None,
                   build_cmd=None, callback=None):

    global _last_callback_id

    io_loop = ioloop.IOLoop.current()
    cache_key = _make_build_info_cache_key(build_key)

    build_info = cache.get(cache_key)
    add_queue = True
    if build_info:
        status = build_info['status']
        if status == 'running':
            logger.debug('stopping previous build "%s"', build_key)

            try:
                _docker_kill_container(build_info['container_id'])
                _docker_remove_container(build_info['container_id'])

            except DockerException as e:
                logger.error('failed to stop previous build "%s": %s', build_key, e)

            for handler in _build_cancel_handlers:
                io_loop.spawn_callback(functools.partial(handler, build_info))

        elif status == 'pending':
            logger.debug('found pending previous build "%s"', build_key)
            add_queue = False

    else:
        build_keys = set(cache.get(_BUILD_KEYS_NAME, []))
        build_keys.add(build_key)
        cache.set(_BUILD_KEYS_NAME, list(build_keys))

    callback_id = None
    if callback:
        _last_callback_id += 1
        callback_id = _last_callback_id
        _callbacks[callback_id] = callback

    build_info = {
        'status': 'pending',
        'build_key': build_key,
        'service': service,
        'repo': repo,
        'git_url': git_url,
        'board': board,
        'version': version,
        'commit': commit,
        'pr_no': pr_no,
        'branch': branch,
        'build_cmd': build_cmd,
        'callback_id': callback_id,
        'container_id': None,
        'begin_time': None,
        'end_time': None
    }

    logger.debug('scheduling build "%s"', build_key)
    cache.set(cache_key, build_info)
    if add_queue:
        cache.push(_BUILD_QUEUE_NAME, build_key)


@gen.coroutine
def run_custom_build_cmd(build_key, service, repo, git_url, board, commit, build_cmd,
                         version=None, pr_no=None, branch=None):

    task = gen.Task(schedule_build, build_key, service, repo, git_url, board, commit,
                    version=version, pr_no=pr_no, branch=branch, build_cmd=build_cmd)

    result = yield task
    build_info, exit_code = result[0]

    if exit_code:
        container_id = build_info['container_id']
        build_log = get_build_log(container_id, last_lines=20)

        delimiter = '*' * 3
        logger.error('custom docker command "%s" failed:\n\n %s\n%s\n %s\n',
                     build_cmd, delimiter, build_log, delimiter)

        raise DockerException('custom docker command failed')


def get_build_info(build_key):
    return cache.get(_make_build_info_cache_key(build_key))


def get_build_log(container_id, last_lines=None):
    try:
        log = _docker_cmd(['logs', container_id])

    except DockerException:
        log_path = _make_build_log_path(container_id)
        if os.path.exists(log_path):
            with open(log_path) as f:
                log = f.read()

        else:
            log = ''

    if last_lines:
        partial_log = ''
        while last_lines > 0:
            last_lines -= 1
            p = log.rfind('\n')
            if p < 0:
                partial_log = log + partial_log
                log = ''

            else:
                partial_log = log[p:] + partial_log
                log = log[:p]

        return partial_log

    return log


def add_build_begin_handler(handler):
    _build_begin_handlers.append(handler)


def add_build_end_handler(handler):
    _build_end_handlers.append(handler)


def add_build_cancel_handler(handler):
    _build_cancel_handlers.append(handler)


def init():
    global _busy

    io_loop = ioloop.IOLoop.current()

    io_loop.spawn_callback(_run_loop)
    io_loop.spawn_callback(_status_loop)
    io_loop.spawn_callback(_cleanup_loop)

    # initialize busy counter from cache

    try:
        containers = _docker_list_containers()

    except Exception as e:
        logger.error('failed to list docker containers: %s', e, exc_info=True)
        raise

    build_keys = set(cache.get(_BUILD_KEYS_NAME, []))
    build_info_list = []
    for key in build_keys:
        build_info = cache.get(_make_build_info_cache_key(key))
        if build_info:
            build_info_list.append(build_info)

    build_info_by_container_id = {bi['container_id']: bi for bi in build_info_list if bi['container_id']}

    for container in containers:
        container_id = container['id']

        build_info = build_info_by_container_id.get(container_id)
        if build_info:
            _busy += 1

    if _busy:
        logger.debug('initial busy: %s', _busy)
