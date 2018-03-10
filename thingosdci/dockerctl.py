
import logging
import subprocess

from tornado import gen
from tornado import ioloop

from thingosdci import cache
from thingosdci import settings


_BUILD_QUEUE_NAME = 'docker-build-queue'
_BUILD_IDS_NAME = 'docker-build-ids'

logger = logging.getLogger(__name__)

_busy = False
_build_begin_handlers = []
_build_end_handlers = []


@gen.coroutine
def _run_loop():
    global _busy

    while True:
        build_info = cache.pop(_BUILD_QUEUE_NAME)
        if not build_info:  # empty queue
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
               'thingos-builder')

        cmd = cmd.format(git_url=build_info['git_url'],
                         board=build_info['board'],
                         version=build_info['version'],
                         dl_dir=settings.DL_DIR,
                         ccache_dir=settings.CCACHE_DIR,
                         output_dir=settings.OUTPUT_DIR)

        # run docker container
        container_id = _docker_cmd(cmd)
        build_info['container_id'] = container_id
        build_info['status'] = 'running'

        # mark dockerctl as busy
        _busy = True

        # notify listeners that build has begun
        for handler in _build_begin_handlers:
            handler(build_info)

        # wait finish
        while _busy:
            yield gen.sleep(1)


@gen.coroutine
def _status_loop():
    global _busy

    # TODO set busy False


@gen.coroutine
def _cleanup_loop():
    # TODO remove from cache build ids as well
    pass


def schedule_build(build_id, service, git_url, pr_no, version, board):
    cache_key = 'build/{}'.format(build_id)

    build_info = cache.get(cache_key)
    add_queue = True
    if build_info:
        status = build_info['status']
        if status == 'running':
            logger.debug('stopping previous build "%s"', build_id)
            # TODO stop

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
        'version': version,
        'board': board
    }

    logger.debug('scheduling build "%s"', build_id)
    cache.set(cache_key, build_info)
    if add_queue:
        cache.push(_BUILD_QUEUE_NAME, build_id)


def add_build_begin_handler(handler):
    pass


def add_build_end_handler(handler):
    pass


def init():
    io_loop = ioloop.IOLoop.current()

    io_loop.spawn_callback(_run_loop)
    io_loop.spawn_callback(_status_loop)
    io_loop.spawn_callback(_cleanup_loop)
