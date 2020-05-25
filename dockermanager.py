#
# Copyright (c) 2013-2017 - Adjacent Link LLC, Bridgewater, New Jersey
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

import os
import socket
import shutil
import stat
import sys
import time
import etce.utils

from etce.platform import Platform
from etce.config import ConfigDictionary
from etce.dockerplanfiledoc import DOCKERPlanFileDoc
from etce.dockererror import DOCKERError


def startdockers(dockerplan, writehosts=False, forcedockerroot=False, dryrun=False):
    dockerplanfiledoc = dockerplan

    if not type(dockerplan) == DOCKERPlanFileDoc:
        # assume file name
        dockerplanfiledoc = DOCKERPlanFileDoc(dockerplan)

    try:
        DOCKERManagerImpl().start(dockerplanfiledoc,
                               writehosts=writehosts,
                               forcedockerroot=forcedockerroot,
                               dryrun=dryrun)
    except Exception as e:
        raise DOCKERError(e.message)


def stopdockers(dockerplan):
    dockerplanfiledoc = dockerplan

    if not type(dockerplan) == DOCKERPlanFileDoc:
        # assume file name
        dockerplanfiledoc = DOCKERPlanFileDoc(dockerplan)

    try:
        DOCKERManagerImpl().stop(dockerplanfiledoc)
    except Exception as e:
        raise DOCKERError(e.message)



class DOCKERManagerImpl(object):
    def __init__(self):
        # check root
        #if not os.geteuid() == 0:
        #    raise RuntimeError('You need to be root to perform this command.')
        self._platform = Platform()


    def start(self, plandoc, writehosts, forcedockerroot=False, dryrun=False):
        hostname = socket.gethostname().split('.')[0].lower()
        dockerrootdir = plandoc.docker_root_directory(hostname)
        containers = plandoc.containers(hostname)

        if not containers:
            print 'No containers assigned to "%s". Skipping.' % hostname
            return
        
        if not dockerrootdir[0] == '/':
            print 'root_directory "%s" for hostname "%s" is not an absolute path. Quitting.' % \
                (dockerrootdir, hostname)
            return

        directory_level = len(dockerrootdir.split('/')) - 1
        if not directory_level >= 3:
            print 'root_directory "%s" for hostname "%s" is less than 3 levels deep. Quitting.' % \
                (dockerrootdir, hostname)
            return

        allowed_roots = ('tmp', 'opt', 'home', 'var', 'mnt')
        if not dockerrootdir.split('/')[1] in allowed_roots:
            print 'root_directory "%s" for hostname "%s" is not located in one of {%s} ' \
                'directory trees. Quitting.' % \
                (dockerrootdir, hostname, ', '.join(allowed_roots))
            return

        if dockerrootdir is None or len(containers) == 0:
            print 'No containers assigned to host %s. Quitting.' % hostname
            return

        # delete and remake the node root
        if os.path.exists(dockerrootdir):
            if forcedockerroot:
                print 'Force removal of "%s" docker root directory.' \
                    % dockerrootdir
                shutil.rmtree(dockerrootdir)
            else:
                raise DOCKERError('%s docker root directory already exists, Quitting.' % dockerrootdir)

        os.makedirs(dockerrootdir)

        # set kernelparameters
        kernelparameters = plandoc.kernelparameters(hostname)
        if len(kernelparameters) > 0:
            print 'Setting kernel parameters:'

            for kernelparamname,kernelparamval in kernelparameters.items():
                os.system('sysctl %s=%s' % (kernelparamname,kernelparamval))

        #vxlan tunnel
        if not dryrun:
            for _,vxlantunnel in plandoc.vxlantunnels(hostname).items():
                if not self._platform.isdeviceup('vxlan1'):
                    self._platform.runcommand('ip link add %s ' \
                                              'type vxlan id %s ' \
                                              'group 239.1.1.1 ' \
                                              'dev %s' % \
                                              (vxlantunnel.name,
                                               vxlantunnel.id,
                                               vxlantunnel.device))
                    self._platform.networkinterfaceup(vxlantunnel.name)

        # bring up bridge
        if not dryrun:
            for _,bridge in plandoc.bridges(hostname).items():
                if not bridge.persistent:
                    print 'Bringing up bridge: %s' % bridge.devicename

                    self._platform.dockerbridgeup(bridge.devicename,
                                                  bridge.subnet,
                                                  bridge.iprange,
                                                  bridge.gateway,
                                                  bridge.mtu,
                                                  bridge.addifs,
                                                  enablemulticastsnooping=True)
                    '''
                    if not bridge.ipv4 is None:
                        self._platform.adddeviceaddress(bridge.devicename,
                                                        bridge.ipv4)

                    if not bridge.ipv6 is None:
                        self._platform.adddeviceaddress(bridge.devicename,
                                                        bridge.ipv6)
                    '''

                    time.sleep(0.1)
                        
                elif not self._platform.isdeviceup(bridge.devicename):
                    raise RuntimeError('Bridge %s marked persistent is not up. Quitting.')

        # write hosts file
        if not dryrun:
            if writehosts:
                self._writehosts(containers)

        # create container files
        for container in containers:
            docker_directory = container.docker_directory

            self._makedirs(docker_directory)

            # make the config
            with open(os.path.join(docker_directory, 'config'), 'w') as configf:
                configf.write(str(container))

            # make init script
            filename,initscripttext = container.initscript

            if initscripttext:
                scriptfile = os.path.join(docker_directory, filename)

                with open(scriptfile, 'w') as sf:
                    sf.write(initscripttext)

                    os.chmod(scriptfile, 
                             stat.S_IRWXU | stat.S_IRGRP | \
                             stat.S_IXGRP | stat.S_IROTH | \
                             stat.S_IXOTH)

        if dryrun:
            print 'dryrun'
        else:
            self._startnodes(containers)


    def stop(self, plandoc):
        hostname = self._platform.hostname()

        noderoot = plandoc.docker_root_directory(hostname)

        for _, vxlantunnel in plandoc.vxlantunnels(hostname).items():
            if vxlantunnel.name in self._platform.getnetworkdevicenames():
                self._platform.networkinterfacedown(vxlantunnel.name)
                self._platform.networkinterfaceremove(vxlantunnel.name)

        for _,bridge in plandoc.bridges(hostname).items():

            if not bridge.persistent:
                print 'Bringing down bridge: %s' % bridge.devicename
                self._platform.dockerbridgedown(bridge.devicename)

        for container in plandoc.containers(hostname):
            command = 'docker rm -f %s &> /dev/null' % container.docker_name
            print command
            os.system(command)

        #os.remove(plandoc.planfile())


    def _makedirs(self, noderoot):
        os.makedirs(noderoot)


    def _startnodes(self, containers):
        for container in containers:
            image = ''
            params = ''
            for name,value in container.params:
                if name == 'image':
                    image = value
                else:
                    if '=' in name:
                        params += name + value + ' '
                    #else:
                    #    params += name + ' ' + value + ' '
            if image == '':
                raise RuntimeError('Image not defined. Quitting.')
            command = 'docker run ' \
                      '--detach ' \
                      '--tty ' \
                      '--name=%s ' \
                      '--hostname=%s ' \
                      '--cap-add=ALL ' \
                      '--privileged=true ' \
                      '--network=none ' \
                      '--volume %s ' \
                      '%s ' \
                      '%s > /dev/null' % \
                      (container.docker_name,
                       container.docker_name,
                       container.docker_directory,
                       params,
                       image)
            os.system(command)
            print command

            command = 'docker network disconnect -f none %s > /dev/null' % container.docker_name
            os.system(command)

            i = 0
            for bridgename, interfaceparams in container.interfaces.items():

                command = 'docker network connect --ip %s %s %s > /dev/null' % \
                           (interfaceparams['ipv4'], bridgename, container.docker_name)
                os.system(command)
                time.sleep(1)
                command = 'docker exec -t %s bash -c "ethtool -K eth%d tx off" > /dev/null' % \
                          (container.docker_name, i)
                os.system(command)
                i += 1

            

    def _waitstart(self, nodecount, dockerroot):
        numstarted = 0
        for i in range(10):
            command = 'docker ps --format "{{.Names}}" '
            numstarted = len(self._platform.runcommand(command))
            print 'Waiting for docker containers: %d of %d are running.' % \
                (numstarted, nodecount)

            if numstarted == nodecount:
                break

            time.sleep(1)

        print 'Continuing with %d of %d running docker containers.' % \
            (numstarted, nodecount)

        
    def _writehosts(self, containers):
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
        for i,line in enumerate(etcehostlines):
            if len(line.strip()) > 0:
                etcehostlines = etcehostlines[i:]
                break
        etcehostlines.reverse()

        with open('/etc/hosts', 'w') as ofd:
            for line in etcehostlines:
                ofd.write(line)

            ofd.write('\n')
            ofd.write(opentag)
            
            # ipv4
            ipv4_entries = []
            for container in containers:
                for hostentry,hostaddr in container.hosts_entries_ipv4:
                    ipv4_entries.append((hostentry,hostaddr))
            for hostentry,hostaddr in sorted(ipv4_entries):
                ofd.write('%s %s\n' % (hostaddr,hostentry))

            #ipv6 = []
            ipv6_entries = []
            for container in containers:
                for hostentry,hostaddr in container.hosts_entries_ipv6:
                    ipv6_entries.append((hostaddr,hostentry))
            for hostentry,hostaddr in sorted(ipv6_entries):
                ofd.write('%s %s\n' % (hostaddr,hostentry))

            ofd.write(closetag)
