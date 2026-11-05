#!/usr/bin/env python3
# coding=utf-8

"""Standard setup.py. Speaks for itself."""

from setuptools import setup, find_packages

with open('README.md', 'r') as fh:
    long_description = fh.read()

setup(
    name='isdttool-pal',
    version='1.1.1',
    packages=find_packages(),
    url='https://github.com/pallemannen/isdttool',
    license='GPLv3',
    install_requires='hidapi>=0.10.1',
    author='Max Ried',
    author_email='maxried@posteo.de',
    maintainer='Patrik Bodin',
    maintainer_email='the@pal.pp.se',
    description='Tool to retrieve information from ISDT chargers with USB connection.',
    long_description=long_description,
    long_description_content_type="text/markdown",
    entry_points={'console_scripts': ['isdttool=isdttool.cli_tool:main']},
    python_requires='>=3.6',
    classifiers=[
        "Programming Language :: Python :: 3",
        "Topic :: System :: Hardware :: Hardware Drivers",
        "License :: OSI Approved :: GNU General Public License v3 (GPLv3)",
        "Operating System :: OS Independent",
    ],

)
