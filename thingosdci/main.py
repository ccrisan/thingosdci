
import logging
import mimetypes

from tornado import ioloop

from thingosdci import cache
from thingosdci import dockerctl
from thingosdci import github
from thingosdci import settings


logger = logging.getLogger('thingosdci')


def configure_logging():
    logging.basicConfig(filename=None, level=settings.LOG_LEVEL,
                        format='%(asctime)s: %(levelname)7s: [%(name)s]: %(message)s',
                        datefmt='%Y-%m-%d %H:%M:%S')


def main():
    configure_logging()
    logger.info('hello!')

    mimetypes.init()
    cache.init()
    dockerctl.init()
    github.init()
    ioloop.IOLoop.current().start()
    logger.info('bye!')


if __name__ == '__main__':
    main()
