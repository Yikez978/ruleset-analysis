#!/usr/bin/env python
#
# Mapper job for firewall log analysis
#
import sys
sys.path.append('lib/fw-regex')
import os
import re
import shelve
import os.path
from libfwregex import get_builtconn
from firewallrule import FirewallRule

DEBUG=False

CONFIGFILE = 'config.py'

# Load config file
try:
    config = {}
    execfile(CONFIGFILE, config)
except:
    sys.stderr.write('Unable to load config file ({0})! Aborting.\n'.format(CONFIGFILE))
    sys.exit(1)

class Connection(FirewallRule):
    '''A connection object with some extra attributes compared to a firewall rule.
    
    Attributes:
        firewall: String - hostname of firewall reporting this connection.
        logline: String - original log line where the connection was reported
        timestamp: String - date and time of connection creation
        direction: String - 'in' or 'out' from the firewall's perspective.
        interface_in: String - name of firewall interface the connection came in on.
        interface_out: String - name of firewall interface used to reach the destination.

    Inherited from FirewallRule:
        allowed, protocol, src, dst, sport, dport.

        Set allowed to True if the connection is permitted and False if the connection
        is being blocked by the firewall.
    '''

    def __init__(self, firewall, logline, timestamp, direction, interface_in, interface_out, allowed, protocol, src, dst, sport, dport):
        self.firewall = firewall
        self.logline = logline
        self.timestamp = timestamp
        self.direction = direction
        self.interface_in = interface_in
        self.interface_out = interface_out
        FirewallRule.__init__(self, allowed, protocol, logline, src, dst, sport, dport)

    def serialize(self):
        '''Return a string representation of connection object better suited for transfer to reducers.'''
        allowed = str(self.action)
        src = str(self.src)
        dst = str(self.dst)
        sport = str(self.sport[0])  # Safe to assume only one element in list, since a connection has exactly one source port
        dport = str(self.dport[0])  # Same goes for destination port
        return ';'.join([self.firewall, self.logline, self.timestamp, self.direction, self.interface_in, self.interface_out, allowed, self.protocol, src, dst, sport, dport])

    def empty(self):
        '''Reset all attributes to None to prepare for object reuse.'''
        self.firewall = None
        self.logline = None
        self.timestamp = None
        self.direction = None
        self.interface_in = None
        self.interface_out = None
        self.allowed = None
        self.protocol = None
        self.original = None
        self.src = None
        self.dst = None
        self.sport = None
        self.dport = None


# Open database of firewall rules
try:
    acldb = shelve.open(config['ACCESSLIST_DATABASE_FILENAME'])
except:
    sys.stderr.write('Unable to open access-list database ' + \
        '("{0}"). '.format(config['ACCESSLIST_DATABASE_FILENAME']) + \
        'Did you remember to run preprocessor? Aborting mapper.\n')
    sys.exit(1)

# Read from database and close file
try:
    accesslists = acldb['accesslists']
    firewalls = acldb['firewalls']
    acldb.close()
except KeyError as e:
    sys.stderr.write('Unable to load key {0} from access-list database. '.format(e) + \
        'Did you remember to run preprocessor? Aborting mapper.\n')
    sys.exit(1)
except TypeError as e:
    sys.stderr.write('Unable to load keys from access-list database. ' + \
        'Did you remember to run preprocessor? Aborting mapper.\n')
    sys.exit(1)


# Get hostname from directory name of input file
#
# Note: Expects Mapper input to be files in a directory structure like /<top-level>/<hostname>/<filename>
#       Adjust split and index at end of hostname line below if hostname is some other part of the string.
try:
    hostname = os.environ['mapred_input_dir'].split('/')[-2]
    if DEBUG:
        print 'Got hostname ' + hostname + ' from mapred_input_dir ' + os.environ['mapred_input_dir']
except KeyError, e:
    raise KeyError('Unable to determine hostname from mapred_input_dir! Environment variable not found: ' + str(e))

# Validate prerequisites for processing
if hostname not in firewalls or hostname not in accesslists:
    print('Firewall {0} not present in data structure. Aborting.'.format(hostname))
    sys.exit(1)

# Connection object pointer
conn = None

# Process each line of input
for line in sys.stdin:
    data = get_builtconn(line)
    if data:
        # Create a timestamp
        if data['year']:
            timestamp = data['year'] + '-' + data['month'] + '-' + data['day'].zfill(2) + ' ' + data['time']
        else:
            # This timestamp is incomplete, no year info makes it impossible to piece a complete time together
            timestamp = data['month'] + '-' + data['day'].zfill(2) + ' ' + data['time']

        # Save interesting info
        allowed = True      # Because the log message is about Built Connections
        protocol = data['protocol'].lower()

        # Create or re-use connection object
        if not conn:
            conn = Connection(hostname, line, timestamp, data['direction'], data['interface_in'], data['interface_out'], allowed, protocol.lower(), data['src'], data['dst'], data['sport'], data['dport'])
        else:
            conn.empty()
            conn.__init__(hostname, line, timestamp, data['direction'], data['interface_in'], data['interface_out'], allowed, protocol.lower(), data['src'], data['dst'], data['sport'], data['dport'])

        # Find relevant access-list
        if data['interface_in'] in firewalls[hostname]:
            acl = firewalls[hostname][data['interface_in']]['in']       # Only support for access-lists applied inbound to an interface at the moment
        else:
            # No ACL on the incoming interface, skip line since there is no ACL to compare traffic against
            continue

        # Validate info
        if acl not in accesslists[hostname]:
            # Print error and skip line
            print('Unable to process line because access-list {0} is missing from data structure for host {1}, skipping line.'.format(acl, hostname))
            print('The skipped line is: {0}'.format(line))
            continue

        # Find relevant rules for this connection
        if protocol in ['tcp', 'udp']:
            if protocol in accesslists[hostname][acl]['protocols']:
                relevantrules = accesslists[hostname][acl]['protocols'][protocol] + accesslists[hostname][acl]['protocols']['ip']
                relevantrules.sort()
            else:
                relevantrules = accesslists[hostname][acl]['protocols']['ip']
        else:
            relevantrules = accesslists[hostname][acl]['protocols'][protocol]

        for ruleindex in relevantrules:
            # Check if connection would be permitted by this rule
            if conn in accesslists[hostname][acl]['rules'][ruleindex]:
                if DEBUG:
                    rule = accesslists[hostname][acl]['rules'][ruleindex]
                    print('MATCH on firewall rule {0} of {1} for {2}'.format(rule.rulenum, acl, hostname))
                    print('Connection   : {0}'.format(conn.logline))
                    print('Firewall rule: {0}'.format(rule.original))
                    print('Firewall rule: {0}'.format(repr(rule)))
                    print('')

                # Report to reducer
                #
                # Key is enough to uniquely identify the firewall rule that was matched
                # Value is the original logline that matched
                #
                key = ';'.join([hostname, acl, str(ruleindex)])
                value = conn.logline
                print('\t'.join([key, value]))

                # Don't check for more matching rules for this connection
                break

