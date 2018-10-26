
from setuptools import setup

import thingosdci


name = 'thingosdci'
version = thingosdci.VERSION
description = 'thingOS Docker CI'

setup(
    name=name,
    version=version,

    description=description,
    long_description=description,

    url='https://github.com/ccrisan/thingosdci',

    author='Calin Crisan',
    author_email='ccrisan@gmail.com',

    license='GPLv3',

    packages=['thingosdci', 'thingosdci.reposervices'],

    install_requires=[
        'tornado>=4',
        'redis>=2.10',
        'uritemplate>=3'
    ],

    data_files=[
        ('bin', ['thingosdci.sh'])
    ],

    entry_points={
        'console_scripts': [
            'thingosdci=thingosdci.main:main',
        ],
    }
)
