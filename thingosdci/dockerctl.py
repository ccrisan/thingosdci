
import functools
import logging
import re
import shlex
import subprocess

from tornado import gen
from tornado import ioloop

from thingosdci import cache
from thingosdci import settings


_BUILD_QUEUE_NAME = 'docker-build-queue'
_BUILD_IDS_NAME = 'docker-build-ids'

logger = logging.getLogger(__name__)

_busy = 0
_build_begin_handlers = []
_build_end_handlers = []


class DockerException(Exception):
    pass


@gen.coroutine
def _run_loop():
    global _busy

    while True:
        build_id = cache.pop(_BUILD_QUEUE_NAME)
        if not build_id:  # empty queue
            yield gen.sleep(1)
            continue

        cache_key = _make_build_info_cache_key(build_id)

        build_info = cache.get(cache_key)
        if not build_info:
            logger.warning('cannot find cached build info for build id "%s"', build_id)
            yield gen.sleep(1)
            continue

        # wait for a free slot
        while _busy >= settings.DOCKER_MAX_PARALLEL:
            yield gen.sleep(1)

        logger.debug('starting build %s', build_info['build_id'])

        cmd = ('run -td --privileged '
               '-e TB_REPO={git_url} '
               '-e TB_BOARD={board} '
               '-e TB_VERSION={version} '
               '-e TB_PR={pr_no} '
               '-v {dl_dir}:/mnt/dl '
               '-v {ccache_dir}:/mnt/ccache '
               '-v {output_dir}:/mnt/output '
               '--cap-add=SYS_ADMIN '
               '--cap-add=MKNOD '
               '{image}')

        cmd = cmd.format(git_url=build_info['git_url'],
                         board=build_info['board'],
                         version=build_info.get('version', ''),
                         pr_no=build_info.get('pr_no', ''),
                         dl_dir=settings.DL_DIR,
                         ccache_dir=settings.CCACHE_DIR,
                         output_dir=settings.OUTPUT_DIR,
                         image=settings.DOCKER_IMAGE_NAME)

        # run docker container
        container_id = _docker_run_container(cmd)

        # update build info
        build_info['container_id'] = container_id
        build_info['status'] = 'running'
        cache.set(cache_key, build_info)

        _busy += 1
        logger.debug('busy: %d', _busy)

        io_loop = ioloop.IOLoop.current()

        # notify listeners that build has begun
        for handler in _build_begin_handlers:
            io_loop.spawn_callback(functools.partial(handler, build_info))


@gen.coroutine
def _status_loop():
    global _busy

    io_loop = ioloop.IOLoop.current()

    while True:
        # fetch build info
        build_ids = set(cache.get(_BUILD_IDS_NAME, []))
        build_info_list = []
        for build_id in build_ids:
            build_info = cache.get(_make_build_info_cache_key(build_id))
            if build_info:
                build_info_list.append(build_info)

        build_info_by_container_id = {bi['container_id']: bi for bi in build_info_list if 'container_id' in bi}

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
                build_id = build_info['build_id']

                if not container['running']:
                    logger.debug('build %s exited (exit code %s)', build_id, exit_code)

                    _busy -= 1

                    logger.debug('busy: %d', _busy)

                    cache.delete(_make_build_info_cache_key(build_id))
                    build_ids.remove(build_id)
                    cache.set(_BUILD_IDS_NAME, list(build_ids))

                    for handler in _build_end_handlers:
                        io_loop.spawn_callback(functools.partial(handler, build_info, exit_code))

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


def _docker_run_container(cmd):
    return _docker_cmd(cmd).strip()


def _docker_remove_container(container_id):
    _docker_cmd(['container', 'rm', container_id]).strip()


def _docker_kill_container(container_id):
    _docker_cmd(['container', 'kill', container_id]).strip()


def _docker_list_containers():
    containers = []
    s = _docker_cmd(['container', 'ls', '-a', '--no-trunc'])

    lines = s.split('\n')
    lines = [l.strip() for l in lines if l.strip()]
    lines = lines[1:]  # skip header

    for line in lines:
        parts = re.split('\s\s+', line)
        if parts[1] != settings.DOCKER_IMAGE_NAME:
            continue  # not ours

        container_id = parts[0]
        running = parts[4].startswith('Up')
        exit_code = None
        if not running:  # determine exit code
            try:
                exit_code = int(_docker_cmd(['wait', container_id]).strip())

            except Exception as e:
                logger.error('failed to retrieve exit code of container %s: %s', container_id, e,
                             exc_info=True)

                exit_code = 1

        containers.append({
            'id': container_id,
            'running': running,
            'exit_code': exit_code
        })

    return containers


def _docker_cmd(cmd):
    if isinstance(cmd, str):
        cmd = shlex.split(cmd)

    docker_base_cmd = shlex.split(settings.DOCKER_COMMAND)

    cmd = docker_base_cmd + cmd

    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, universal_newlines=True)
    stdout, stderr = p.communicate()

    if p.returncode:
        raise DockerException(stderr)

    return stdout


def _make_build_info_cache_key(build_id):
    return 'build/{}'.format(build_id)


def schedule_build(build_id, service, git_url, pr_no, version, board):
    cache_key = _make_build_info_cache_key(build_id)

    build_info = cache.get(cache_key)
    add_queue = True
    if build_info:
        status = build_info['status']
        if status == 'running':
            logger.debug('stopping previous build "%s"', build_id)

            _docker_kill_container(build_info['container_id'])
            _docker_remove_container(build_info['container_id'])

        elif status == 'pending':
            logger.debug('found pending previous build "%s"', build_id)
            add_queue = False

    else:
        build_ids = set(cache.get(_BUILD_IDS_NAME, []))
        build_ids.add(build_id)
        cache.set(_BUILD_IDS_NAME, list(build_ids))

    build_info = {
        'status': 'pending',
        'build_id': build_id,
        'service': service,
        'git_url': git_url,
        'pr_no': pr_no,
        'version': version,
        'board': board
    }

    logger.debug('scheduling build "%s"', build_id)
    cache.set(cache_key, build_info)
    if add_queue:
        cache.push(_BUILD_QUEUE_NAME, build_id)


def add_build_begin_handler(handler):
    _build_begin_handlers.append(handler)


def add_build_end_handler(handler):
    _build_end_handlers.append(handler)


def init():
    io_loop = ioloop.IOLoop.current()

    io_loop.spawn_callback(_run_loop)
    io_loop.spawn_callback(_status_loop)
