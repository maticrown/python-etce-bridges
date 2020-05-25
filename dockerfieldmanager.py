#!/usr/bin/env python
#
# Copyright (c) 2013-2018 - Adjacent Link LLC, Bridgewater, New Jersey
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions
# are met:
#
# * Redistributions of source code must retain the above copyright
#   notice, this list of conditions and the following disclaimer.
# * Redistributions in binary form must reproduce the above copyright
#   notice, this list of conditions and the following disclaimer in
#   the documentation and/or other materials provided with the
#   distribution.
# * Neither the name of Adjacent Link LLC nor the names of its
#   contributors may be used to endorse or promote products derived
#   from this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
# "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
# LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS
# FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE
# COPYRIGHT OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT,
# INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING,
# BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
# LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
# CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT
# LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN
# ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.
#

import argparse
import os
import shutil
import sys
from netaddr import IPNetwork, IPAddress

from etce.clientbuilder import ClientBuilder
from etce.config import ConfigDictionary
from etce.dockererror import DOCKERError
from etce.dockermanager import startdockers,stopdockers
from etce.dockerplanfiledoc import DOCKERPlanFileDoc
from etce.platform import Platform
import etce.utils
from etce.parserpl import RFMatrix
from etce.field import Field


def startfield(args):
    this_hostname = Platform().hostname()

    config = ConfigDictionary()

    workdir = config.get('etce', 'WORK_DIRECTORY')

    workdir = os.getenv('WORKDIR', workdir)

    if not os.path.exists(workdir):
        raise DOCKERError('ETCE WORK_DIRECTORY "%s" not found. ' \
                       'Please create it before starting.' % workdir)

    if args.dockerplanfile:
        dockerplanfile = args.dockerplanfile
    else:
        dockerplanfile = os.path.join(workdir, 'dockerplan.xml')

    plandoc = DOCKERPlanFileDoc(dockerplanfile)

    # lockfile
    lockfilename = \
        os.path.join(workdir, 'etce.docker.lock')

    if os.path.isfile(lockfilename):
        err = 'Detected an active docker field with root at: %s. ' \
              'Run "etce-docker stop" first.' % \
              plandoc.docker_root_directory(this_hostname)
        raise DOCKERError(err)

    cidr = os.getenv('CIDR', '10.99.0.0/16')
    containers = []
    for hostname,_ in plandoc.hostnames():
        for container in plandoc.containers(hostname):
            for bridgename, interfaceparams in container.interfaces.items():
                if IPAddress(interfaceparams['ipv4']) in IPNetwork(cidr):
                    containers.append((container.docker_name, interfaceparams['ipv4']))
                    break


        ipexist = []
    for _,ip in containers:
        ipexist.append(ip)
    my_ip = ''
    for ip in IPNetwork(cidr)[1:]:
        if not str(ip) in ipexist:
            my_ip = str(ip)
            break

    my_ip = my_ip + '/' + cidr.split('/')[1]

    # write to /etc/hosts in container/machine controller all external ip
    writehosts(plandoc, containers)

    hostfile = \
        os.path.join(workdir, 'hosts')

    if not args.dryrun:
        shutil.copy(dockerplanfile, lockfilename)
        shutil.copy('/etc/hosts', hostfile)

    startdockers(plandoc,
                 args.writehosts,
                 args.forcedockerroot,
                 args.dryrun)

    other_hosts = []

    for hostname,ip in plandoc.hostnames():
        if hostname != (this_hostname and 'localhost'):
            other_hosts.append(hostname)

    # start containers on other hosts, if any
    if other_hosts:
        client = None
        try:
            client = ClientBuilder().build(\
                        other_hosts,
                        user=args.user,
                        port=args.port,
                        password=args.password)

            # push the file and execute
            client.put(dockerplanfile, '.', other_hosts, doclobber=True)

            # push the file
            client.put('/etc/hosts', '.', other_hosts, doclobber=True)

            # on the destination node the netplan file gets pushed to the
            # ETCE WORK_DIRECTORY
            command = 'dockermanager startdockers %s writehosts=%s forcedockerroot=%s' \
                      % (os.path.basename(dockerplanfile),
                         args.writehosts,
                         args.forcedockerroot)

            ret = client.execute(command,
                                 other_hosts)

            for k in ret:
                print '[%s] return: %s' % (k, ret[k].retval['result'])

        finally:
            if client:
                client.close()

                # A valid ETCE Test Directory.
                TESTDIRECTORY = os.path.join(workdir, 'pub-tdmact')

                # The output directory to place the built Test Directory.
                TESTROOT = os.path.join(workdir, TESTDIRECTORY + '_' + etce.utils.timestamp())

                os.system('etce-test publish %s %s --verbose' %
                          (TESTDIRECTORY, TESTROOT))

                # A user tag to prepend to the name of each test result directory.
                TESTPREFIX = 'tdmact'
                # Run scenario order steps
                #if not args.collect:
                os.system('etce-test run --user root --policy autoadd -v --kill before --nocollect %s %s %s' %
                          (TESTPREFIX, HOSTFILE, TESTROOT))
                #else:
                #    os.system('etce-test run --user root --policy autoadd -v %s %s %s' %
                #              (TESTPREFIX, HOSTFILE, TESTROOT))



def stopfield(args):
    workdir = ConfigDictionary().get('etce', 'WORK_DIRECTORY')

    workdir = os.getenv('WORKDIR', workdir)

    lockfilename = os.path.join(workdir, 'etce.docker.lock')

    if not os.path.exists(lockfilename) or not os.path.isfile(lockfilename):
        raise DOCKERError('Lockfile "%s" not found. Quitting.' % lockfilename)

    if args.dockerplanfile:
        dockerplanfile = args.dockerplanfile
    else:
        dockerplanfile = os.path.join(workdir, 'dockerplan.xml')

    plandoc = DOCKERPlanFileDoc(dockerplanfile)

    this_hostname = Platform().hostname()

    other_hosts = []

    for hostname,ip in plandoc.hostnames():
        if hostname != (this_hostname and 'localhost'):
            other_hosts.append(hostname)
    
    # stop containers on other hosts, if any
    try:
        if other_hosts:
            if args.collect:
                client_nodes = None
                try:
                    print
                    'Collecting results.'

                    time = 'collect_on_%s' % etce.timeutils.getstrtimenow().split('.')[0]

                    localtestresultsdir = os.path.join(workdir, 'data', time)

                    field = Field(os.path.join(workdir, 'HOSTFILE'))

                    # root nodes host the filesystem for all of the virtual nodes attached
                    filesystemnodes = list(field.roots())

                    testdir = 'data'

                    client_nodes = ClientBuilder().build(filesystemnodes,
                                                         user=args.user,
                                                         port=args.port,
                                                         password=args.password,
                                                         policy=args.policy)
                    try:

                        client_nodes.collect(testdir,
                                             localtestresultsdir,
                                             filesystemnodes)
                    except:
                        pass

                finally:
                    if client_nodes:
                        client_nodes.close()

            client = None
            try:
                client = ClientBuilder().build(other_hosts,
                                               user=args.user,
                                               port=args.port,
                                               password=args.password)

                # push the file and execute
                client.put(lockfilename, '.', other_hosts, doclobber=True)
                # on the destination node the netplan file gets pushed to the
                # ETCE WORK_DIRECTORY
                command = 'dockermanager stopdockers %s' % os.path.basename(dockerplanfile)

                ret = client.execute(command, other_hosts)

                for k in ret:
                    print '[%s] return: %s' % (k, ret[k].retval['result'])
            finally:
                if client:
                    client.close()

    finally:
 #       os.system('ip link del vxlan1')
        stopdockers(plandoc)
        os.system('rm -f %s' % lockfilename)

def writehosts(plandoc, containers):
    opentag = '#### Start auto-generated ETCE control mappings\n'
    closetag = '#### Stop auto-generated ETCE control mappings\n'
    etcehostlines = []
    searchstate = 0
    for line in open('/etc/hosts', 'r'):
        if searchstate == 0:
          if line.startswith(opentag):
                searchstate = 1
          else:
                etcehostlines.append(line)
        elif searchstate == 1:
            if line.startswith(closetag):
                searchstate == 2
        else:
            etcehostlines.append(line)

    # strip off trailing white spaces
    etcehostlines.reverse()
    for i, line in enumerate(etcehostlines):
        if len(line.strip()) > 0:
            etcehostlines = etcehostlines[i:]
            break
    etcehostlines.reverse()

    with open('/etc/hosts', 'w') as ofd:
        for line in etcehostlines:
           ofd.write(line)

        ofd.write('\n')
        ofd.write(opentag)

        for hostname, ip in plandoc.hostnames():
            ofd.write('%s %s\n' % (ip, hostname))

        # ipv4
        for hostentry, hostaddr in sorted(containers):
            ofd.write('%s %s\n' % (hostaddr, hostentry))

        ofd.write(closetag)

def createhostfile(containers, HOSTFILE):
    with open(HOSTFILE, 'w') as f:
        for hostentry,_ in sorted(containers):
            f.write('%s\n' % hostentry)

def main():
    parser = argparse.ArgumentParser(prog='etce-docker')

    parser.add_argument('--port',
                        action='store',
                        type=int,
                        default=None,
                        help='''If the DOCKERPLANFILE contains remote host(s),
                        connect to the hosts via the specified port. If not
                        specified, ETCE will look for the host's "Port" value
                        in the ~/.ssh/config file. If not found, uses the default
                        ssh port value, 22.''')
    parser.add_argument('--user',
                        action='store',
                        default=None,
                        help='''If the DOCKERPLANFILE contains remote host(s),
                        connect to the hosts as the specified user. If not
                        specified, ETCE will look for the host's "User" value
                        in the ~/.ssh/config file. If not found, uses the
                        current user''')
    parser.add_argument('--password',
                        action='store',
                        default=None,
                        help='''Use password if exist''')
    parser.add_argument('--policy',
                        action='store',
                        choices=['reject', 'warning', 'autoadd'],
                        default='reject',
                        help='''Specify the policy to use when a target
                        host is not listed in the local "known_hosts" file.
                        Default: reject''')

    subparsers = parser.add_subparsers()

    parser_start = \
        subparsers.add_parser('start', 
                              help='Start a network of DOCKER container and Linux bridges' \
                              'based on the description in the DOCKER plan file.')

    parser_start.add_argument('--dryrun',
                              action='store_true',
                              default=False,
                              help='''Create the container configurations but do not start
                              the containers.''')
    parser_start.add_argument('--forcedockerroot',
                              action='store_true',
                              default=False,
                              help='''Force the deletion of the dockerroot directory
                              if it already exists.''')
    parser_start.add_argument('--writehosts',
                              action='store_true',
                              default=False,
                              help='''Add an /etc/hosts entry for interface elements in the
                              DOCKERPLANFILE that contain a hosts_entry_ipv4 or hosts_entry_ipv6 
                              attribute. The has form "ipv4 hosts_entry_ipv4" or
                              "ipv6 hosts_entry_ipv6".''')
    parser_start.add_argument('--runsteps',
                              action='store_true',
                              default=False,
                              help='''Run immediately steps of scenario''')
    parser_start.add_argument('dockerplanfile',
                              metavar='DOCKERPLANFILE',
                              action='store',
                              nargs='?',
                              help='The DOCKER plan file')

    parser_start.set_defaults(func=startfield)

    parser_stop = \
        subparsers.add_parser('stop', 
                              help = 'Stop the DOCKER container network previously started with ' \
                              '"etce-docker start"')

    parser_stop.add_argument('--collect',
                              action='store_true',
                              default=False,
                              help='''On test completion, make the collection step that 
                              retrieves test data from the field nodes to the localhost.
                              Default: no collect.''')
    parser_stop.add_argument('dockerplanfile',
                              metavar='DOCKERPLANFILE',
                              action='store',
                              nargs='?',
                              help='The DOCKER plan file')

    parser_stop.set_defaults(func=stopfield)

    args = parser.parse_args()

    try:
        args.func(args)
    except DOCKERError as e:
        print
        print e
        print

if __name__=='__main__':
    main()
