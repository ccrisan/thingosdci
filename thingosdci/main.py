
import logging
import mimetypes
import os
import sys

from tornado import ioloop

from thingosdci import building
from thingosdci import dockerctl
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
    reposervices.init()
    ioloop.IOLoop.current().start()
    logger.info('bye!')


if __name__ == '__main__':
    main()
