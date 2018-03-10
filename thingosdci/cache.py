
import json
import logging
import redis

from thingosdci import settings


logger = logging.getLogger(__name__)

_redis_client = None


def init():
    global _redis_client

    logger.debug('initializing redis connection')

    _redis_client = redis.StrictRedis(host=settings.REDIS_HOST,
                                      port=settings.REDIS_PORT,
                                      password=settings.REDIS_PASSWORD,
                                      db=settings.REDIS_DB)


def set(name, value):
    _redis_client.set(name, json.dumps(value))


def get(name, default=None):
    value = _redis_client.get(name)
    if value is None:
        return default

    return json.loads(value)


def delete(name):
    _redis_client.delete(name)


def push(name, value):
    _redis_client.rpush(name, json.dumps(value))


def pop(name):
    value = _redis_client.lpop(name)
    if value is not None:
        value = json.loads(value)

    return value
