#!/usr/local/bin/python2.7

#!/usr/bin/env python

"""
The MIT License (MIT)

Copyright (c) 2015 Maker Musings

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
"""

# For a complete discussion, see http://www.makermusings.com

import email.utils
import requests
import select
import socket
import struct
import sys
import os
import time
import urllib
import uuid



echo_device_limit = 16
base_port=54900

isyuser = os.getenv('ISY_USER', "admin")
isypass = os.getenv('ISY_PASS', "admin")
#isyaddr = os.getenv('ISY_ADDR', None)
isyaddr = os.getenv('ISY_ADDR', "10.1.1.36")

# DOF DON DIM BRT
#  /rest/nodes/<node-id>/cmd/<cmd>>/<cmd value>
# /rest/nodes/16 3F E5 1/cmd/DOF
isydevs = {
    "office light"     : "16 3F E5 1" ,
    "garage"     : "20326",
    "floor lamp" : "32468",
    "bathroom light" : "17 54 69 1",
    "bathroom Fan"   : "17 50 F4 1", 
    "desk light"   : "16 D3 73 1",
    "backyard lights" : "FF 03 0F 2",
    "bedtime mode" : "53278",
}

isydevslist = [
    ["office light", "16 3F E5 1"],
    ["garage", "20326"],
    ["floor lamp", "32468"],
]

# /rest/programs/<pgm-id>/<pgm-cmd>
# Valid Commands : 'run', 'runThen', 'runElse', 'stop', 'enable', 'disable', 'enableRunAtStartup', 'disableRunAtStartup'


isyprog = {
    "bath light" : {
        "on"  : ("0075", "runThen"),
        "off" : ("002B", "runThen"),
        },
    "bath fan" : {
        "on"  : ("0074", "runThen"),
        "off" : ("0073", "runThen"),
        },
    "garage all" : {
        "on"  : ("0082", "runThen"),
        "off" : ("003C", "runThen"),
        },
    "garage beep" : {
        "on"  : ("002F", "runThen"),
        "off" : ("006C", "runThen"),
        },
    "computer" : {
        "on"  : ("0083", "runThen"),
        "off" : ("0083", "runThen"),
        },

}

# This XML is the minimum needed to define one of our virtual switches
# to the Amazon Echo

SETUP_XML = """<?xml version="1.0"?>
<root>
  <device>
    <deviceType>urn:MakerMusings:device:controllee:1</deviceType>
    <friendlyName>%(device_name)s</friendlyName>
    <manufacturer>Belkin International Inc.</manufacturer>
    <modelName>Emulated Socket</modelName>
    <modelNumber>3.1415</modelNumber>
    <UDN>uuid:Socket-1_0-%(device_serial)s</UDN>
  </device>
</root>
"""


DEBUG = False

def dbg(msg):
    global DEBUG
    if DEBUG:
        print msg
        sys.stdout.flush()


# A simple utility class to wait for incoming data to be
# ready on a socket.

class poller:
    def __init__(self):
        if 'poll' in dir(select):
            self.use_poll = True
            self.poller = select.poll()
        else:
            self.use_poll = False
        self.targets = {}

    def add(self, target, fileno=None):
        if not fileno:
            fileno = target.fileno()
        if self.use_poll:
            self.poller.register(fileno, select.POLLIN)
        self.targets[fileno] = target

    def remove(self, target, fileno = None):
        if not fileno:
            fileno = target.fileno()
        if self.use_poll:
            self.poller.unregister(fileno)
        del(self.targets[fileno])

    def poll(self, timeout = 0):
        if self.use_poll:
            ready = self.poller.poll(timeout)
        else:
            ready = []
            if len(self.targets) > 0:
                (rlist, wlist, xlist) = select.select(self.targets.keys(), [], [], timeout)
                ready = [(x, None) for x in rlist]
        for one_ready in ready:
            target = self.targets.get(one_ready[0], None)
            if target:
                target.do_read(one_ready[0])
 

# Base class for a generic UPnP device. This is far from complete
# but it supports either specified or automatic IP address and port
# selection.

class upnp_device(object):
    this_host_ip = None

    @staticmethod
    def local_ip_address():
        if not upnp_device.this_host_ip:
            temp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                temp_socket.connect(('8.8.8.8', 53))
                upnp_device.this_host_ip = temp_socket.getsockname()[0]
            except:
                upnp_device.this_host_ip = '127.0.0.1'
            del(temp_socket)
            dbg("got local address of %s" % upnp_device.this_host_ip)
        return upnp_device.this_host_ip
        

    def __init__(self, listener, poller, port, root_url, server_version, persistent_uuid, other_headers=None, ip_address=None):
        self.listener = listener
        self.poller = poller
        self.port = port
        self.root_url = root_url
        self.server_version = server_version
        self.persistent_uuid = persistent_uuid
        self.uuid = uuid.uuid4()
        self.other_headers = other_headers

        if ip_address:
            self.ip_address = ip_address
        else:
            self.ip_address = upnp_device.local_ip_address()

        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.bind((self.ip_address, self.port))
        self.socket.listen(5)
        if self.port == 0:
            self.port = self.socket.getsockname()[1]
        self.poller.add(self)
        self.client_sockets = {}
        self.listener.add_device(self)

    def fileno(self):
        return self.socket.fileno()

    def do_read(self, fileno):
        if fileno == self.socket.fileno():
            (client_socket, client_address) = self.socket.accept()
            self.poller.add(self, client_socket.fileno())
            self.client_sockets[client_socket.fileno()] = client_socket
        else:
            data, sender = self.client_sockets[fileno].recvfrom(4096)
            if not data:
                self.poller.remove(self, fileno)
                del(self.client_sockets[fileno])
            else:
                self.handle_request(data, sender, self.client_sockets[fileno])

    def handle_request(self, data, sender, socket):
        pass

    def get_name(self):
        return "unknown"
        
    def respond_to_search(self, destination, search_target):
        dbg("Responding to search for {!s:} @ {!s:}".format( self.get_name(), destination) )
        date_str = email.utils.formatdate(timeval=None, localtime=False, usegmt=True)
        location_url = self.root_url % {'ip_address' : self.ip_address, 'port' : self.port}
        message = ("HTTP/1.1 200 OK\r\n"
                  "CACHE-CONTROL: max-age=86400\r\n"
                  "DATE: %s\r\n"
                  "EXT:\r\n"
                  "LOCATION: %s\r\n"
                  "OPT: \"http://schemas.upnp.org/upnp/1/0/\"; ns=01\r\n"
                  "01-NLS: %s\r\n"
                  "SERVER: %s\r\n"
                  "ST: %s\r\n"
                  "USN: uuid:%s::%s\r\n" % (date_str, location_url, self.uuid, self.server_version, search_target, self.persistent_uuid, search_target))
        if self.other_headers:
            for header in self.other_headers:
                message += "%s\r\n" % header
        message += "\r\n"
        temp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        temp_socket.sendto(message, destination)
 

# This subclass does the bulk of the work to mimic a WeMo switch on the network.

class fauxmo(upnp_device):
    @staticmethod
    def make_uuid(name):
        return ''.join(["%x" % sum([ord(c) for c in name])] + ["%x" % ord(c) for c in "%sfauxmo!" % name])[:14]

    def __init__(self, name, listener, poller, ip_address, port, action_handler=None):
        self.serial = self.make_uuid(name)
        self.name = name
        self.ip_address = ip_address
        persistent_uuid = "Socket-1_0-" + self.serial
        other_headers = ['X-User-Agent: redsonic']
        upnp_device.__init__(self, listener, poller, port, "http://%(ip_address)s:%(port)s/setup.xml", "Unspecified, UPnP/1.0, Unspecified", persistent_uuid, other_headers=other_headers, ip_address=ip_address)
        if action_handler:
            self.action_handler = action_handler
        else:
            self.action_handler = self
        dbg("FauxMo device '%s' ready on %s:%s" % (self.name, self.ip_address, self.port))

    def get_name(self):
        return self.name

    def handle_request(self, data, sender, socket):
        if data.find('GET /setup.xml HTTP/1.1') == 0:
            dbg("Responding to setup.xml for %s" % self.name)
            xml = SETUP_XML % {'device_name' : self.name, 'device_serial' : self.serial}
            date_str = email.utils.formatdate(timeval=None, localtime=False, usegmt=True)
            message = ("HTTP/1.1 200 OK\r\n"
                       "CONTENT-LENGTH: %d\r\n"
                       "CONTENT-TYPE: text/xml\r\n"
                       "DATE: %s\r\n"
                       "LAST-MODIFIED: Sat, 01 Jan 2000 00:01:15 GMT\r\n"
                       "SERVER: Unspecified, UPnP/1.0, Unspecified\r\n"
                       "X-User-Agent: redsonic\r\n"
                       "CONNECTION: close\r\n"
                       "\r\n"
                       "%s" % (len(xml), date_str, xml))
            socket.send(message)
        elif data.find('SOAPACTION: "urn:Belkin:service:basicevent:1#SetBinaryState"') != -1:
            success = False
            if data.find('<BinaryState>1</BinaryState>') != -1:
                # on
                dbg("Responding to ON for %s" % self.name)
                success = self.action_handler.on()
            elif data.find('<BinaryState>0</BinaryState>') != -1:
                # off
                dbg("Responding to OFF for %s" % self.name)
                success = self.action_handler.off()
            else:
                dbg("Unknown Binary State request:")
                dbg(data)
            if success:
                # The echo is happy with the 200 status code and doesn't
                # appear to care about the SOAP response body
                soap = ""
                date_str = email.utils.formatdate(timeval=None, localtime=False, usegmt=True)
                message = ("HTTP/1.1 200 OK\r\n"
                           "CONTENT-LENGTH: %d\r\n"
                           "CONTENT-TYPE: text/xml charset=\"utf-8\"\r\n"
                           "DATE: %s\r\n"
                           "EXT:\r\n"
                           "SERVER: Unspecified, UPnP/1.0, Unspecified\r\n"
                           "X-User-Agent: redsonic\r\n"
                           "CONNECTION: close\r\n"
                           "\r\n"
                           "%s" % (len(soap), date_str, soap))
                socket.send(message)
            else :
                dbg("success={!s:}".format(success))
        else:
            dbg(data)

    def on(self):
        return False

    def off(self):
        return True


# Since we have a single process managing several virtual UPnP devices,
# we only need a single listener for UPnP broadcasts. When a matching
# search is received, it causes each device instance to respond.
#
# Note that this is currently hard-coded to recognize only the search
# from the Amazon Echo for WeMo devices. In particular, it does not
# support the more common root device general search. The Echo
# doesn't search for root devices.

class upnp_broadcast_responder(object):
    TIMEOUT = 0

    def __init__(self):
        self.devices = []

    def init_socket(self):
        ok = True
        self.ip = '239.255.255.250'
        self.port = 1900
        try:
            #This is needed to join a multicast group
            self.mreq = struct.pack("4sl",socket.inet_aton(self.ip),socket.INADDR_ANY)

            #Set up server socket
            self.ssock = socket.socket(socket.AF_INET,socket.SOCK_DGRAM,socket.IPPROTO_UDP)
            self.ssock.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)

            try:
                self.ssock.bind(('',self.port))
            except Exception, e:
                dbg("WARNING: Failed to bind %s:%d: %s" , (self.ip,self.port,e))
                ok = False

            try:
                self.ssock.setsockopt(socket.IPPROTO_IP,socket.IP_ADD_MEMBERSHIP,self.mreq)
            except Exception, e:
                dbg('WARNING: Failed to join multicast group:',e)
                ok = False

        except Exception, e:
            dbg("Failed to initialize UPnP sockets:",e)
            return False
        if ok:
            dbg("Listening for UPnP broadcasts")

    def fileno(self):
        return self.ssock.fileno()

    def do_read(self, fileno):
        data, sender = self.recvfrom(1024)
        if data:
            if data.find('M-SEARCH') == 0 and data.find('urn:Belkin:device:**') != -1:
                for device in self.devices:
                    time.sleep(0.1)
                    device.respond_to_search(sender, 'urn:Belkin:device:**')
            else:
                pass

    #Receive network data
    def recvfrom(self,size):
        if self.TIMEOUT:
            self.ssock.setblocking(0)
            ready = select.select([self.ssock], [], [], self.TIMEOUT)[0]
        else:
            self.ssock.setblocking(1)
            ready = True

        try:
            if ready:
                return self.ssock.recvfrom(size)
            else:
                return False, False
        except Exception, e:
	    dbg("recvfrom")
            dbg(e)
            return False, False

    def add_device(self, device):
        self.devices.append(device)
        dbg("UPnP broadcast listener: new device registered")


# This is an example handler class. The fauxmo class expects handlers to be
# instances of objects that have on() and off() methods that return True
# on success and False otherwise.
#
# This example class takes two full URLs that should be requested when an on
# and off command are invoked respectively. It ignores any return data.

class rest_api_handler(object):
    def __init__(self, on_cmd=None, off_cmd=None, dev_name=None):
        self.on_cmd = on_cmd
        self.off_cmd = off_cmd
        self.dev_name = dev_name

    def on(self):
        r = requests.get(self.on_cmd)
        if r.status_code != 200 :
            dbg("On status_code={!s:}".format(r.status_code))
            dbg("on_cmd={!s:}".format(self.on_cmd))
        return r.status_code == 200

    def off(self):
        r = requests.get(self.off_cmd)
        if r.status_code != 200 :
            dbg("Off status_code={!s:}".format(r.status_code))
            dbg("off_cmd={!s:}".format(self.on_cmd))
        return r.status_code == 200

    def __repr__(self):
        if DEBUG:
            return "<rest_api_handler {:s} at 0x{:02X}\n\t{:s}\n\t{:s}\n\t>".format(self.dev_name, id(self), self.on_cmd, self.off_cmd)
        else :
            return "<rest_api_handler {:s} at 0x{:02X}>".format(self.dev_name, id(self))


# Each entry is a list with the following elements:
#
# name of the virtual switch
# object with 'on' and 'off' methods
# port # (optional; may be omitted)

# NOTE: As of 2015-08-17, the Echo appears to have a hard-coded limit of
# 16 switches it can control. Only the first 16 elements of the FAUXMOS
# list will be used.

def build_fauxmos (fport=None) :
    """

     Generates a node callback config array from config data  

     args :
	fport		base udp port (optional)


     config array :
	 [
	     ['name1', *api_handler, portnum1],
	     ['name2', *api_handler, portnum2]
	 ]

     where api_handler is the class rest_api_handler :

	 api_handler = rest_api_handler(
	     'http://admin:admin@192.168.1.3/rest/nodes/16 3F E5 1/cmd/DON',
	     'http://admin:admin@192.168.1.3/rest/nodes/16 3F E5 1/cmd/DOF',
	     'office' )

    """

    ret_list = list()

    baseurl =  "http://{:s}:{:s}@{:s}/rest".format(isyuser,isypass, isyaddr)

    for k in sorted(isydevs.keys()) :
        a = rest_api_handler(
                "{:s}/nodes/{:s}/cmd/DON".format(baseurl, isydevs[k]), 
                "{:s}/nodes/{:s}/cmd/DOF".format(baseurl, isydevs[k]),
                k);
        l = [ k, a ]
        if fport is not None :
            l.append(fport)
            fport = fport + 1
        ret_list.append( l )

    # /rest/programs/<pgm-id>/<pgm-cmd>
    #
    #bath fan" : {
    #   "on"  : ("006E", "runThen"),
    #   "off" : ("0070", "runThen"),
    #   },
    for k in sorted(isyprog.keys()) :
        a = rest_api_handler(
                "{:s}/programs/{:s}/{:s}".format(baseurl, isyprog[k]['on'][0],  isyprog[k]['on'][1]), 
                "{:s}/programs/{:s}/{:s}".format(baseurl, isyprog[k]['off'][0], isyprog[k]['off'][1]), 
                k);
        l = [ k, a ]
        if fport is not None :
            l.append(fport)
            fport = fport + 1
        ret_list.append( l )

    return ret_list


if len(sys.argv) > 1 and sys.argv[1] == '-d':
    DEBUG = True

fauxmos = build_fauxmos(base_port)
if len(fauxmos) > echo_device_limit :
     print "Warning : more the {:d} device ( limit = {:d} )".format(len(fauxmos), echo_device_limit)


# Set up our singleton for polling the sockets for data ready
p = poller()

# Set up our singleton listener for UPnP broadcasts
u = upnp_broadcast_responder()
u.init_socket()

# Add the UPnP broadcast listener to the poller so we can respond
# when a broadcast is received.
p.add(u)


if DEBUG > 2 :
    import pprint
    #
    print "\nfauxmos :",
    pprint.pprint(fauxmos)

# Create our FauxMo virtual switch devices
for one_faux in fauxmos:
    if len(one_faux) == 2:
        # a fixed port wasn't specified, use a dynamic one
        one_faux.append(0)
    switch = fauxmo(one_faux[0], u, p, None, one_faux[2], action_handler = one_faux[1])

dbg("Entering main loop\n")

while True:
    try:
        # Allow time for a ctrl-c to stop the process
        p.poll(100)
        time.sleep(0.1)
    except Exception, e:
        dbg(e)
        break

