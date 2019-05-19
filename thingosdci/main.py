
import functools
import logging
import mimetypes
import os
import sys

from tornado import ioloop
from tornado import gen

from thingosdci import building
from thingosdci import dockerctl
from thingosdci import loopdevmanager
from thingosdci import reposervices
from thingosdci import settings
from thingosdci import VERSION


logger = logging.getLogger('thingosdci')


def configure_logging():
    logging.basicConfig(filename=None, level=settings.LOG_LEVEL,
                        format='%(asctime)s: %(levelname)7s: [%(name)s]: %(message)s',
                        datefmt='%Y-%m-%d %H:%M:%S')


def create_dirs():
    os.makedirs(settings.DL_DIR, exist_ok=True)
    os.makedirs(settings.BUILD_LOGS_DIR, exist_ok=True)
    os.makedirs(settings.CCACHE_DIR, exist_ok=True)
    os.makedirs(settings.OUTPUT_DIR, exist_ok=True)
    os.makedirs(settings.PERSIST_DIR, exist_ok=True)


@gen.coroutine
def shell(board=None):
    if board:
        yield building.run_custom_cmd(repo_service=None, custom_cmd='/bin/bash', interactive=True, board=board)

    else:
        yield building.run_custom_cmd(repo_service=None, custom_cmd='/bin/bash', interactive=True)

    ioloop.IOLoop.current().stop()


def main():
    configure_logging()
    logger.info('hello!')
    logger.info('this is thingOS Docker CI %s', VERSION)
    sl = sys.modules.get('settingslocal')
    if sl:
        logger.info('using settings from %s', sl.__file__)

    else:
        logger.warning('using default settings')

    create_dirs()

    mimetypes.init()
    building.init()
    dockerctl.init()
    loopdevmanager.init()

    cmd = len(sys.argv) > 1 and sys.argv[1]
    if cmd == 'shell':
        board = None
        if len(sys.argv) > 2:
            board = sys.argv[2]

        io_loop = ioloop.IOLoop.current()
        io_loop.run_sync(functools.partial(shell, board=board))

    else:
        reposervices.init()
        ioloop.IOLoop.current().start()

    logger.info('bye!')


if __name__ == '__main__':
    main()
