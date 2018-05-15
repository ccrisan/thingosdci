
import json
import logging
import os

from thingosdci import settings


logger = logging.getLogger(__name__)


def load(name, default=None):
    logger.debug('loading %s', name)

    file_path = os.path.join(settings.PERSIST_DIR, '{}.json'.format(name))
    if os.path.exists(file_path):
        try:
            with open(file_path, 'r') as f:
                return json.load(f)

        except Exception:
            logger.error('cannot read file %s', file_path, exc_info=True)
            raise

    return default


def save(name, value):
    logger.debug('saving %s', name)

    file_path = os.path.join(settings.PERSIST_DIR, '{}.json'.format(name))
    try:
        with open(file_path, 'w') as f:
            json.dump(value, f)

    except Exception as e:
        logger.error('cannot save to file %s', file_path, exc_info=True)
        raise
