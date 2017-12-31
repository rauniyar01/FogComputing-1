from twisted.internet import reactor, protocol, task
import json
import redis
from tasks import add, all_task_name, setTaskTime, getTaskTime
from message import state_message, fog_hello_message, fog_ready_message
from communication import UDPListener, UDPMulticaster, find_idle_port
import socket

class FogServerProtocol(protocol.Protocol):
    def connectionMade(self):
        self._peer = self.transport.getPeer()
        print("Connected to", self._peer)
        fog_ready = bytes(json.dumps(fog_ready_message), "ascii")
        self.transport.write(fog_ready)

    def taskInspection(self, task_message):
        print("task time:", self.factory.r.get(task_message["task_name"]))
        if self.factory.cloud_mode and task_message["cloud_processing"] == True or task_message["offload_times"] >= task_message["max_offload"]:
            operation = "cloud"
        elif self.factory.offloading_mode and task_message["time_requirement"] <= float(self.factory.r.get(task_message["task_name"])):
            operation = "fog"
        else:
            operation = "accept"

        return operation

    #TODO: 1.maintain a table of other servers; 2.periodic share task time with other fog servers
    def taskOffloading(self, task_message):
        task_message["offload_times"] += 1


    def taskProcessing(self, task_message):
        def onError(err):
            self.transport.write("task failed, reason: ", err)

        def respond(result):
            self.transport.write(bytes(json.dumps(result), "ascii"))

        if task_message["task_name"] == "add":
            d = add.delay(task_message["content"])
            d.addCallback(respond)
            d.addErrback(onError)

    def taskHandler(self, task_message):
        operation = self.taskInspection(task_message)
        if operation == "cloud":
            pass
        elif operation == "fog":
            pass
        elif operation == "accept":
            self.taskProcessing(task_message)

    def stateHandler(self, state_message):
        self.factory.state_table[self] = state_message["task_time"]
        print(self.factory.state_table)

    def saveFogNeighbourConnection(self):
        self.factory.fog_neighbour_connection.append(self)
        print(self.factory.fog_neighbour_connection)

    def deleteFogNeighbourConnection(self):
        self.factory.fog_neighbour_connection.remove(self)
        print(self.factory.fog_neighbour_connection)


    def dataReceived(self, data):
        data = data.decode("ascii")
        message = json.loads(data)
        if message["message_type"] == "task":
            self.taskHandler(message)
        elif message["message_type"] == "state":
            self.stateHandler(message)
        elif message["message_type"] == "fog_ready":
            self.saveFogNeighbourConnection()


    def connectionLost(self, reason):
        print("Disconnected from", self.transport.getPeer())
        self.deleteFogNeighbourConnection()






class FogServerFactory(protocol.ClientFactory):
    protocol = FogServerProtocol

    def __init__(self, r, offloading_mode = True, cloud_mode = True, sharing_interval = 5):
        self.fog_neighbour_connection = []
        self.state_table = {}
        self.r = r
        self.offloading_mode = offloading_mode
        self.cloud_mode = cloud_mode
        self.sharing_interval = sharing_interval
        self.lc = task.LoopingCall(self.shareState)
        self.lc.start(self.sharing_interval)


    def shareState(self):
        task_time = getTaskTime()
        state_sharing_message = state_message
        state_sharing_message["task_time"] = task_time
        state_sharing_message = bytes(json.dumps(state_sharing_message), "ascii")
        if self.fog_neighbour_connection:
            for fog in self.fog_neighbour_connection:
                fog.transport.write(state_sharing_message)




class MulticastSeverProtocol(protocol.DatagramProtocol):
    def __init__(self, tcp_port, fog_factory, group, multicast_port):
        self.group = group
        self.fog_hello = fog_hello_message
        self.fog_hello['tcp_port'] = tcp_port
        self.multicast_port = multicast_port
        self.fog_factory = fog_factory


    def startProtocol(self):
        self.transport.setTTL(5) # Set the TTL>1 so multicast will cross router hops
        self.transport.joinGroup(self.group)
        self.transport.write(bytes(json.dumps(self.fog_hello), "ascii"), (self.group, self.multicast_port))


    def datagramReceived(self, data, addr):
        data = data.decode("ascii")
        message = json.loads(data)
        if message["message_type"] == "fog_hello":
            fog_ip = addr[0]
            tcp_port = message["tcp_port"]
            reactor.connectTCP(fog_ip, tcp_port, self.fog_factory)



def main():
    r = redis.Redis(host='localhost', port=6379, decode_responses=True)
    setTaskTime()
    #TODO:  2. discvoery fogs 3. connect to fogs and store the connections in factory
    tcp_port = find_idle_port()
    multicast_group = "228.0.0.5"
    multicast_port = 8005
    fog_factory = FogServerFactory(r)
    reactor.listenTCP(tcp_port, fog_factory)
    reactor.listenMulticast(multicast_port, MulticastSeverProtocol(tcp_port, fog_factory, multicast_group, multicast_port), listenMultiple=True)
    reactor.run()



if __name__ == "__main__":
    main()