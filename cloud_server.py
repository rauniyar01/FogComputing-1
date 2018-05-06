from twisted.internet import reactor, protocol, task
import json
import redis
from tasks import add, resetTaskTime, resetQueueState, getAllTaskTime, taskInQueue
from message import state_message, fog_hello_message, fog_ready_message, fog_ack_message
from communication import find_idle_port
import socket
import time
from functions import unpack
import sys

class FogServerProtocol(protocol.Protocol):
    def connectionMade(self):
        self._peer = self.transport.getPeer()
        print("Connected to", self._peer)
        if self._peer.host == self.factory.cloud_ip:
            self.factory.cloud_connection = self
        else:
            fog_ready = bytes(json.dumps(fog_ready_message), "ascii")
            #self.transport.write(fog_ready)

    def taskInspection(self, task_message):
        '''
        print("task time:", self.factory.r.get(task_message["task_name"]))
        if self.factory.cloud_mode and task_message["cloud_processing"] == True or task_message["offload_times"] >= task_message["max_offload"]:
            operation = "cloud"
        elif self.factory.offloading_mode and task_message["time_requirement"] <= float(self.factory.r.get(task_message["task_name"])):
            operation = "fog"
        else:
            operation = "accept"

        if (self.factory.cloud_mode == False and \
            self.factory.offloading_mode == False) or True:pass
            '''

        estimated_task_time = float(self.factory.r.get(task_message["task_name"]))
        idle_fog_task_time = self.factory.findIdleFog(task_message["task_name"], task_message["offloading_fog"])[1]
        print(estimated_task_time)
        if self.factory.cloud_mode == True and self.factory.fog_mode == True:
            if task_message["cloud_processing"] == True:
                operation = "cloud"
            else:
                if estimated_task_time <= task_message["time_requirement"] or \
                        (task_message["offload_times"] >= task_message["max_offload"] and task_message["task_type"] != "heavy") or \
                        (estimated_task_time <= idle_fog_task_time and task_message["task_type"] != "heavy"):
                    operation = "accept"
                elif (task_message["offload_times"] >= task_message["max_offload"] and task_message["task_type"] == "heavy") or \
                        (estimated_task_time <= idle_fog_task_time and task_message["task_type"] == "heavy"):
                    operation = "cloud"
                else:
                    operation = "fog"
        elif self.factory.cloud_mode == False and self.factory.fog_mode == True:
            if estimated_task_time <= task_message["time_requirement"] or \
                    task_message["offload_times"] >= task_message["max_offload"] or \
                    estimated_task_time <= idle_fog_task_time:
                operation = "accept"
            else:
                operation = "fog"
        elif self.factory.cloud_mode == True and self.factory.fog_mode == False:
            if task_message["cloud_processing"] == True or \
                    (estimated_task_time >= task_message["time_requirement"] and task_message['task_type'] == "heavy"):
                operation = "cloud"
            else:
                operation = "accept"
        elif self.factory.cloud_mode == False and self.factory.fog_mode == False:
            operation = "accept"

        return operation

    #TODO: 1.maintain a table of other servers; 2.periodic share task time with other fog servers
    def taskOffloading(self, task_message):
        task_id = task_message["task_id"]
        self.factory.send_back_table[task_id] = self
        fog = self.factory.findIdleFog(task_message["task_name"])[0]
        task_message["offload_times"] += 1
        host = self.transport.getHost().host
        #port = self.transport.getHost().port
        task_message["offloading_fog"].append(host)
        fog.transport.write(bytes(json.dumps(task_message), "ascii"))
        #b = self.factory.state_table.keys()
        #for c in b:
        #    c1 = c.transport.getHost()
        #    c2 = c.transport.getPeer()
        #a = self.transport.getHost()

    def taskSendToCloud(self, task_message):
        task_id = task_message["task_id"]
        self.factory.send_back_table[task_id] = self
        cloud = self.factory.cloud_connection
        cloud.transport.write(bytes(json.dumps(task_message), "ascii"))
#


    def taskProcessing(self, task_message):
        def onError(err):
            self.transport.write("task failed, reason: ", err)

        def respond(result):
            self.transport.write(bytes(json.dumps(result), "ascii"))

        if task_message["task_name"] == "add":
            light_task_num = self.factory.r.get('light_task_num')
            if light_task_num == None:
                self.factory.r.set('light_task_num', 1)
            else:
                self.factory.r.set('light_task_num', int(light_task_num) + 1)
            enqueue_time = time.time()
            d = add.delay(task_message["content"], task_message["task_id"], enqueue_time)
            d.addCallback(respond)
            d.addErrback(onError)

    def taskDistributor(self, task_message):
        if task_message["offload_times"] == 0:
            #task_message["task_id"] = self.factory.next_task_id
            self.factory.next_task_id += 1

        operation = self.taskInspection(task_message)
        if operation == "cloud":
            self.taskSendToCloud(task_message)
        elif operation == "fog":
            self.taskOffloading(task_message)
        elif operation == "accept":
            self.taskProcessing(task_message)

    def resultHandler(self, result_message):
        task_id = result_message["task_id"]
        connection = self.factory.send_back_table[task_id]
        connection.transport.write(bytes(json.dumps(result_message), "ascii"))

    def stateHandler(self, state_message):
        self.factory.state_table[self] = state_message["task_time"]
        print(self.factory.state_table)

    def saveFogNeighbourConnection(self):
        self.factory.fog_neighbour_connection.append(self)
        print(self.factory.fog_neighbour_connection)

    def deleteFogNeighbourConnection(self):
        if self in self.factory.fog_neighbour_connection:
            self.factory.fog_neighbour_connection.remove(self)
            print(self.factory.fog_neighbour_connection)


    def dataReceived(self, data):
        data = data.decode("ascii")
        unpacked_data = unpack(data)
        for data in unpacked_data:
            print(data)
            message = json.loads(data)
            print(message)
            self.taskProcessing(message)


    def connectionLost(self, reason):
        print("Disconnected from", self.transport.getPeer())
        self.deleteFogNeighbourConnection()


class FogServerFactory(protocol.ClientFactory):
    protocol = FogServerProtocol

    def __init__(self, r, task_id_root, cloud_ip, fog_mode = True, cloud_mode = True, sharing_interval = 5):
        self.fog_neighbour_connection = []
        self.cloud_ip = cloud_ip
        self.cloud_connection = None
        self.current_connection = None
        self.state_table = {}
        self.state_table_without_offloaded_fog = {}
        self.send_back_table = {}
        self.previous_task_time = 0
        self.r = r
        self.next_task_id = task_id_root
        self.fog_mode = fog_mode
        self.cloud_mode = cloud_mode
        self.sharing_interval = sharing_interval
        self.lc = task.LoopingCall(self.shareState)
        self.lc.start(self.sharing_interval)


    def shareState(self):
        total_num, light_num, medium_num, heavy_num = taskInQueue()
        if total_num == 0:
            resetTaskTime()
        task_time = getAllTaskTime()
        state_sharing_message = state_message
        state_sharing_message["task_time"] = task_time
        state_sharing_message = bytes(json.dumps(state_sharing_message), "ascii")
        if self.fog_neighbour_connection:
            for fog in self.fog_neighbour_connection:
                fog.transport.write(state_sharing_message)

    def findIdleFog(self, task_name, offloaded_fog_ip = []):
        self.state_table_without_offloaded_fog = self.state_table.copy()
        if len(self.state_table):
            if len(offloaded_fog_ip) != 0:
                for fog_connection in self.state_table.keys():
                    if fog_connection.transport.getPeer().host in offloaded_fog_ip:
                        del self.state_table_without_offloaded_fog[fog_connection]
            if len(self.state_table_without_offloaded_fog) == 0:
                fog_connection, task_time = None, 1000000
            else:
                fog_connection, all_task_time = min(self.state_table_without_offloaded_fog.items(),
                                                    key=lambda x: x[1][task_name])
                task_time = all_task_time[task_name]
        else:
            fog_connection, task_time = None, 1000000
        return fog_connection, task_time



class MulticastSeverProtocol(protocol.DatagramProtocol):

    def __init__(self, tcp_port, fog_factory, group, multicast_port):
        self.group = group
        self.tcp_port = tcp_port
        self.fog_hello = fog_hello_message
        self.fog_hello['tcp_port'] = tcp_port
        self.fog_ack = fog_ack_message
        self.fog_ack['tcp_port'] = tcp_port
        self.multicast_port = multicast_port
        self.fog_factory = fog_factory
        self.ip = self.get_host_ip()

    def startProtocol(self):
        self.transport.setTTL(5) # Set the TTL>1 so multicast will cross router hops
        self.transport.joinGroup(self.group)
        self.transport.write(bytes(json.dumps(self.fog_hello), "ascii"), (self.group, self.multicast_port))


    def datagramReceived(self, data, addr):
        data = data.decode("ascii")
        message = json.loads(data)
        print(message)
        if message["message_type"] == "fog_hello":
            fog_ip = addr[0]
            tcp_port = message["tcp_port"]
            # if fog_ip != self.ip:
            if tcp_port != self.tcp_port or fog_ip != self.ip:
                reactor.connectTCP(fog_ip, tcp_port, self.fog_factory)
        elif message["message_type"] == "endpoint_hello":
            self.transport.write(bytes(json.dumps(self.fog_ack), "ascii"), (self.group, self.multicast_port))

    def get_host_ip(self):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(('8.8.8.8', 80))
            ip = s.getsockname()[0]
        finally:
            s.close()
        return ip



def main():
    r = redis.Redis(host='localhost', port=6379, decode_responses=True)
    resetTaskTime()
    resetQueueState()
    #TODO:  2. discvoery fogs 3. connect to fogs and store the connections in factory
    cloud_ip = '54.206.45.203'
    cloud_port = 10000
    #if len(sys.argv) > 0:
    #    cloud_ip = sys.argv[0]
    tcp_port = find_idle_port()
    multicast_group = "228.0.0.5"
    multicast_port = 8005
    task_id_root = 10000
    fog_factory = FogServerFactory(r, task_id_root, cloud_ip)
    multicast_server_protocol = MulticastSeverProtocol(tcp_port, fog_factory, multicast_group, multicast_port)
    #reactor.connectTCP(cloud_ip, cloud_port, fog_factory)
    #reactor.connectTCP(cloud_ip, cloud_port, fog_factory)
    reactor.listenTCP(tcp_port, fog_factory)
    #reactor.listenMulticast(multicast_port, multicast_server_protocol, listenMultiple=True)
    reactor.run()



if __name__ == "__main__":
    main()