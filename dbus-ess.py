#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
control smart loads like kettles, heaters or heat pumps.
"""


from gi.repository import GLib
import platform
import logging
import sys
import os, time

import libmqtt

sys.path.insert(1, os.path.join(os.path.dirname(__file__), './ext/velib_python'))

from vedbus import VeDbusService
from dbusmonitor import DbusMonitor
from ve_utils import exit_on_error

servicename='com.victronenergy.ess'

# from /opt/victronenergy/vrmlogger/vrmlogger.py
def calculate_rtt(load, rtt, scale=0.2):
    """ This is an exponentially decaying function for working out the average
    rount-trip time over some window similar to what you'd use for load
    averages. Here we use it to get a clear picture of the round trip time
    of messages over dbus. For the chosen values you get a 4-minute picture
    when using 10-second spaced probes."""
    # return int(load * 0.8 + rtt * 0.2)
    return load * (1.0-scale) + rtt * scale

class ESS(object):

    def __init__(self, productname='IBR dbus ess', connection='dbus-ess'):

        logging.debug("Service %s starting... " % servicename)

        dummy = {'code': None, 'whenToLog': 'configChange', 'accessLevel': None}
        dbus_tree= {
                # victron system
                'com.victronenergy.system': { 
                    '/Dc/Pv/Power': dummy,
                    '/Dc/Battery/Power': dummy,
                    '/Ac/Consumption/NumberOfPhases': dummy,
                    '/Ac/Consumption/L1/Power': dummy,
                    '/Ac/Consumption/L2/Power': dummy,
                    '/Ac/Consumption/L3/Power': dummy,
                    '/Ac/ActiveIn/Source': dummy
                    },
                'com.victronenergy.battery': { '/Ess/Throttling': dummy, '/Ess/Prequest': dummy, '/Ess/Chgmode': dummy }
                }

        self._dbusmonitor = DbusMonitor(dbus_tree, valueChangedCallback=self.value_changed_wrapper)

        # com.victronenergy.battery.ttyUSB0
        battservices = self._get_connected_service_list(classfilter="com.victronenergy.battery")
        assert(len(battservices) > 0)

        if len(battservices) > 1:
            logging.info("Warning: MORE than one battery service found!")
            assert(0)

        for i in battservices:
            logging.info(f"battery service: {i}")
            self.battserviceName = i

        self._dbusservice = VeDbusService(servicename)

        # Create the management objects, as specified in the ccgx dbus-api document
        self._dbusservice.add_path('/Mgmt/ProcessName', __file__)
        self._dbusservice.add_path('/Mgmt/ProcessVersion', 'Unkown version, and running on Python ' + platform.python_version())
        self._dbusservice.add_path('/Mgmt/Connection', connection)

        # Create the mandatory objects
        self._dbusservice.add_path('/DeviceInstance', 1)
        self._dbusservice.add_path('/ProductId', 0)
        self._dbusservice.add_path('/ProductName', productname)
        self._dbusservice.add_path('/FirmwareVersion', 0)
        self._dbusservice.add_path('/HardwareVersion', 0)
        self._dbusservice.add_path('/Connected', 1)

        self.numberOfPhases = self._dbusmonitor.get_value("com.victronenergy.system", "/Ac/Consumption/NumberOfPhases")
        self.acsource = self._dbusmonitor.get_value("com.victronenergy.system", "/Ac/ActiveIn/Source") or 0
        logging.info(f"initial system:/Ac/ActiveIn/Source: {self.acsource}")

        self.loadSwitch = libmqtt.MqttSwitch("DynamicLoadSwitch", "cmnd/tasmota_exess_power/Dimmer", rate=1)
        self.loadSwitch.publish("0") # xxx errorhandling

        self.ysum = 0

        self.pvavg = 0
        self.pbatt = 0

        self.logtime = 0

        GLib.timeout_add(1000, exit_on_error, self.update)

    def update(self):

        #
        # Do not run the load when input is connected to mains, in this case we have
        # not enough solar power to run the load.
        #
        # acsource is 240 if ac input is not connected, 0 when input switch is closed
        if not self.acsource:

            if (self.logtime % 10) == 0:
                logging.info(f"***")
                logging.info(f"Load off, AC IN is connected.")
            self.logtime += 1

            self.loadSwitch.publish("0") # xxx errorhandling
            return True

        chgmode = self._dbusmonitor.get_value(self.battserviceName, "/Ess/Chgmode")
        th = self._dbusmonitor.get_value(self.battserviceName, "/Ess/Throttling")

        # Self-consumption
        powerconsumption = 0
        for i in range(self.numberOfPhases):
            powerconsumption += self._dbusmonitor.get_value("com.victronenergy.system", f"/Ac/Consumption/L{i+1}/Power")

        ppv = self._dbusmonitor.get_value("com.victronenergy.system", "/Dc/Pv/Power") # - pself

        if ppv > self.pvavg:
            self.pvavg = calculate_rtt(self.pvavg, ppv, scale=0.2)
        else:
            self.pvavg = calculate_rtt(self.pvavg, ppv, scale=0.1)

        pbatt = self._dbusmonitor.get_value("com.victronenergy.system", "/Dc/Battery/Power") or 0

        pdest = 0
        if not th:
            if chgmode == 0: # bulk
                pdest = 0.75*self.pvavg
            elif chgmode == 1: # balance
                # pdest = 250
                if pbatt <= 0:
                    pdest = -pbatt*1.25
                else:
                    pdest = self.pbatt
            elif chgmode == 2: # sink
                pass
            else: # 3 float
                # pdest = 250
                if pbatt <= 0:
                    pdest = -pbatt*1.25
                else:
                    pdest = self.pbatt

        self.pbatt = calculate_rtt(self.pbatt, pdest, scale=0.05)
        pbattchg = int(self.pbatt)

        battdsc = 0
        if chgmode == 2: # sink
            battdsc = 50

        # p_avail = self.pvavg - powerconsumption - pbattchg + battdsc
        e = self.pvavg - powerconsumption - pbattchg + battdsc

        maxout = 100

        self.ysum += e 

        Kc = 1/500 # 250
        Ki = 0.005 # 0.0025 # 0.0015

        ymaxpos = maxout/Ki
        ymaxneg = 0 # -ymaxpos/10

        if self.ysum > ymaxpos:
            self.ysum = ymaxpos
        elif self.ysum < ymaxneg:
            self.ysum = ymaxneg

        yp = Kc * e 
        yi = Ki*self.ysum

        y = ( yp + yi )

        out = round( max(0, y) )

        if (self.logtime % 10) == 0:
            logging.info(f"***")
            logging.info(f"pbatt: {pbatt:.0f}, batt reserve: {self.pbatt:.0f}, pdest: {pdest:.0f}")
            logging.info(f"p_avail {e:.0f} = pvavg {self.pvavg:.0f} - powerconsumption {powerconsumption:.0f} - pbattchg {pbattchg:.0f} + battdsc {battdsc:.0f}")
            logging.info(f"th: {th}, state: {chgmode}, p_avail/e: {e:.0f}, yP: {yp:.2f}, yI: {yi:.2f}, out: {out}, ysum: {self.ysum} ({ymaxneg}..{ymaxpos})")
        self.logtime += 1

        self.loadSwitch.publish(f"{out}") # xxx errorhandling
        return True

    def _get_connected_service_list(self, classfilter=None):
        services = self._dbusmonitor.get_service_list(classfilter=classfilter)
        return services

    # Calls value_changed with exception handling
    def value_changed_wrapper(self, *args, **kwargs):
        exit_on_error(self.value_changed, *args, **kwargs)

    # /Ac/ActiveIn/Source
    def value_changed(self, service, path, options, changes, deviceInstance):
        # logging.info('value_changed %s %s %s' % (service, path, str(changes)))

        if path == "/Ac/ActiveIn/Source":

            self.acsource = changes["Value"] or 0
            logging.info(f"update acsource: {self.acsource}")

# === All code below is to simply run it from the commandline for debugging purposes ===

# It will created a dbus service called com.victronenergy.pvinverter.output.
# To try this on commandline, start this program in one terminal, and try these commands
# from another terminal:
# dbus com.victronenergy.pvinverter.output
# dbus com.victronenergy.pvinverter.output /Ac/Energy/Forward GetValue
# dbus com.victronenergy.pvinverter.output /Ac/Energy/Forward SetValue %20
#
# Above examples use this dbus client: http://code.google.com/p/dbus-tools/wiki/DBusCli
# See their manual to explain the % in %20

def main():

    # set timezone used for log entries
    # os.environ['TZ'] = 'Europe/Berlin'
    # time.tzset()

    format = "%(asctime)s %(levelname)s:%(name)s:%(message)s"
    logging.basicConfig(level=logging.DEBUG, format=format, datefmt="%d.%m.%y_%X_%Z")

    from dbus.mainloop.glib import DBusGMainLoop
    # Have a mainloop, so we can send/receive asynchronous calls to and from dbus
    DBusGMainLoop(set_as_default=True)

    ess = ESS()

    logging.info('Connected to dbus, and switching over to GLib.MainLoop() (= event based)')
    mainloop = GLib.MainLoop()

    mainloop.run()


if __name__ == "__main__":
    main()


