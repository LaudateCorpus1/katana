# This file is part of Buildbot.  Buildbot is free software: you can
# redistribute it and/or modify it under the terms of the GNU General Public
# License as published by the Free Software Foundation, version 2.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE.  See the GNU General Public License for more
# details.
#
# You should have received a copy of the GNU General Public License along with
# this program; if not, write to the Free Software Foundation, Inc., 51
# Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.
#
# Copyright Buildbot Team Members

from twisted.internet import defer
from buildbot.util import tuplematch
from buildbot.test.util import types

class FakeMQConnector(object):

    # a fake connector that doesn't actually bridge messages from production to
    # consumption, and thus doesn't do any topic handling or persistence

    # note that this *does* verify all messages sent and received, unless this
    # is set to false:
    verifyMessages = True

    def __init__(self, master, testcase):
        self.master = master
        self.testcase = testcase
        self.setup_called = False
        self.productions = []
        self.qrefs = []

    def setup(self):
        self.setup_called = True
        return defer.succeed(None)

    def produce(self, routingKey, data):
        self.testcase.assertIsInstance(routingKey, tuple)
        if self.verifyMessages:
            types.verifyMessage(self.testcase, routingKey, data)
        if [ k for k in routingKey if not isinstance(k, str) ]:
            raise AssertionError("%s is not all strings" % (routingKey,))
        self.productions.append((routingKey, data))
        # note - no consumers are called: IT'S A FAKE

    def callConsumer(self, routingKey, msg):
        if self.verifyMessages:
            types.verifyMessage(self.testcase, routingKey, msg)
        matched = False
        for q in self.qrefs:
            if tuplematch.matchTuple(routingKey, q.filter):
                matched = True
                q.callback(routingKey, msg)
        if not matched:
            raise AssertionError("no consumer found")

    def startConsuming(self, callback, filter, persistent_name=None):
        if [ k for k in filter if not isinstance(k, str) and k is not None ]:
            raise AssertionError("%s is not a filter" % (filter,))
        qref = FakeQueueRef()
        qref.qrefs = self.qrefs
        qref.callback = callback
        qref.filter = filter
        qref.persistent_name = persistent_name
        self.qrefs.append(qref)
        return qref

class FakeQueueRef(object):

    def stopConsuming(self):
        if self in self.qrefs:
            self.qrefs.remove(self)
