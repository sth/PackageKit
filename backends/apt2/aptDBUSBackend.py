#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Provides an apt backend to PackageKit

Copyright (C) 2007 Ali Sabil <ali.sabil@gmail.com>
Copyright (C) 2007 Tom Parker <palfrey@tevp.net>
Copyright (C) 2008 Sebastian Heinlein <glatzor@ubuntu.com>

Licensed under the GNU General Public License Version 2

This program is free software; you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation; either version 2 of the License, or
(at your option) any later version.
"""

__author__  = "Sebastian Heinlein <devel@glatzor.de>"
__state__   = "experimental"

import logging
import os
import re
import warnings

import apt
import dbus
import dbus.service
import dbus.mainloop.glib
import xapian

from packagekit.daemonBackend import PACKAGEKIT_DBUS_INTERFACE, PACKAGEKIT_DBUS_PATH, PackageKitBaseBackend, PackagekitProgress
from packagekit.enums import *

logging.basicConfig()
log = logging.getLogger("aptDBUSBackend")
log.setLevel(logging.DEBUG)

warnings.filterwarnings(action='ignore', category=FutureWarning)

PACKAGEKIT_DBUS_SERVICE = 'org.freedesktop.PackageKitAptBackend'

XAPIANDBPATH = os.environ.get("AXI_DB_PATH", "/var/lib/apt-xapian-index")
XAPIANDB = XAPIANDBPATH + "/index"
XAPIANDBVALUES = XAPIANDBPATH + "/values"
DEFAULT_SEARCH_FLAGS = (xapian.QueryParser.FLAG_BOOLEAN |
                        xapian.QueryParser.FLAG_PHRASE |
                        xapian.QueryParser.FLAG_LOVEHATE |
                        xapian.QueryParser.FLAG_BOOLEAN_ANY_CASE)

class PackageKitOpProgress(apt.progress.OpProgress):
    def __init__(self, backend):
        self._backend = backend
        apt.progress.OpProgress.__init__(self)

    # OpProgress callbacks
    def update(self, percent):
        self._backend.PercentageChanged(int(percent))

    def done(self):
        self._backend.PercentageChanged(100)

class PackageKitFetchProgress(apt.progress.FetchProgress):
    def __init__(self, backend):
        self._backend = backend
        apt.progress.FetchProgress.__init__(self)
    # FetchProgress callbacks
    def pulse(self):
        apt.progress.FetchProgress.pulse(self)
        self._backend.percentage(self.percent)
        return True

    def stop(self):
        self._backend.percentage(100)

    def mediaChange(self, medium, drive):
        #FIXME: use the Message method to notify the user
        self._backend.error(ERROR_INTERNAL_ERROR,
                "Medium change needed")

class PackageKitInstallProgress(apt.progress.InstallProgress):
    def __init__(self, backend):
        apt.progress.InstallProgress.__init__(self)

class PackageKitAptBackend(PackageKitBaseBackend):
    def __init__(self, bus_name, dbus_path):
        log.info("Initializing backend")
        PackageKitBaseBackend.__init__(self, bus_name, dbus_path)
        self._cache = None
        self._xapian = None

    # Methods ( client -> engine -> backend )

    @dbus.service.method(PACKAGEKIT_DBUS_INTERFACE,
                         in_signature='', out_signature='')
    def Init(self):
        log.info("Initializing cache")
        self._cache = apt.Cache(PackageKitOpProgress(self))
        self._xapian = xapian.Database(XAPIANDB)

    @dbus.service.method(PACKAGEKIT_DBUS_INTERFACE,
                         in_signature='', out_signature='')
    def Exit(self):
        self.loop.quit()

    @dbus.service.method(PACKAGEKIT_DBUS_INTERFACE,
                         in_signature='ss', out_signature='')
    def SearchName(self, filters, search):
        '''
        Implement the apt2-search-name functionality
        '''
        log.info("Searching for package name: %s" % search)
        self.AllowCancel(True)
        self.NoPercentageUpdates()

        self.StatusChanged(STATUS_QUERY)

        for pkg in self._cache:
            if search in pkg.name and self._package_is_visible(pkg, filters):
                self._emit_package(pkg)
        self.Finished(EXIT_SUCCESS)


    @dbus.service.method(PACKAGEKIT_DBUS_INTERFACE,
                         in_signature='ss', out_signature='')
    def SearchDetails(self, filters, search):
        '''
        Implement the apt2-search-details functionality
        '''
        log.info("Searching for package name: %s" % search)
        self.AllowCancel(True)
        self.NoPercentageUpdates()
        self.StatusChanged(STATUS_QUERY)

        self._xapian.reopen()
        parser = xapian.QueryParser()
        query = parser.parse_query(unicode(search),
                                   DEFAULT_SEARCH_FLAGS)
        enquire = xapian.Enquire(self._xapian)
        enquire.set_query(query)
        matches = enquire.get_mset(0, 1000)
        for m in matches:
            name = m[xapian.MSET_DOCUMENT].get_data()
            if self._cache.has_key(name):
                pkg = self._cache[name]
                if self._package_is_visible(pkg) == True:
                    self._emit_package(pkg)

        self.Finished(EXIT_SUCCESS)


    @dbus.service.method(PACKAGEKIT_DBUS_INTERFACE,
                         in_signature='', out_signature='')
    def GetUpdates(self):
        '''
        Implement the {backend}-get-update functionality
        '''
        self.AllowCancel(True)
        self.NoPercentageUpdates()
        self.StatusChanged(STATUS_INFO)
        self._cache.upgrade(False)
        for pkg in self._cache.getChanges():
            self._emit_package(pkg)
        self.Finished(EXIT_SUCCESS)

    @dbus.service.method(PACKAGEKIT_DBUS_INTERFACE,
                         in_signature='s', out_signature='')
    def GetDescription(self, pkg_id):
        '''
        Implement the {backend}-get-description functionality
        '''
        self.AllowCancel(True)
        self.NoPercentageUpdates()
        self.StatusChanged(STATUS_INFO)
        name, version, arch, data = self.get_package_from_id(pkg_id)
        #FIXME: error handling
        pkg = self._cache[name]
        #FIXME: should perhaps go to python-apt since we need this in 
        #       several applications
        desc = pkg.description
        # Skip the first line - it's a duplicate of the summary
        i = desc.find('\n')
        desc = desc[i+1:]
        # do some regular expression magic on the description
        # Add a newline before each bullet
        p = re.compile(r'^(\s|\t)*(\*|0|-)',re.MULTILINE)
        desc = p.sub('\n*', desc)
        # replace all newlines by spaces
        p = re.compile(r'\n', re.MULTILINE)
        desc = p.sub(" ", desc)
        # replace all multiple spaces by newlines
        p = re.compile(r'\s\s+', re.MULTILINE)
        desc = p.sub('\n', desc)
        # Get the homepage of the package
        # FIXME: switch to the new unreleased API
        if pkg.candidateRecord.has_key('Homepage'):
            homepage = pkg.candidateRecord['Homepage']
        else:
            homepage = ''
        #FIXME: group and licence information missing
        self.Description(pkg_id, 'unknown', 'unknown', desc,
                         homepage, pkg.packageSize)
        self.Finished(EXIT_SUCCESS)


    @dbus.service.method(PACKAGEKIT_DBUS_INTERFACE,
                         in_signature='', out_signature='')
    def Unlock(self):
        self.doUnlock()

    def doUnlock(self):
        if self.isLocked():
            PackageKitBaseBackend.doUnlock(self)


    @dbus.service.method(PACKAGEKIT_DBUS_INTERFACE,
                         in_signature='', out_signature='')
    def Lock(self):
        self.doLock()

    def doLock(self):
        pass

    #
    # Helpers
    #
    def get_id_from_package(self, pkg, installed=False):
        '''
        Returns the id of the installation candidate of a core
        apt package. If installed is set to True the id of the currently
        installed package will be returned.
        '''
        origin = ''
        if installed == False and pkg.isInstalled:
            pkgver = pkg.installedVersion
        else:
            pkgver = pkg.candidateVersion
            if pkg.candidateOrigin:
                origin = pkg.candidateOrigin[0].label
        id = self._get_package_id(pkg.name, pkgver, pkg.architecture, origin)
        return id

    def _emit_package(self, pkg, installed=False):
        '''
        Send the Package signal for a given apt package
        '''
        id = self.get_id_from_package(pkg, installed)
        if installed and pkg.isInstalled:
            status = INFO_INSTALLED
        else:
            status = INFO_AVAILABLE
        summary = pkg.summary
        self.Package(status, id, summary)

    def _package_is_visible(self, pkg, filters):
        '''
        Return True if the package should be shown in the user interface
        '''
        #FIXME: Needs to be optmized
        if filters == 'none':
            return True
        if FILTER_INSTALLED in filters and not pkg.isInstalled:
            return False
        if FILTER_NOT_INSTALLED in filters and pkg.isInstalled:
            return False
        if FILTER_GUI in filters and not self._package_has_gui(pkg):
            return False
        if FILTER_NOT_GUI in filters and self._package_has_gui(pkg):
            return False
        if FILTER_DEVELOPMENT in filters and not self._package_is_devel(pkg):
            return False
        if FILTER_NOT_DEVELOPMENT in filters and self._package_is_devel(pkg):
            return False
        return True

    def _package_has_gui(self, pkg):
        #FIXME: should go to a modified Package class
        #FIXME: take application data into account. perhaps checking for 
        #       property in the xapian database
        return pkg.section.split('/')[-1].lower() in ['x11', 'gnome', 'kde']

    def _package_is_devel(self, pkg):
        #FIXME: should go to a modified Package class
        return pkg.name.endswith("-dev") or pkg.name.endswith("-dbg") or \
               pkg.section.split('/')[-1].lower() in ['devel', 'libdevel']


if __name__ == '__main__':
    loop = dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = dbus.SystemBus(mainloop=loop)
    bus_name = dbus.service.BusName(PACKAGEKIT_DBUS_SERVICE, bus=bus)
    manager = PackageKitAptBackend(bus_name, PACKAGEKIT_DBUS_PATH)

# vim: ts=4 et sts=4
