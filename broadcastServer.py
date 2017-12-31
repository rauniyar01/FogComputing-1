from __future__ import print_function

from twisted.internet.protocol import DatagramProtocol
from twisted.internet import reactor


class BroadcastServer(DatagramProtocol):

    def datagramReceived(self, data, addr):
        print("received %r from %s" % (data, addr))
        #self.transport.write(data, addr)

reactor.listenUDP(9999, BroadcastServer())

reactor.run()