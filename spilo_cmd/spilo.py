#!/usr/bin/env python
# -*- coding: utf-8 -*-
import click
import atexit
import getpass
import collections
import logging
import re
import sys
import boto
import os
import prettytable
import json
import dateutil
import time
import socket
import datetime
import subprocess
import yaml
import configparser

from clickclick import AliasedGroup, OutputFormat
from clickclick.console import print_table, format_time
import senza.cli
from senza.cli import get_region, check_credentials, get_stacks, resources, handle_exceptions, get_instance_health, \
    parse_time, watching
from dateutil import parser as dateutil_parser

STYLES = senza.cli.STYLES
TITLES = senza.cli.TITLES

STYLES['MASTER'] = {'fg': 'green'}
STYLES['REPLICA'] = {'fg': 'yellow'}

ec2 = None
Spilo = collections.namedtuple('Spilo', 'stack_name, version, dns, elb, instances, vpc_id, stack')

processed = False
PIUCONFIG = '~/.config/piu/piu.yaml'
if sys.platform == 'darwin':
    PIUCONFIG = '~/Library/Application Support/piu/piu.yaml'
PIUCONFIG = os.path.expanduser(PIUCONFIG)

option_port = click.option('-p', '--port', type=click.INT, help='The PostgreSQL port', envvar='PGPORT', default=5432)
option_log_level = click.option('--log-level', '--loglevel', help='Set the log level.', default='WARNING')
option_odd_host = click.option('--odd-host', help='Odd SSH bastion hostname')
option_odd_user = click.option('--odd-user', help='Username to use for OAuth2 authentication')
option_odd_config_file = click.option('--odd-config-file', help='Alternative odd config file',
                                      type=click.Path(exists=False), default=os.path.expanduser(PIUCONFIG))
option_pg_service_file = click.option('--pg_service-file', help='The PostgreSQL service file', envvar='PGSERVICEFILE',
                                      type=click.Path(exists=True))
option_region = click.option('--region', envvar='AWS_DEFAULT_REGION', metavar='AWS_REGION_ID',
                             help='AWS region ID (e.g. eu-west-1)')

cluster_argument = click.argument('cluster')


@click.group(cls=AliasedGroup)
def cli():
    """
    Spilo can connect to your Spilo cluster running inside a vpc. It does this using the stups infrastructure.
    """

    global processes
    global tunnels
    global options

    processes = dict()
    tunnels = dict()

    # # Ensure all are spawned processes will be cleaned in the end
    atexit.register(cleanup)


#    logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=options['loglevel'])

def process_options(opts):
    global options
    global odd_config
    global pg_service_name
    global pg_service
    global odd_config
    global processed

    if processed:
        logging.debug('We have already processed options')
        return

    processed = True

    options = opts

    logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=options['loglevel'])
    pg_service_name, pg_service = get_pg_service()

    if (pg_service_name or 'default') != 'default':
        options['cluster'] = pg_service['host']

    odd_config = load_odd_config()


def cleanup():
    for name in processes:
        if processes[name].returncode is None:
            logging.info('Terminating process {} (pid={})'.format(name, processes[name].pid))
            processes[name].kill()
    os.system('stty sane')


def libpq_parameters():
    parameters = dict()
    parameters['host'] = 'localhost'
    parameters['port'] = tunnels['postgres']

    if pg_service_name is not None:
        parameters['service'] = pg_service_name

    return parameters, ' '.join(['{}={}'.format(k, v) for (k, v) in parameters.items()])


@cli.command('connect', short_help='Connect using psql')
@cluster_argument
@option_port
@option_pg_service_file
@option_odd_config_file
@option_odd_user
@option_odd_host
@option_region
@option_log_level
@click.argument('psql_arguments', nargs=-1, metavar='[-- [psql OPTIONS]]')
def connect(**options):
    """Connects to the the cluster specified using psql"""

    process_options(options)

    tunnel_pid = open_tunnel()

    psql_cmd = ['psql', libpq_parameters()[1]]
    psql_cmd.extend(options['psql_arguments'])

    logging.debug('psql command: {}'.format(psql_cmd))

    psql = subprocess.Popen(psql_cmd)
    processes['psql'] = psql
    psql.wait()


@cli.command('healthcheck', short_help='Healthcheck')
@click.option('--watch', help='Keep watching every WATCH seconds')
@option_port
@option_pg_service_file
@cluster_argument
@click.argument('libpq_parameters', nargs=-1)
def healthcheck(**options):
    """Does a healthcheck on the given cluster"""

    pass


@cli.command('list', short_help='List available spilos')
@option_log_level
@option_region
@click.option('--tunnel', help='List only the established tunnels', is_flag=True, default=False)
@click.option('--details', help='Show more details', is_flag=True, default=False)
@click.option('--watch', help='Auto update the screen every X seconds', type=click.IntRange(1, 300), metavar='SECS')
@click.argument('clusters', nargs=-1)
def list_spilos(**options):
    process_options(options)

    if options['tunnel']:
        spilos = list()
    else:
        spilos = get_spilos(region=options['region'], clusters=options['clusters'], details=options['details'])

    processes = get_my_processes(options['clusters'])

    for _ in watching(w=False, watch=options['watch']):
        spilos = update_spilo_info(spilos)
        print_spilos(spilos)


def print_spilos(spilos):
    if len(spilos) == 0:
        return

    columns = [
        'cluster',
        'dns',
        'instance_id',
        'private_ip',
        'role',
        'launch_time',
    ]
    if spilos[0].instances is None:
        columns = ['cluster', 'dns']

    pretty_rows = list()

    for s in spilos:
        pretty_row = {'cluster': s.version}
        pretty_row['dns'] = ', '.join(s.dns)

        if s.instances is not None:
            for i in s.instances:
                pretty_row.update(i)
                pretty_rows.append(pretty_row.copy())

                # # Do not repeat general cluster information
                pretty_row = {'cluster': '', 'dns': ''}
        else:
            pretty_rows.append(pretty_row)

    print_table(columns, pretty_rows, styles=STYLES, titles=TITLES)


def re_search(needles=None, haystacks=None):
    """Searches a list of values for a list of regexp"""

    if needles is None or haystacks is None:
        return None

    if isinstance(needles, str):
        needles = [needles]

    if isinstance(haystacks, str):
        haystacks = [haystacks]

    for n in needles:
        rp = re.compile(n)
        for h in haystacks:
            if rp.search(h):
                return n, h

    return None


def get_spilo_resources(stack, cloud_formation_connection):
    if 'COMPLETE' in stack.stack_status and 'DELETE' not in stack.stack_status and 'ROLLBACK' not in stack.stack_status:
        resources = cloud_formation_connection.describe_stack_resources(stack.stack_name)

        # # We know it is a Spilo if it has a PostgresLoadBalancer
        for resource in resources:
            if resource.logical_resource_id == 'PostgresLoadBalancer':
                return resources

    logging.debug('Stack {} is not a spilo appliance'.format(stack.stack_name))
    return None

def update_spilo_info(spilos):
    global ec2
    global elb_conn

    new_spilos = list()

    for old_spilo in spilos:
        new_spilos.append( Spilo(old_spilo.stack_name, old_spilo.version, old_spilo.dns, old_spilo.elb, get_stack_instance_details(old_spilo.stack), old_spilo.vpc_id, old_spilo.stack) )
        
    return new_spilos    

def get_spilos(region, clusters=None, details=False):
    global ec2
    global elb_conn

    if len(clusters) == 0:
        clusters = None
    if isinstance(clusters, str):
        clusters = [clusters]

    region = get_region(region)
    check_credentials(region)
    if ec2 is None:
        ec2 = boto.ec2.connect_to_region(region)
    cf = boto.cloudformation.connect_to_region(region)
    elb_conn = boto.ec2.elb.connect_to_region(region)
    route53 = boto.route53.connect_to_region(region)

    zones = route53.get_zones()

    # # Getting a "DNSServerError: 400 Bad Request" when adding type='CNAME' to the below function call
    route53_records = route53.get_all_rrsets(hosted_zone_id=zones[0].id)

    cname_records = list()

    for rr in route53_records:
        if rr.type == 'CNAME':
            cname_records.append({'name': rr.name, 'resource_records': rr.resource_records})

    spilos = list()


    # # We try to do as little work as possible. Therefore we try to filter out non-matching stacks asap
    stacks = list()
    for stack in get_stacks(stack_refs=None, region=region, all=True):
        res = get_spilo_resources(stack, cf)
        if res is not None:
            stacks.append((stack, res))

    for stack, resources in stacks:
        stack_name = stack.name
        version = stack.version
        elb = None
        instances = None
        vpc_id = None
        dns = list()

        for resource in resources:
            if resource.logical_resource_id == 'PostgresLoadBalancer':
                info = elb_conn.get_all_load_balancers(load_balancer_names=[resource.physical_resource_id])[0]
                elb = {'name': info.name, 'dns_name': info.dns_name}
                vpc_id = info.vpc_id

                dns = [info.dns_name]

                for record in cname_records:
                    for rr in record['resource_records']:
                        if rr == info.dns_name:
                            dns.append(record['name'][:-1])

                if clusters is None or re_search(clusters, dns) or re_search(clusters, stack.version):
                    if details:
                        instances = get_stack_instance_details(stack)
                    if len(dns) > 1:
                        dns.pop(0)
                    spilos.append(Spilo(stack_name=resource.stack_name, version=stack.version, elb=elb,
                                  instances=instances, dns=dns, vpc_id=vpc_id, stack=stack))

    return spilos

def get_stack_instance_details(stack):
    global ec2
    global elb_conn

    instances_info = \
        ec2.get_only_instances(filters={'tag:aws:cloudformation:stack-id': stack.stack_id})
    instances_health = elb_conn.describe_instance_health(stack.stack_name)

    instances = list()
    for ii in instances_info:
        for ih in instances_health:
            if ih.instance_id == ii.id:
                instance = {'instance_id': ii.id, 'private_ip': ii.private_ip_address,
                            'launch_time': parse_time(ii.launch_time)}

                if ih.state == 'InService':
                    instance['role'] = 'MASTER'
                else:
                    instance['role'] = 'REPLICA'

                instances.append(instance)

    instances.sort(key=lambda k: k['role'])

    return instances


def parse_time(s: str) -> float:
    '''
    >>> parse_time('2015-04-14T19:09:01.000Z') > 0
    True
    '''
    try:
        utc = datetime.datetime.strptime(s, '%Y-%m-%dT%H:%M:%S.%fZ')
        ts = time.time()
        utc_offset = datetime.datetime.fromtimestamp(ts) - datetime.datetime.utcfromtimestamp(ts)
        local = utc + utc_offset
        return local.timestamp()
    except:
        return None

def list_tunnels(cluster_names=None):
    logging.debug('Cluster names: {}'.format(cluster_names))

    processes = get_my_processes(cluster_names)
    processes.sort(key=lambda k: k['cluster'])

    if len(processes) == 0:
        return

    columns = [
        'pid',
        'process',
        'cluster',
        'pgport',
        'patroniport',
        'dsn',
    ]
    table = prettytable.PrettyTable(columns)
    table.align['cluster'] = 'r'
    table.align['pid'] = 'r'
    table.align['pgport'] = 'r'
    table.align['patroniport'] = 'r'
    for p in processes:
        table.add_row([p[c] for c in columns])

    print(table)

def get_my_processes(cluster_names=None, commands=None):
    if cluster_names is not None and len(cluster_names) == 0:
        cluster_names = None
    if isinstance(cluster_names, str):
        cluster_names = [cluster_names]

    if commands is not None and len(commands) == 0:
        commands = None
    if isinstance(commands, str):
        commands = [commands]

    logging.debug('Cluster names: {}'.format(cluster_names))

    # # We do not use psutil for processes, as environment variables of processes is not
    # # available from it. We will just use good old ps for the task

    ps_cmd = [
        'ps',
        'e',
        '-eww',
        '-u',
        getpass.getuser(),
        '-A',
        '-o',
        'pid,command',
    ]

    ps_output = subprocess.check_output(ps_cmd, shell=False, stderr=subprocess.DEVNULL, env={'LANG': 'C'}).splitlines()
    ps_output.reverse()

    logging.debug('Examining {} processes'.format(len(ps_output)))

    processes = list()

    process_re = re.compile('^\s*(\d+)\s+([^\s]+).*SPILOCLUSTER=([^\s]+)')
    pgport_re = re.compile('SPILOPGPORT=(\d+)')
    patroniport_re = re.compile('SPILOPATRONIPORT=(\d+)')
    service_re = re.compile('SPILOSERVICE=(\w+)')

    # # We cannot disable the header on every ps (Mac OS X for example), the first line is a header)
    line = ps_output.pop().decode('utf-8')
    while len(ps_output) > 0:
        line = ps_output.pop().decode('utf-8')

        match = process_re.search(line)
        if match:
            logging.debug('Matched line: {}'.format(line[0:120]))
            process = dict()
            process['pid'] = match.group(1)
            process['process'] = match.group(2)
            process['cluster'] = match.group(3)

            match = process_re.match(line)

            process['pid'] = match.group(1)
            process['process'] = match.group(2)

            match = pgport_re.search(line)
            if match:
                process['pgport'] = match.group(1)

            match = patroniport_re.search(line)
            if match:
                process['patroniport'] = match.group(1)

            match = service_re.search(line)
            if match:
                process['service'] = match.group(1)

            logging.debug('Process: {}'.format(process))
        else:

            # logging.debug("Disregarding line: {}".format(line))
            pass

    for p in processes:
        p['dsn'] = '"host=localhost port={} service={}"'.format(p['pgport'], p['service'])

    logging.debug('Processes : {}'.format(pretty(processes)))
    return processes


@cli.command('tunnel', short_help='Create a tunnel')
@click.option('--background/--no-background', default=True, help='Push the tunnel in the background')
@cluster_argument
@option_port
@option_pg_service_file
@option_odd_config_file
@option_odd_user
@option_odd_host
@option_region
@option_log_level
def tunnel(**options):
    """Sets up a tunnel to use for connecting to Spilo"""

    global tunnels

    process_options(options)

    tunnel_pid = open_tunnel()

    if pg_service_name is None:
        pg_service_env = ''
    else:
        pg_service_env = 'export PGSERVICE={}'.format(pg_service_name)

    print("""
The ssh tunnel is running as a process with pid {pid}.

You can now connect to {cluster} using the following information:

"{dsn}"

Examples:

    psql "{dsn}"
    pg_dump "{dsn}"
    pg_basebackup -d "{dsn}" --pgdata=- --format=tar | gzip -4 > "{cluster}-backup.tar.gz"

Or you can set the environment so you connect using your chosen tool:

export PGHOST=localhost
export PGPORT={port}
{pgservice}

""".format(pid=tunnel_pid,
            cluster=options['cluster'], dsn=libpq_parameters()[1], port=tunnels['postgres'], pgservice=pg_service_env))

    list_tunnels(options['cluster'])

    sys.exit(0)


def pretty(something):
    return json.dumps(something, sort_keys=True, indent=4)


def get_pg_service():
    """Reads all the services from all the pg service files it can find"""

    # # http://www.postgresql.org/docs/current/static/libpq-pgservice.html
    # #
    # # There are some precedence rules which we want to honour.

    if options.get('cluster') is None:
        return None, dict()

    filenames = list()

    if options['pg_service_file'] is not None:
        filenames.append(options['pg_service_file'])
    else:
        filenames.append('~/.pg_service.conf')
        filenames.append('~/pg_service.conf')
        if filenames.append(os.environ.get('PGSYSCONFDIR')) is not None:
            filenames.append(os.environ.get('PGSYSCONFDIR') + '/pg_service.conf')
        filenames.append('/etc/pg_service.conf')

    filenames = [os.path.expanduser(f) for f in filenames if f is not None]

    logging.debug(pretty(options))

    defaults = dict()
    defaults['port'] = options['port']
    defaults['host'] = options['cluster']

    parser = configparser.ConfigParser(defaults=defaults)

    services = [options['cluster'], 'spilo']
    parsed = parser.read(filenames)
    logging.debug('Read pg_service definitions from the following files: {}'.format(parsed))

    pg_service = dict()

    for service in services:
        if parser.has_section(service):
            logging.debug('Using service definition [{}]'.format(service))
            return service, dict(parser.items(service, raw=True))

    return None, dict(parser.items('DEFAULT', raw=True))


def load_odd_config():
    if options.get('odd_config_file') is None:
        return dict()

    if os.path.isfile(options['odd_config_file']):
        with open(options['odd_config_file'], 'r') as f:
            odd_config = yaml.load(f)
        logging.debug('Loaded odd configuration from {}:\n{}'.format(options['odd_config_file'], pretty(odd_config)))
    else:
        odd_config = dict()

    odd_config['user_name'] = options.get('odd_user') or odd_config.get('user_name')
    odd_config['odd_host'] = options.get('odd_host') or odd_config.get('odd_host')

    return odd_config


def open_tunnel():
    cluster = '^' + options['cluster']
    p = get_my_processes(cluster_names=cluster + '$', commands=['ssh'])
    if len(p) == 1:
        logging.info('Found a tunnel which is available: {}'.format(pretty(p)))
        tunnels['postgres'] = p[0]['pgport']
        tunnels['patroni'] = p[0]['patroniport']
        return

    spilos = get_spilos(options['region'], [cluster])

    if len(spilos) == 0:
        raise Exception('Could not find a spilo cluster beginning with {}'.format(options['cluster']))

    if len(spilos) > 1:
        print_spilos(spilos)
        raise Exception('Multiple candidates starting with {}'.format(options['cluster']))

    p = get_my_processes(cluster_names=cluster)
    if len(p) > 0:
        logging.info('Found a tunnel which is available: {}'.format(pretty(p)))
        tunnels['postgres'] = p[0]['pgport']
        tunnels['patroni'] = p[0]['patroniport']
        return p[0]['pid']

    cluster = spilos[0]
    logging.info("Didn't find a tunnel available")
    # # We open 2 sockets and let the OS pick a free port for us
    # # later on we will use these ports for portforwarding
    pg_socket = socket.socket()
    pg_socket.bind(('', 0))
    tunnels['postgres'] = int(pg_socket.getsockname()[1])
    logging.debug('Postgres tunnel port: {}'.format(tunnels['postgres']))

    patroni_socket = socket.socket()
    patroni_socket.bind(('', 0))
    tunnels['patroni'] = int(patroni_socket.getsockname()[1])
    logging.debug('tunnel port: {}'.format(tunnels['postgres']))

    ssh_cmd = ['ssh']
    if odd_config['user_name'] is not None:
        ssh_cmd += ['{}@{}'.format(odd_config['user_name'], odd_config['odd_host'])]
    else:
        ssh_cmd += [odd_config['odd_host']]

    logging.debug('Testing ssh access using cmd:{}'.format(ssh_cmd))
    test = subprocess.check_output(ssh_cmd + ['printf t3st'], shell=False, stderr=subprocess.DEVNULL)
    if test != b't3st':
        logging.error('Could not setup a working tunnel. You may need to request access using piu')
        raise Exception(str(test))

    env = os.environ.copy()
    env['SPILOCLUSTER'] = options['cluster']
    env['SPILOSERVICE'] = pg_service_name
    env['SPILOVPCID'] = cluster.vpc_id

    # # We will close the opened socket as late as possible, to prevent other processes from occupying this port
    patroni_socket.close()
    pg_socket.close()

    ssh_cmd.append('-L')
    logging.debug(pg_service)

    env['SPILOPGPORT'] = str(tunnels['postgres'])
    port = str(pg_service['port'] or options['port'])
    ssh_cmd.append('{}:{}:{}'.format(tunnels['postgres'], cluster.elb['dns_name'], str(port)))

    ssh_cmd.append('-L')
    env['SPILOPATRONIPORT'] = str(tunnels['patroni'])
    port = 8008
    ssh_cmd.append('{}:{}:{}'.format(tunnels['patroni'], pg_service['host'], str(port)))

    ssh_cmd.append('-N')

    logging.info('Setting up tunnel command: {}'.format(ssh_cmd))

    tunnel = subprocess.Popen(ssh_cmd, shell=False, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, env=env)
    tunnel.poll()
    if tunnel.returncode is not None:
        raise Exception('Tunnel not running anymore, exitcode tunnel: {}'.format(tunnel.returncode))

    # # Wait for the tunnel to be available
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    conn_test = '127.0.0.1', tunnels['postgres']
    result = sock.connect_ex(conn_test)

    timeout = 5
    epoch_time = time.time()
    threshold_time = time.time() + timeout

    # # Loop until connection is established
    while result != 0:
        time.sleep(0.1)
        result = sock.connect_ex(conn_test)
        if time.time() > threshold_time:
            raise Exception('Tunnel was not established within timeout of {} seconds'.format(timeout))
            break

    sock.close()

    logging.debug('Established connectivity on tunnel after {} seconds'.format(time.time() - epoch_time))

    if not options.get('background', False):
        processes['tunnel'] = tunnel

    return tunnel.pid


if __name__ == '__main__':
    handle_exceptions(cli)()