# -*- coding: utf-8 -*-
# Copyright 2016 Dravetech AB. All rights reserved.
#
# The contents of this file are licensed under the Apache License, Version 2.0
# (the "License"); you may not use this file except in compliance with the
# License. You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations under
# the License.

"""Napalm driver for Dell/Force10 FTOS."""

import re
import socket
import types

from napalm.base.helpers import textfsm_extractor
from napalm.base.helpers import canonical_interface_name, mac, ip
from napalm.base.netmiko_helpers import netmiko_args

from napalm.base import NetworkDriver
from napalm.base.exceptions import ConnectionException

# Easier to store these as constants
MINUTE_SECONDS = 60
HOUR_SECONDS = 60 * MINUTE_SECONDS
DAY_SECONDS = 24 * HOUR_SECONDS
WEEK_SECONDS = 7 * DAY_SECONDS
YEAR_SECONDS = 365 * DAY_SECONDS


# overload canonical_interface_name and apply some FTOS specifics
def _canonical_interface_name(iface):
    iface = canonical_interface_name(iface)

    # add whitespace in TenGigabitEthernet names
    m = re.search(r'^(TenGigabitEthernet)(\d+\/\d+)$', iface)
    if m:
        iface = ' '.join(m.groups())

    return iface


class FTOSDriver(NetworkDriver):
    """NAPALM Dell Force10 FTOS Handler."""

    def __init__(self, hostname, username, password, timeout=60, optional_args=None):
        """NAPALM Dell Force10 FTOS Handler."""
        self.device = None
        self.hostname = hostname
        self.username = username
        self.password = password
        self.timeout = timeout

        if optional_args is None:
            optional_args = {}

        self.netmiko_optional_args = netmiko_args(optional_args)

    def _send_command(self, command):
        try:
            if isinstance(command, list):
                for cmd in command:
                    output = self.device.send_command(cmd)
                    if "% Invalid" not in output:
                        break
            else:
                output = self.device.send_command(command)
            return output
        except (socket.error, EOFError) as e:
            raise ConnectionException(str(e))

    @staticmethod
    def _parse_uptime(uptime_str, short=False):
        # Extract the uptime string from the given FTOS Device given in form of
        # 32 week(s), 6 day(s), 10 hour(s), 39 minute(s).
        #
        # When short is set to True, expect the format to be either hh:mm:ss or
        # in form 32w6d10h.
        #
        # Return the uptime in seconds as an integer.

        # Initialize to zero
        (years, weeks, days, hours, minutes, seconds) = (0, 0, 0, 0, 0, 0)

        uptime_str = uptime_str.strip()
        if short:
            # until a day has passed, time is expressed in hh:mm:ss
            # after a day, time is expressed as 1d22h23m or even 20w4d21h
            # perhaps even in years at some point

            match = re.compile(r'^(\d+):(\d+):(\d+)$').search(uptime_str)
            if match:
                (hours, minutes, seconds) = (int(match.group(1)), int(match.group(2)), int(match.group(3)))
            else:
                match = re.compile(r'(\d+w)?(\d+d)?(\d+h)?(\d+m)?').search(uptime_str)
                if match:
                    for m in match.groups():
                        if m is None:
                            continue
                        # year
                        elif m.endswith('y'):
                            years = int(m[:-1])
                        # week
                        elif m.endswith('w'):
                            weeks = int(m[:-1])
                        # day
                        elif m.endswith('d'):
                            days = int(m[:-1])
                        # hour
                        elif m.endswith('h'):
                            hours = int(m[:-1])
                        # minute
                        elif m.endswith('m'):
                            minutes = int(m[:-1])
        else:
            # in longer format, uptime is expressed in form of
            # 32 week(s), 6 day(s), 10 hour(s), 39 minute(s)
            time_list = uptime_str.split(', ')
            for element in time_list:
                if re.search(r"year", element):
                    years = int(element.split()[0])
                elif re.search(r"w(ee)?k", element):
                    weeks = int(element.split()[0])
                elif re.search(r"day", element):
                    days = int(element.split()[0])
                elif re.search(r"h(ou)?r", element):
                    hours = int(element.split()[0])
                elif re.search(r"min(ute)?", element):
                    minutes = int(element.split()[0])

        return (years * YEAR_SECONDS) + (weeks * WEEK_SECONDS) + \
               (days * DAY_SECONDS) + (hours * HOUR_SECONDS) + \
               (minutes * MINUTE_SECONDS) + seconds

    def open(self):
        """Open a connection to the device."""
        self.device = self._netmiko_open(
            'dell_force10',
            netmiko_optional_args=self.netmiko_optional_args,
        )

    def close(self):
        """Close the connection to the device."""
        self._netmiko_close()

    def get_arp_table(self):
        """FTOS implementation of get_arp_table."""
        command = "show arp"
        arp_entries = self._send_command(command)
        arp_entries = textfsm_extractor(self, 'show_arp', arp_entries)

        table = []
        for idx, arp in enumerate(arp_entries):
            entry = {
                'interface': arp['interface'],
                'ip': ip(arp['ip']),
                'mac': mac(arp['mac']),
            }

            try:
                # age is given in minutes
                entry['age'] = float(arp['age']) * 60
            except ValueError:
                entry['age'] = -1.0

            table.append(entry)

        return table

    def get_bgp_neighbors_detail(self, neighbor_address=u''):
        """FTOS implementation of get_bgp_neighbors_detail."""
        cmd = ["show ip bgp neighbors"]
        if len(neighbor_address.strip()) > 0:
            cmd.append(neighbor_address)

        command = ' '.join(cmd)
        neighbors = self._send_command(command)
        neighbors = textfsm_extractor(self, 'show_ip_bgp_neighbors', neighbors)

        table = {}
        for idx, entry in enumerate(neighbors):
            # TODO: couldn't detect VRF from output
            vrf = u'global'

            if vrf not in table:
                table[vrf] = {}

            # set some flags
            entry['up'] = (entry['connection_state'] == 'ESTABLISHED')
            # unimplemented flags
            for k in ['local_address_configured', 'multihop', 'multipath',
                      'suppress_4byte_as', 'local_as_prepend', 'remove_private_as']:
                entry[k] = False

            # case some ip addresses
            for k in ['local_address', 'router_id', 'remote_address']:
                if len(entry[k].strip()) > 0:
                    entry[k] = ip(entry[k])

            # cast some integers
            for k in ['remote_as', 'local_port', 'remote_port', 'input_messages',
                      'output_messages', 'input_updates', 'output_updates',
                      'messages_queued_out', 'holdtime', 'keepalive',
                      'accepted_prefix_count', 'advertised_prefix_count',
                      'flap_count']:
                try:
                    entry[k] = int(entry[k])
                except ValueError:
                    entry[k] = -1

            # unimplemented integers
            for k in ['local_as', 'configured_holdtime', 'configured_keepalive',
                      'active_prefix_count', 'received_prefix_count',
                      'suppressed_prefix_count']:
                entry[k] = -1

            # unimplemented strings
            for k in ['export_policy', 'import_policy', 'last_event',
                      'previous_connection_state', 'routing_table']:
                entry[k] = u''

            # make sure all strings are unicode
            for k in entry.keys():
                if isinstance(entry[k], str):
                    entry[k] = unicode(entry[k])

            # add neighbor to table
            if entry['remote_as'] not in table[vrf]:
                table[vrf][entry['remote_as']] = []
            table[vrf][entry['remote_as']].append(entry)

        return table

    def get_config(self, retrieve='all'):
        """FTOS implementation of get_config."""
        config = {
            'startup':   u'',
            'running':   u'',
            'candidate': u'Not implemented for FTOS',  # not implemented
        }

        if retrieve in ['all', 'running']:
            config['running'] = self._send_command("show running-config")

        if retrieve in ['all', 'startup']:
            config['startup'] = self._send_command("show startup-config")

        return config

    def get_environment(self):
        """FTOS implementation of get_environment."""
        env = {
            'fans': {},
            'temperature': {},
            'power': {},
            'cpu': {},
            'memory': {
                'available_ram': 0,
                'used_ram': 0,
            },
        }

        # get fan data
        #

        # get sensor data
        environment = self._send_command("show environment stack-unit")
        environment = textfsm_extractor(self, 'show_environment_stack-unit', environment)
        for idx, entry in enumerate(environment):
            name = "Unit %d" % int(entry['unit'])
            # temperature
            env['temperature'][name] = {
                'temperature': float(entry['temperature']),
                'is_alert': (entry['temp_status'] != '2'),
                'is_critical': (entry['temp_status'] != '2')
            }
            # power
            env['power'][name] = {
                'status': (entry['volt_status'] == 'ok'),
                'capacity': -1.0,  # not implemented
                'output': -1.0,    # not implemented
            }

        # get CPU data
        processes = self._send_command("show processes cpu summary")
        processes = textfsm_extractor(self, 'show_processes_cpu_summary', processes)
        for idx, entry in enumerate(processes):
            env['cpu']["Unit %d" % int(entry['unit'])] = {
                '%usage': float(entry['omin']),
            }

        # get memory data
        memory = self._send_command("show memory")
        memory = textfsm_extractor(self, "show_memory", memory)
        for idx, entry in enumerate(memory):
            env['memory']['available_ram'] += int(entry['total'])
            env['memory']['used_ram'] += int(entry['used'])

        return env

    def get_facts(self):
        """FTOS implementation of get_facts."""
        # default values.
        facts = {
            'uptime': -1,
            'vendor': u'Dell EMC',
            'os_version': 'Unknown',
            'serial_number': 'Unknown',
            'model': 'Unknown',
            'hostname': 'Unknown',
            'fqdn': 'Unknown',
            'interface_list': [],
        }

        show_ver = self._send_command("show system stack-unit 0")

        # parse version output
        for line in show_ver.splitlines():
            if line.startswith('Up Time'):
                uptime_str = line.split(': ')[1]
                facts['uptime'] = self._parse_uptime(uptime_str)
            elif line.startswith('Mfg By'):
                facts['vendor'] = line.split(': ')[1].strip()
            elif ' OS Version' in line:
                facts['os_version'] = line.split(': ')[1].strip()
            elif line.startswith('Serial Number'):
                facts['serial_number'] = line.split(': ')[1].strip()
            elif line.startswith('Product Name'):
                facts['model'] = line.split(': ')[1].strip()

        # invoke get_interfaces and list interfaces
        facts['interface_list'] = self.get_interfaces().keys()

        # get hostname from running config
        config = self.get_config('running')['running']
        for line in config.splitlines():
            if line.startswith('hostname '):
                facts['hostname'] = re.sub('^hostname ', '', line)
                facts['fqdn'] = facts['hostname']
                break

        return facts

    def get_lldp_neighbors(self):
        """FTOS implementation of get_lldp_neighbors."""
        lldp = {}
        neighbors_detail = self.get_lldp_neighbors_detail()
        for intf_name, entries in neighbors_detail.items():
            lldp[intf_name] = []
            for lldp_entry in entries:
                hostname = lldp_entry['remote_system_name']
                lldp_dict = {
                    'port': lldp_entry['remote_port_description'],
                    'hostname': hostname,
                }
                lldp[intf_name].append(lldp_dict)

        return lldp

    def get_lldp_neighbors_detail(self, interface=''):
        """FTOS implementation of get_lldp_neighbors_detail."""
        if interface:
            command = "show lldp neighbors interface {} detail".format(interface)
        else:
            command = "show lldp neighbors detail"

        lldp_entries = self._send_command(command)
        lldp_entries = textfsm_extractor(self, 'show_lldp_neighbors_detail', lldp_entries)

        lldp = {}
        for idx, lldp_entry in enumerate(lldp_entries):
            # TODO: the current textfsm template keeps adding an empty entry at
            # the end of each interface and I couldn't fix it so at some point
            # it was just easier to get rid of these empty entries in code
            nonZero = False
            for key in lldp_entry.keys():
                # local_interface is set to Filldown so that is always filled
                if key == 'local_interface':
                    continue
                if len(lldp_entry[key].strip()) > 0:
                    nonZero = True
                    break
            if not nonZero:
                continue

            # get pretty interface name
            local_intf = _canonical_interface_name(lldp_entry.pop('local_interface'))

            # cast some mac addresses
            for k in ['remote_port', 'remote_chassis_id']:
                if len(lldp_entry[k].strip()) > 0:
                    lldp_entry[k] = mac(lldp_entry[k])

            # not implemented
            lldp_entry['parent_interface'] = u''

            lldp.setdefault(local_intf, [])
            lldp[local_intf].append(lldp_entry)

        return lldp

    def get_mac_address_table(self):
        """FTOS implementation of get_mac_address_table."""
        mac_entries = self._send_command("show mac-address-table")
        mac_entries = textfsm_extractor(self, 'show_mac-address-table', mac_entries)

        mac_table = []
        for idx, entry in enumerate(mac_entries):
            entry['mac'] = mac(entry['mac'])
            entry['interface'] = _canonical_interface_name(entry['interface'])
            entry['vlan'] = int(entry['vlan'])
            entry['static'] = (entry['static'] == 'Static')
            entry['active'] = (entry['active'] == 'Active')
            entry['moves'] = -1        # not implemented
            entry['last_move'] = -1.0  # not implemented

            mac_table.append(entry)

        return mac_table

    def _get_interfaces_detail(self):
        iface_entries = self._send_command("show interfaces")
        return textfsm_extractor(self, 'show_interfaces', iface_entries)

    def get_interfaces(self):
        """FTOS implementation of get_interfaces."""
        iface_entries = self._get_interfaces_detail()

        interfaces = {}
        for i, entry in enumerate(iface_entries):
            if len(entry['iface_name']) == 0:
                continue

            # init interface entry with default values
            iface = {
                'is_enabled':   False,
                'is_up':        False,
                'description':  entry['description'],
                'mac_address':  mac(entry['mac_address']),
                'last_flapped': 0.0,  # in seconds
                'speed':        0,    # in megabits
            }

            # set statuses
            if entry['admin_status'] == 'up':
                iface['is_enabled'] = True
            if entry['oper_status'] == 'up':
                iface['is_up'] = True

            # parse line_speed
            if re.search(r'bit$', entry['line_speed']):
                speed = entry['line_speed'].split(' ')
                if speed[1] == 'Mbit':
                    iface['speed'] = int(speed[0])
                # not sure if this ever occurs
                elif speed[1] == 'Gbit':
                    iface['speed'] = int(speed[0]*1000)

            # parse last_flapped
            iface['last_flapped'] = float(self._parse_uptime(entry['last_flapped'], True))

            # add interface data to dict
            local_intf = _canonical_interface_name(entry['iface_name'])
            interfaces[local_intf] = iface

        return interfaces

    def get_interfaces_counters(self):
        """FTOS implementation of get_interfaces_counters."""
        iface_entries = self._get_interfaces_detail()
        interfaces = {}
        key_map = [
            'rx_octets',
            ['rx_unicast', 'rx_unicast_packets'],
            ['rx_mcast',   'rx_multicast_packets'],
            ['rx_bcast',   'rx_broadcast_packets'],
            ['rx_dcard',   'rx_discards'],
            'tx_octets',
            ['tx_unicast', 'tx_unicast_packets'],
            ['tx_mcast',   'tx_multicast_packets'],
            ['tx_bcast',   'tx_broadcast_packets'],
            ['tx_dcard',   'tx_discards'],
        ]
        for idx, entry in enumerate(iface_entries):
            iface = {
                'rx_errors': 0,  # unimplemented
                'tx_errors': 0,  # unimplemented
            }
            for key in key_map:
                if (isinstance(key, types.ListType)):
                    src, dst = key
                else:
                    src = key
                    dst = key

                try:
                    iface[dst] = int(entry[src])
                except ValueError:
                    iface[dst] = 0

            # add interface data to dict
            local_intf = _canonical_interface_name(entry['iface_name'])
            interfaces[local_intf] = iface

        return interfaces

    def get_interfaces_ip(self):
        """FTOS implementation of get_interfaces_ip."""
        addr = {}

        # get IPv4 info
        ip_cmd = "show ip interface"
        ip_res = self._send_command(ip_cmd)

        # parse IP addresses
        iface = None
        for line in ip_res.splitlines():
            # interface line
            m = re.search(r'^(\w+( \d+(\/\d+)?)?) is \w+', line)
            if m:
                iface = m.group(1)
                continue

            # address line
            m = re.search(r'^Internet address is ([0-9\.]+)(?:\/(\d+))?', line)
            if m:
                if iface not in addr:
                    addr[iface] = {u'ipv4': {}}

                address = ip(m.group(1))
                mask = 32
                if m.group(2):
                    mask = int(m.group(2))
                    address = address.replace('/%d' % mask, '')
                addr[iface][u'ipv4'][address] = {
                    'prefix_length': mask
                }

        ip6_cmd = "show ipv6 interface brief"
        ip6_res = self._send_command(ip6_cmd)

        # parse IPv6 addresses
        iface = None
        for line in ip6_res.splitlines():
            # interface line
            m = re.search(r'^(\w+( \d+(\/\d+)?)?)\s+', line)
            if m:
                iface = m.group(1)
                continue

            # address line
            m = re.search(r'^\s*([a-f0-9:]+)(?:\/(\d+))?', line)
            if m:
                if iface not in addr:
                    addr[iface] = {u'ipv6': {}}
                elif u'ipv6' not in addr[iface]:
                    addr[iface][u'ipv6'] = {}

                address = ip(m.group(1))
                if m.group(2):
                    mask = int(m.group(2))
                    address = address.replace('/%d' % mask, '')
                elif re.search(r'^fe80', address):
                    mask = 64
                else:
                    mask = 128
                addr[iface][u'ipv6'][address] = {
                    'prefix_length': mask
                }

        return addr

    def _get_ntp_assoc(self):
        ntp_entries = self._send_command("show ntp associations")
        return textfsm_extractor(self, 'show_ntp_associations', ntp_entries)

    def get_ntp_peers(self):
        """FTOS implementation of get_ntp_peers."""
        entries = self._get_ntp_assoc()

        peers = {}
        for idx, entry in enumerate(entries):
            peers[ip(entry['remote'])] = {}

        return peers

    def get_ntp_servers(self):
        """FTOS implementation of get_ntp_servers."""
        return self.get_ntp_peers()

    def get_ntp_stats(self):
        """FTOS implementation of get_ntp_stats."""
        entries = self._get_ntp_assoc()
        stats = []
        for idx, entry in enumerate(entries):
            # cast ints
            for key in ['stratum', 'hostpoll', 'reachability']:
                try:
                    entry[key] = int(entry[key])
                except ValueError:
                    entry[key] = 0
            # cast floats
            for key in ['delay', 'offset', 'jitter']:
                try:
                    entry[key] = float(entry[key])
                except ValueError:
                    entry[key] = 0.0
            # cast ips
            for k in ['referenceid', 'remote']:
                if len(entry[k].strip()) > 0:
                    entry[k] = ip(entry[k])

            entry['synchronized'] = (entry['type'] == '*')
            stats.append(entry)

        return stats

    def get_snmp_information(self):
        """FTOS implementation of get_snmp_information."""
        command = "show running-config snmp"
        snmp = self._send_command(command)

        info = {
            'chassis_id': u'',  # not implemented
            'community':  {},
            'contact':    u'',
            'location':   u'',
        }

        for line in snmp.splitlines():
            if 'community' in line:
                m = re.search(r'^snmp-server community ([^\s]+) ([^\s]+)(?: ([^\s]+))?', line.strip())
                if not m:
                    continue
                com = {
                    'mode': m.group(2),
                    'acl':  u'N/A',
                }
                if m.group(3):
                    com['acl'] = m.group(3)

                info['community'][m.group(1)] = com

            elif 'location' in line:
                info['location'] = line.strip().replace('snmp-server location ', '').strip('"')
            elif 'contact' in line:
                info['contact'] = line.strip().replace('snmp-server contact ', '').strip('"')

        return info

    def get_users(self):
        """FTOS implementation of get_users."""
        command = "show running-config users"
        output = self._send_command(command)

        ptr = re.compile(r'^username ([^\s]+).+(?:sha256-)?password \d+ ([^\s]+) (?:privilege (\d+))?')
        users = {}
        for line in output.splitlines():
            m = ptr.search(line.strip())
            if not m:
                continue

            g = m.groups()
            user = {
                'password': g[1],
                'sshkeys':  [],
                'level':    0,
            }
            if g[2]:
                user['level'] = int(g[2])

            users[g[0]] = user

        return users

    def is_alive(self):
        """FTOS implementation of is_alive."""
        null = chr(0)
        if self.device is None:
            return {'is_alive': False}

        try:
            # Try sending ASCII null byte to maintain the connection alive
            self.device.write_channel(null)
            return {'is_alive': self.device.remote_conn.transport.is_active()}
        except (socket.error, EOFError):
            # If unable to send, we can tell for sure that the connection is unusable
            return {'is_alive': False}

        return {'is_alive': False}

    def ping(self, destination, source=u'', ttl=255, timeout=2, size=100, count=5, vrf=u''):
        """FTOS implementation of ping."""
        # build command string based on input
        cmd = ["ping"]
        if len(vrf.strip()) > 0:
            cmd.append("vrf %s" % vrf)
        cmd.append(destination)
        cmd.append("timeout %d" % timeout)
        cmd.append("datagram-size %d" % size)
        if len(source.strip()) > 0:
            cmd.append("source ip %s" % source)
        cmd.append("count %d" % count)

        command = ' '.join(cmd)
        result = self._send_command(command)

        # check if output holds an error
        m = re.search(r'% Error: (.+)', result)
        if m:
            return {
                'error': m.group(1)
            }

        # try to parse the output
        m = re.search(r'Success rate is [\d\.]+ percent \((\d+)\/(\d+)\).+ = (\d+)\/(\d+)\/(\d+)', result)
        if not m:
            return {
                'error': 'could not parse output',
            }

        g = m.groups()
        return {
            'success': {
                'probes_sent': int(g[1]),
                'packet_loss': int(g[1]) - int(g[0]),
                'rtt_min':     float(g[2]),
                'rtt_avg':     float(g[3]),
                'rtt_max':     float(g[4]),
                'rtt_stddev':  0.0,  # not implemented
                'results': [
                    {
                        'ip_address': ip(destination),
                        'rtt':        float(g[3]),
                    }
                ],
            }
        }

    def traceroute(self, destination, source=u'', ttl=255, timeout=2, vrf=u''):
        """FTOS implementation of traceroute."""
        # source, ttl and timeout are not implemented and therefore ignored
        cmd = ["traceroute"]
        if len(vrf.strip()) > 0:
            cmd.append("vrf %s" % vrf)
        cmd.append(destination)

        command = ' '.join(cmd)
        result = self._send_command(command)

        # check if output holds an error
        m = re.search(r'% Error: (.+)', result)
        if m:
            return {
                'error': m.group(1)
            }

        # process results of succesful traceroute
        result = textfsm_extractor(self, 'traceroute', result)
        trace = {}
        ttl = None
        for idx, entry in enumerate(result):
            if len(entry['ttl'].strip()) > 0 and ttl != int(entry['ttl']):
                ttl = int(entry['ttl'])
                trace[ttl] = {'probes': {}}
                ctr = 1

            # rewrite probes for easier splitting
            probes = re.sub(r'\s+', ' ', entry['probes'].replace('ms', '').strip())
            if len(probes) == 0:
                probes = []
            else:
                probes = probes.split(' ')

            for probe in probes:
                trace[ttl]['probes'][ctr] = {
                    'rtt': float(probe),
                    'ip_address': ip(unicode(entry['hop'])),
                    'host_name': unicode(entry['hop']),
                }
                ctr += 1

        return {
            'success': trace,
        }
