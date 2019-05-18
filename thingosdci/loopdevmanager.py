
import logging
import os
import stat

from thingosdci import settings


_LOOP_DEV_PATTERN = '/dev/loop{}'
_FILE_PERMS = 0o660

logger = logging.getLogger(__name__)

_loop_devs = {}


class LoopDevManagerException(Exception):
    pass


def acquire_loop_dev():
    for l, busy in _loop_devs.items():
        if not busy:
            loop_dev = _LOOP_DEV_PATTERN.format(l)
            logger.debug('acquiring %s', loop_dev)
            _loop_devs[l] = True

            return loop_dev

    raise LoopDevManagerException('no free loop device')


def release_loop_dev(loop_dev):
    l = loop_dev[9:]
    try:
        l = int(l)

    except ValueError:
        raise LoopDevManagerException('unknown loop device: {}'.format(loop_dev))

    try:
        busy = _loop_devs[l]

    except KeyError:
        raise LoopDevManagerException('unknown loop device: {}'.format(loop_dev))

    if not busy:
        raise LoopDevManagerException('attempt to release free loop device: {}'.format(loop_dev))

    logger.debug('releasing %s', loop_dev)
    _loop_devs[l] = False


def _ensure_loop_dev(loop_dev):
    if os.path.exists(loop_dev):
        return

    try:
        os.mknod(loop_dev, mode=stat.S_IFBLK | _FILE_PERMS)

    except Exception as e:
        logger.error('failed to create loop device: %s', e)


def init():
    global _loop_devs

    rng = range(settings.LOOP_DEV_RANGE[0], settings.LOOP_DEV_RANGE[1] + 1)
    logger.debug('initializing loop devices (/dev/loop%s - /dev/loop%s)', *settings.LOOP_DEV_RANGE)
    _loop_devs = {l: False for l in rng}

    for l in rng:
        loop_dev = _LOOP_DEV_PATTERN.format(l)
        _ensure_loop_dev(loop_dev)
