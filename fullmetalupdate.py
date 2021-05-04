#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from configparser import ConfigParser
from pathlib import Path
import logging
import argparse
import asyncio
import aiohttp
from distutils.util import strtobool
from fullmetalupdate.fullmetalupdate_ddi_client import FullMetalUpdateDDIClient


async def main():
    # config parsing
    config = ConfigParser()
    parser = argparse.ArgumentParser()
    parser.add_argument(
        '-c',
        '--config',
        type=str,
        help="config file")
    parser.add_argument(
        '-d',
        '--debug',
        action='store_true',
        default=False,
        help="enable debug mode"
    )

    args = parser.parse_args()

    if not args.config:
        args.config = 'config.cfg'

    cfg_path = Path(args.config)

    if not cfg_path.is_file():
        print("Cannot read config file '{}'".format(cfg_path.name))
        exit(1)

    config.read_file(cfg_path.open())

    try:
        LOG_LEVEL = {
            'debug': logging.DEBUG,
            'info': logging.INFO,
            'warn': logging.WARN,
            'error': logging.ERROR,
            'fatal': logging.FATAL,
        }[config.get('client', 'log_level').lower()]
    except Exception:
        LOG_LEVEL = logging.INFO

    local_domain_name = config.get('server', 'server_host_name')

    HOST = local_domain_name + ":" + config.get('client', 'hawkbit_url_port')
    SSL = config.getboolean('client', 'hawkbit_ssl')
    TENANT_ID = config.get('client', 'hawkbit_tenant_id')
    TARGET_NAME = config.get('client', 'hawkbit_target_name')
    AUTH_TOKEN = config.get('client', 'hawkbit_auth_token')
    ATTRIBUTES = {'FullMetalUpdate': config.get('client', 'hawkbit_target_name')}

    if strtobool(config.get('ostree', 'ostree_ssl')):
        url_type = 'https://'
    else:
        url_type = 'http://'

    OSTREE_REMOTE_ATTRIBUTES = {'name': config.get('ostree', 'ostree_name_remote'),
                                'gpg-verify': strtobool(config.get('ostree', 'ostree_gpg-verify')),
                                'url': url_type + local_domain_name + ":" + config.get('ostree', 'ostree_url_port')}

    if args.debug:
        LOG_LEVEL = logging.DEBUG

    logging.basicConfig(level=LOG_LEVEL,
                        format='%(asctime)s %(levelname)-8s %(message)s',
                        datefmt='%Y-%m-%d %H:%M:%S')

    async with aiohttp.ClientSession() as session:
        client = FullMetalUpdateDDIClient(session, HOST, SSL, TENANT_ID, TARGET_NAME,
                                          AUTH_TOKEN, ATTRIBUTES)

        if not client.init_checkout_existing_containers():
            client.logger.info("ConTest :: There is no containers pre-installed on the target")

        if not client.init_ostree_remotes(OSTREE_REMOTE_ATTRIBUTES):
            client.logger.error("ConTest :: Cannot initialize OSTree remote from config file '{}'".format(cfg_path.name))
        else:
            await client.start_polling()

if __name__ == '__main__':
    # create event loop, open aiohttp client session and start polling
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main())
