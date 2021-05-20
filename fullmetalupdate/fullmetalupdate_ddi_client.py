# -*- coding: utf-8 -*-

from datetime import datetime, timedelta
import os
import os.path
import re
import logging
import json
import threading
import socket as s
import subprocess
import asyncio
import gi

from fullmetalupdate.updater import AsyncUpdater
from rauc_hawkbit.ddi.client import DDIClient, APIError
from rauc_hawkbit.ddi.client import (
    ConfigStatusExecution, ConfigStatusResult)
from rauc_hawkbit.ddi.deployment_base import (
    DeploymentStatusExecution, DeploymentStatusResult)
from rauc_hawkbit.ddi.cancel_action import (
    CancelStatusExecution, CancelStatusResult)
from aiohttp.client_exceptions import ClientOSError, ClientResponseError
gi.require_version('OSTree', '1.0')
from gi.repository import GLib, Gio, OSTree

PATH_REBOOT_DATA = '/var/local/fullmetalupdate/reboot_data.json'
PATH_NOTIFY_SOCKET = '/tmp/fullmetalupdate/fullmetalupdate_notify.sock'


class FullMetalUpdateDDIClient(AsyncUpdater):
    """
    Client broker communicating via DBUS and HawkBit DDI HTTP
    interface.
    """
    def __init__(self, session, host, ssl, tenant_id, target_name, auth_token,
                 attributes, lock_keeper=None):
        super(FullMetalUpdateDDIClient, self).__init__()

        self.attributes = attributes

        self.logger = logging.getLogger('fullmetalupdate_hawkbit')
        self.ddi = DDIClient(session, host, ssl, auth_token, tenant_id, target_name)
        self.action_id = None

        self.lock_keeper = lock_keeper

        os.makedirs(os.path.dirname(PATH_REBOOT_DATA), exist_ok=True)
        os.makedirs(os.path.dirname(PATH_NOTIFY_SOCKET), exist_ok=True)

    async def start_polling(self, wait_on_error=60):
        """Wrapper around self.poll_base_resource() for exception handling."""

        while True:
            try:
                await self.poll_base_resource()
            except asyncio.CancelledError:
                self.logger.info('Polling cancelled')
                break
            except asyncio.TimeoutError:
                self.logger.warning('Polling failed due to TimeoutError')
            except (APIError, TimeoutError, ClientOSError, ClientResponseError) as e:
                # log error and start all over again
                self.logger.warning('Polling failed with a temporary error: {}'.format(e))
            except Exception:
                self.logger.exception('Polling failed with an unexpected exception:')
            self.action_id = None
            self.logger.info('Retry will happen in {} seconds'.format(
                wait_on_error))
            await asyncio.sleep(wait_on_error)

    async def identify(self):
        """Identify target against HawkBit."""
        self.logger.info('Sending identifying information to HawkBit')
        # identify
        await self.ddi.configData(ConfigStatusExecution.closed,
                                  ConfigStatusResult.success, **self.attributes)

    async def cancel(self, base):
        self.logger.info('Received cancelation request')
        # retrieve action id from URL
        deployment = base['_links']['cancelAction']['href']
        match = re.search('/cancelAction/(.+)$', deployment)
        action_id, = match.groups()
        # retrieve stop_id
        stop_info = await self.ddi.cancelAction[action_id]()
        stop_id = stop_info['cancelAction']['stopId']
        # Reject cancel request
        self.logger.info('Rejecting cancelation request')
        await self.ddi.cancelAction[stop_id].feedback(
            CancelStatusExecution.rejected,
            CancelStatusResult.success,
            status_details=("Cancelling not supported",))

    async def install(self):
        if self.lock_keeper and not self.lock_keeper.lock(self):
            self.logger.info("Another installation is already in progress, aborting")
            return

    async def process_deployment(self, base):
        """
        Check for deployments, download them, verify checksum and trigger
        RAUC install operation.
        """
        if self.action_id is not None:
            self.logger.info('Deployment is already in progress')
            return

        status_result = DeploymentStatusResult.success

        # retrieve action id and resource parameter from URL
        deployment = base['_links']['deploymentBase']['href']
        match = re.search('/deploymentBase/(.+)\?c=(.+)$', deployment)
        action_id, resource = match.groups()
        # fetch deployment information
        deploy_info = await self.ddi.deploymentBase[action_id](resource)
        reboot_needed = False

        chunks_qty = len(deploy_info['deployment']['chunks'])

        if chunks_qty == 0:
            msg = 'Deployment without chunks found. Ignoring'
            status_execution = DeploymentStatusExecution.closed
            status_result = DeploymentStatusResult.failure
            await self.ddi.deploymentBase[action_id].feedback(
                status_execution, status_result, [msg])
            raise APIError(msg)
        else:
            msg = "FullMetalUpdate:Proceeding"
            percentage = {"cnt": 0, "of": chunks_qty}
            status_execution = DeploymentStatusExecution.proceeding
            status_result = DeploymentStatusResult.none
            await self.ddi.deploymentBase[action_id].feedback(
                status_execution, status_result, [msg],
                percentage=percentage)

        self.action_id = action_id

        seq = ('name', 'version', 'rev', 'part', 'autostart', 'autoremove', 'status_execution', 'status_update', 'status_result', 'notify', 'timeout')
        updates = []

        for chunk in deploy_info['deployment']['chunks']:
            update = dict.fromkeys(seq)
            # parse the metadata included in the update
            for meta in chunk['metadata']:
                if meta['key'] == 'rev':
                    update['rev'] = meta['value']
                if meta['key'] == 'autostart':
                    update['autostart'] = int(meta['value'])
                if meta['key'] == 'autoremove':
                    update['autoremove'] = int(meta['value'])
                if meta['key'] == 'notify':
                    update['notify'] = int(meta['value'])
                if meta['key'] == 'timeout':
                    update['timeout'] = int(meta['value'])
            update['name'] = chunk['name']
            update['version'] = chunk['version']
            update['part'] = chunk['part']

            self.logger.info("Updating chunk part: {}".format(update['part']))

            if update['part'] == 'os':

                # checking if we just rebooted and we need to send the feedback in which
                # case we don't need to pull the update image again
                [feedback, reboot_data] = self.feedback_for_os_deployment(update['rev'])
                if feedback:
                    await self.ddi.deploymentBase[reboot_data["action_id"]].feedback(
                        DeploymentStatusExecution(reboot_data["status_execution"]),
                        DeploymentStatusResult(reboot_data["status_result"]),
                        [reboot_data["msg"]])
                    self.action_id = None
                    return

                self.logger.info("OS {} v.{} - updating...".format(update['name'], update['version']))
                update['status_update'] = self.update_system(update['rev'])
                update['status_execution'] = DeploymentStatusExecution.closed
                if not update['status_update']:
                    msg = "OS {} v.{} Deployment failed".format(update['name'], update['version'])
                    self.logger.error(msg)
                    update['status_result'] = DeploymentStatusResult.failure
                    await self.ddi.deploymentBase[self.action_id].feedback(
                        update['status_execution'], update['status_result'], [msg])
                    return
                else:
                    msg = "OS {} v.{} Deployment succeed".format(update['name'], update['version'])
                    self.logger.info(msg)
                    update['status_result'] = DeploymentStatusResult.success
                    reboot_needed = True
                    self.write_reboot_data(self.action_id,
                                           update['status_execution'],
                                           update['status_result'],
                                           msg)

            elif update['part'] == 'bApp':
                self.logger.info("App {} v.{} - updating...".format(update['name'], update['version']))
                update['status_update'] = self.update_container(update['name'], update['rev'], update['autostart'], update['autoremove'], update['notify'], update['timeout'])
                update['status_execution'] = DeploymentStatusExecution.closed
                updates.append(update)

        self.systemd.Reload()

        final_result = True
        fails = ""

        for update in updates:
            update['status_update'] &= self.handle_container(update['name'], update['autostart'], update['autoremove'])
            if not update['status_update']:
                msg = "App {} v.{} Deployment failed".format(update['name'], update['version'])
                self.logger.error(msg)
                update['status_result'] = DeploymentStatusResult.failure
                fails += update['name'] + " "
            elif update['notify'] != 1:
                msg = "App {} v.{} Deployment succeed".format(update['name'], update['version'])
                self.logger.info(msg)
                update['status_result'] = DeploymentStatusResult.success

            final_result &= (update['status_result'] == DeploymentStatusResult.success)
        
        if(final_result):
            msg = "Hawkbit Update Success : All applications have been updated and correctly restarted."
            self.logger.info(msg)
            status_result = DeploymentStatusResult.success
        else:
            msg = "Hawkbit Update Failure : " + fails + "failed to update and / or to restart."
            self.logger.error(msg)
            status_result = DeploymentStatusResult.failure
        await self.ddi.deploymentBase[self.action_id].feedback(DeploymentStatusExecution.closed, status_result, [msg])

        self.action_id = None
        if reboot_needed:
            try:
                subprocess.run("reboot")
            except subprocess.CalledProcessError as e:
                self.logger.error("Reboot failed: {}".format(e))

    async def sleep(self, base):
        """Sleep time suggested by HawkBit."""
        sleep_str = base['config']['polling']['sleep']
        self.logger.info('Will sleep for {}'.format(sleep_str))
        t = datetime.strptime(sleep_str, '%H:%M:%S')
        delta = timedelta(hours=t.hour, minutes=t.minute, seconds=t.second)
        await asyncio.sleep(delta.total_seconds())

    async def poll_base_resource(self):
        """Poll DDI API base resource."""
        while True:
            base = await self.ddi()

            if '_links' in base:
                if 'configData' in base['_links']:
                    await self.identify()
                if 'deploymentBase' in base['_links']:
                    await self.process_deployment(base)
                if 'cancelAction' in base['_links']:
                    await self.cancel(base)

            await self.sleep(base)

    def update_container(self, container_name, rev_number, autostart, autoremove,
                         action_id, notify=None, timeout=None):
        """
        Wrapper method to execute the different steps of a container update.
        """
        try:
            self.init_container_remote(container_name)
            self.pull_ostree_ref(True, rev_number, container_name)
            self.checkout_container(container_name, rev_number)
            self.update_container_ids(container_name)
            if (autostart == 1) and (notify == 1) and (autoremove != 1):
                self.create_and_start_feedback_thread(container_name, rev_number,
                                                      autostart, autoremove, timeout,
                                                      action_id)
            self.create_unit(container_name)
        except Exception as e:
            self.logger.error("Updating {} failed ({})".format(container_name, e))
            return False
        return True

    def update_system(self, rev_number):
        """
        Wrapper method to execute the different steps of a OS update.
        """
        try:
            self.pull_ostree_ref(False, rev_number)
            self.ostree_stage_tree(rev_number)
            self.delete_init_var()
        except Exception as e:
            self.logger.error("Updating the OS failed ({})".format(e))
            return False
        return True

    def write_reboot_data(self, action_id, status_execution, status_result, msg):
        """
        Write information about the current update in a file.

        Parameters:
        action_id (int): the current action is (update id)
        status_execution (enum): the execution status
        status_result (enum): the result status
        msg (str): the message to be sent to the server
        """
        # the enums are not serializable thus we store their value
        reboot_data = {
            "action_id": action_id,
            "status_execution": status_execution.value,
            "status_result": status_result.value,
            "msg": msg
        }

        try:
            with open(PATH_REBOOT_DATA, "w") as f:
                json.dump(reboot_data, f)
        except IOError as e:
            self.logger.error("Writing reboot data failed ({})".format(e))

    def feedback_for_os_deployment(self, revision):
        """
        This method will generate a feedback message for the Hawkbit server and
        return the reboot data which will be used by the the DDI client to return
        the appropriate feedback message.

        Parameters:
        revision (str): the OS revision
        """

        reboot_data = None
        try:
            with open(PATH_REBOOT_DATA, "r") as f:
                reboot_data = json.load(f)
            if reboot_data is None:
                self.logger.error("Rebooting data loading failed")
        except FileNotFoundError:
            return (False, None)

        if self.check_for_rollback(revision):
            reboot_data.update({"status_result": DeploymentStatusResult.failure.value})
            reboot_data.update({"msg": "Deployment has failed and system has rollbacked"})

        os.remove(PATH_REBOOT_DATA)

        return (True, reboot_data)

    def create_and_start_feedback_thread(self, container_name, rev, autostart, autoremove, timeout, action_id):
        """
        This method is called to initialize and start the feedback thread used to
        feedback the server the status of the notify container. See the
        container_feedbacker thread method.

        Parameters:
        container_name (str): the name os the container
        rev (str): the commit revision, used for rollbacking
        autostart (int): autostart of the container, used for rollbacking
        autoremove (int): autoremove of the container, used for rollbacking
        timeout (int): timeout value of the communication socket
        """
        self.logger.info("Creating socket {}".format(PATH_NOTIFY_SOCKET))
        sock = s.socket(s.AF_UNIX, s.SOCK_STREAM)
        sock.settimeout(timeout)
        if os.path.exists(PATH_NOTIFY_SOCKET):
            os.remove(PATH_NOTIFY_SOCKET)
        sock.bind(PATH_NOTIFY_SOCKET)

        container_feedbackd = threading.Thread(
            target=self.container_feedbacker,
            args=(asyncio.get_event_loop(),
                  sock,
                  container_name,
                  rev,
                  autostart,
                  autoremove,
                  action_id),
            name="container-feedback")
        container_feedbackd.start()

    def container_feedbacker(self,
                             event_loop,
                             socket,
                             container_name,
                             rev_number,
                             autostart,
                             autoremove,
                             action_id):
        """
        This thread method is used to feedback the server for containers which provide
        the notify feature of systemd. It will trigger a rollback on the container in case
        of failure (if possible).
        This method will wait on an Unix socket for information about the notify result,
        and proceed in consequence.

        Parameters:
        event_loop (EventLoop): the main event loop. Used to perform a feedback from this
                                thread.
        socket (socket): the socket used for communication between the container service
                         and this thread
        container_name (str): the name of the container
        rev_number (str): the commit revision, used for rollbacking
        autostart (int): autostart of the container, used for rollbacking
        autoremove (int): autoremove of the container, used for rollbacking
        """

        try:
            socket.listen(1)
            [conn, _] = socket.accept()
            datagram = conn.recv(1024)

            if datagram:
                systemd_info = datagram.strip().decode("utf-8").split()
                self.logger.debug("Datagram received : {}".format(systemd_info))
                if systemd_info[0] == 'success':
                    # feedback the server positively
                    msg = "The notify container started successfully"
                    status_result = DeploymentStatusResult.success
                    status_execution = DeploymentStatusExecution.closed
                    self.logger.info(msg)
                    asyncio.run_coroutine_threadsafe(
                        self.ddi.deploymentBase[action_id].feedback(
                            status_execution, status_result, [msg]), event_loop)
                    # Write this new revision for future updates
                    self.set_current_revision(container_name, rev_number)
                else:
                    # rollback + feedback the server
                    status_result = DeploymentStatusResult.failure
                    status_execution = DeploymentStatusExecution.closed
                    end_msg = self.rollback_container(container_name,
                                                      autostart,
                                                      autoremove)
                    msg = "The container failed to start with result :" \
                        + "\n\tSERVICE_RESULT=" + systemd_info[0] \
                        + "\n\tEXIT_CODE=" + systemd_info[1] \
                        + "\n\tEXIT_STATUS=" + systemd_info[2] \
                        + end_msg
                    self.logger.info(msg)
                    asyncio.run_coroutine_threadsafe(self.ddi.deploymentBase[action_id].feedback(
                        status_execution, status_result, [msg]), event_loop)
        except s.timeout:
            # socket timeout, try to rollback if possible
            status_result = DeploymentStatusResult.failure
            status_execution = DeploymentStatusExecution.closed
            msg = "The socket timed out."
            self.logger.error(msg)
            end_msg = self.rollback_container(container_name,
                                              autostart,
                                              autoremove)
            asyncio.run_coroutine_threadsafe(
                self.ddi.deploymentBase[action_id].feedback(
                    status_execution, status_result, [msg + end_msg]), event_loop)

        socket.close()
        self.logger.info("Removing socket {}".format(PATH_NOTIFY_SOCKET))
        try:
            os.remove(PATH_NOTIFY_SOCKET)
        except FileNotFoundError as e:
            self.logger.error("Error while removing socket ({})".format(e))

    def rollback_container(self, container_name, autostart, autoremove):
        """
        This method Rollbacks the container, if possible, and returns a message that will
        be sent to the server.

        Parameters:
        container_name (str): the name of the container
        autostart (int): autostart of the container
        autoremove (int): autoremove of the container

        Returns:
        end_msg (str): the end of the message that will be sent, which depends on the
                       status of the rollback (performed or not)
        """

        end_msg = ""
        previous_rev = self.get_previous_rev(container_name)

        if previous_rev is None:
            end_msg = "\nFirst installation of the container, cannot rollback."
        else:
            res = self.update_container(container_name,
                                        previous_rev,
                                        autostart,
                                        autoremove)
            if res:
                end_msg = "\nContainer has rollbacked."
            else:
                end_msg = "\nContainer has failed to rollback."

        return end_msg
