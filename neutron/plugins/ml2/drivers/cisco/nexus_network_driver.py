# Copyright 2013 OpenStack Foundation
# All rights reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

"""
Implements a Nexus-OS NETCONF over SSHv2 API Client
"""

from neutron.openstack.common import excutils
from neutron.openstack.common import importutils
from neutron.openstack.common import log as logging
from neutron.plugins.ml2.drivers.cisco import config as conf
from neutron.plugins.ml2.drivers.cisco import constants as const
from neutron.plugins.ml2.drivers.cisco import credentials_v2 as cred
from neutron.plugins.ml2.drivers.cisco import exceptions as cexc
from neutron.plugins.ml2.drivers.cisco import nexus_db_v2
from neutron.plugins.ml2.drivers.cisco import nexus_snippets as snipp

LOG = logging.getLogger(__name__)


class CiscoNexusDriver(object):
    """Nexus Driver Main Class."""
    def __init__(self):
        self.ncclient = None
        self.nexus_switches = conf.ML2MechCiscoConfig.nexus_dict
        self.credentials = {}
        self.connections = {}

    def _import_ncclient(self):
        """Import the NETCONF client (ncclient) module.

        The ncclient module is not installed as part of the normal Neutron
        distributions. It is imported dynamically in this module so that
        the import can be mocked, allowing unit testing without requiring
        the installation of ncclient.

        """
        return importutils.import_module('ncclient.manager')

    def _edit_config(self, nexus_host, target='running', config='',
                     allowed_exc_strs=None):
        """Modify switch config for a target config type.

        :param nexus_host: IP address of switch to configure
        :param target: Target config type
        :param config: Configuration string in XML format
        :param allowed_exc_strs: Exceptions which have any of these strings
                                 as a subset of their exception message
                                 (str(exception)) can be ignored

        :raises: NexusConfigFailed

        """
        if not allowed_exc_strs:
            allowed_exc_strs = []
        mgr = self.nxos_connect(nexus_host)
        try:
            mgr.edit_config(target, config=config)
        except Exception as e:
            for exc_str in allowed_exc_strs:
                if exc_str in str(e):
                    break
            else:
                # Raise a Neutron exception. Include a description of
                # the original ncclient exception.
                raise cexc.NexusConfigFailed(config=config, exc=e)

    def get_credential(self, nexus_ip):
        """Return credential information for a given Nexus IP address.

        If credential doesn't exist then also add to local dictionary.
        """
        if nexus_ip not in self.credentials:
            nexus_username = cred.Store.get_username(nexus_ip)
            nexus_password = cred.Store.get_password(nexus_ip)
            self.credentials[nexus_ip] = {
                const.USERNAME: nexus_username,
                const.PASSWORD: nexus_password
            }
        return self.credentials[nexus_ip]

    def nxos_connect(self, nexus_host):
        """Make SSH connection to the Nexus Switch."""
        if getattr(self.connections.get(nexus_host), 'connected', None):
            return self.connections[nexus_host]

        if not self.ncclient:
            self.ncclient = self._import_ncclient()
        nexus_ssh_port = int(self.nexus_switches[nexus_host, 'ssh_port'])
        nexus_creds = self.get_credential(nexus_host)
        nexus_user = nexus_creds[const.USERNAME]
        nexus_password = nexus_creds[const.PASSWORD]
        try:
            man = self.ncclient.connect(host=nexus_host,
                                        port=nexus_ssh_port,
                                        username=nexus_user,
                                        password=nexus_password)
            self.connections[nexus_host] = man
        except Exception as e:
            # Raise a Neutron exception. Include a description of
            # the original ncclient exception.
            raise cexc.NexusConnectFailed(nexus_host=nexus_host, exc=e)

        return self.connections[nexus_host]

    def create_xml_snippet(self, customized_config):
        """Create XML snippet.

        Creates the Proper XML structure for the Nexus Switch Configuration.
        """
        conf_xml_snippet = snipp.EXEC_CONF_SNIPPET % (customized_config)
        return conf_xml_snippet

    def create_vlan(self, nexus_host, vlanid, vlanname):
        """Create a VLAN on Nexus Switch given the VLAN ID and Name."""
        confstr = self.create_xml_snippet(
            snipp.CMD_VLAN_CONF_SNIPPET % (vlanid, vlanname))
        LOG.debug(_("NexusDriver: %s"), confstr)
        self._edit_config(nexus_host, target='running', config=confstr)

        # Enable VLAN active and no-shutdown states. Some versions of
        # Nexus switch do not allow state changes for the extended VLAN
        # range (1006-4094), but these errors can be ignored (default
        # values are appropriate).
        for snippet in [snipp.CMD_VLAN_ACTIVE_SNIPPET,
                        snipp.CMD_VLAN_NO_SHUTDOWN_SNIPPET]:
            try:
                confstr = self.create_xml_snippet(snippet % vlanid)
                self._edit_config(
                    nexus_host,
                    target='running',
                    config=confstr,
                    allowed_exc_strs=["Can't modify state for extended",
                                      "Command is only allowed on VLAN"])
            except cexc.NexusConfigFailed:
                with excutils.save_and_reraise_exception():
                    self.delete_vlan(nexus_host, vlanid)

    def delete_vlan(self, nexus_host, vlanid):
        """Delete a VLAN on Nexus Switch given the VLAN ID."""
        confstr = snipp.CMD_NO_VLAN_CONF_SNIPPET % vlanid
        confstr = self.create_xml_snippet(confstr)
        self._edit_config(nexus_host, target='running', config=confstr)

    def enable_port_trunk(self, nexus_host, interface):
        """Enable trunk mode an interface on Nexus Switch."""
        confstr = snipp.CMD_PORT_TRUNK % (interface)
        confstr = self.create_xml_snippet(confstr)
        LOG.debug(_("NexusDriver: %s"), confstr)
        self._edit_config(nexus_host, target='running', config=confstr)

    def disable_switch_port(self, nexus_host, interface):
        """Disable trunk mode an interface on Nexus Switch."""
        confstr = snipp.CMD_NO_SWITCHPORT % (interface)
        confstr = self.create_xml_snippet(confstr)
        LOG.debug(_("NexusDriver: %s"), confstr)
        self._edit_config(nexus_host, target='running', config=confstr)

    def enable_vlan_on_trunk_int(self, nexus_host, vlanid, interface):
        """Enable a VLAN on a trunk interface."""
        # If more than one VLAN is configured on this interface then
        # include the 'add' keyword.
        if len(nexus_db_v2.get_port_switch_bindings(interface,
                                                    nexus_host)) == 1:
            snippet = snipp.CMD_INT_VLAN_SNIPPET
        else:
            snippet = snipp.CMD_INT_VLAN_ADD_SNIPPET
        confstr = snippet % (interface, vlanid)
        confstr = self.create_xml_snippet(confstr)
        LOG.debug(_("NexusDriver: %s"), confstr)
        self._edit_config(nexus_host, target='running', config=confstr)

    def disable_vlan_on_trunk_int(self, nexus_host, vlanid, interface):
        """Disable a VLAN on a trunk interface."""
        confstr = snipp.CMD_NO_VLAN_INT_SNIPPET % (interface, vlanid)
        confstr = self.create_xml_snippet(confstr)
        LOG.debug(_("NexusDriver: %s"), confstr)
        self._edit_config(nexus_host, target='running', config=confstr)

    def create_and_trunk_vlan(self, nexus_host, vlan_id, vlan_name,
                              nexus_port):
        """Create VLAN and trunk it on the specified ports."""
        self.create_vlan(nexus_host, vlan_id, vlan_name)
        LOG.debug(_("NexusDriver created VLAN: %s"), vlan_id)
        if nexus_port:
            self.enable_vlan_on_trunk_int(nexus_host, vlan_id, nexus_port)

    def delete_and_untrunk_vlan(self, nexus_host, vlan_id, nexus_port):
        """Delete VLAN and untrunk it from the specified ports."""
        self.delete_vlan(nexus_host, vlan_id)
        if nexus_port:
            self.disable_vlan_on_trunk_int(nexus_host, vlan_id, nexus_port)

    def create_vlan_svi(self, nexus_host, vlan_id, gateway_ip):
        confstr = snipp.CMD_VLAN_SVI_SNIPPET % (vlan_id, gateway_ip)
        confstr = self.create_xml_snippet(confstr)
        LOG.debug(_("NexusDriver: %s"), confstr)
        self._edit_config(nexus_host, target='running', config=confstr)

    def delete_vlan_svi(self, nexus_host, vlan_id):
        confstr = snipp.CMD_NO_VLAN_SVI_SNIPPET % vlan_id
        confstr = self.create_xml_snippet(confstr)
        LOG.debug(_("NexusDriver: %s"), confstr)
        self._edit_config(nexus_host, target='running', config=confstr)
