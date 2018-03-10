
import logging

from tornado import ioloop

from thingosdci import cache
from thingosdci import github
from thingosdci import settings


logger = logging.getLogger('thingosdci')


def configure_logging():
    logging.basicConfig(filename=None, level=settings.LOG_LEVEL,
                        format='%(asctime)s: %(levelname)7s: [%(name)s]: %(message)s',
                        datefmt='%Y-%m-%d %H:%M:%S')


def main():
    configure_logging()
    cache.init()
    github.start_event_server()

    ioloop.IOLoop.current().start()


if __name__ == '__main__':
    main()
