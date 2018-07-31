'''

This module implements the P2P communication server.
Peers communicate by sending messages from one to another.
These messages carry requests.

Requests that require information which may not be ready-available, should respond with a request ticket.
This ticket contains a request identification. The request answer will be later sent.

It is the responsibility of the peer which performs the request, to deal with the asynchronous nature of the requests.
A peer must wait for a message indicating the success or failure to complete a request, to react accordingly.

Requests are structured as tuples:
(peer ID, Request name, data_dict{})

Replies are structured as tuples:
(Reply name, result structure)

'''

import sys
import logging
import pickle
import time
import blosc
import struct

from uuid import UUID
from ipaddress import IPv4Address, IPv6Address

import eventlet
from eventlet.green import zmq
from eventlet.semaphore import BoundedSemaphore
from eventlet.green import socket
from ryu.lib import hub
#from eventlet.hubs import trampoline
from ryu.controller import controller

#from eventlet import hubs
#hubs.use_hub('eventlet')

from archsdn import central
from archsdn import database
from archsdn.helpers import logger_module_name, custom_logging_callback
from archsdn.engine.exceptions import PathNotFound
from archsdn.engine.entities import Sector


_log = logging.getLogger(logger_module_name(__file__))

_server_stream = None

_tasks_under_execution = {}

_socket_timeout = 2000  # receive timeout milliseconds
_socket_connect_timeout = 2000  # receive timeout milliseconds
_socket_retries = 3  # number of retries before fail

_connection_objects = {}


class ConnectionFailed(Exception):
    pass


class UnexpectedResponse(Exception):
    def __init__(self, response):
        self.response = response


class __PeerProxy:

    __counter = 0

    def __init__(self, location):
        try:
            self.__counter = __class__.__counter
            __class__.__counter += 1
            _log.debug("Initializing communication to peer ({:s}: {:d})".format(str(location), self.__counter))

            self.__location = (str(location[0]), location[1])
            self.__stream_client = hub.StreamClient(self.__location)
            self.__socket = self.__stream_client.connect()
            self.__socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.__socket.settimeout(_socket_timeout)
            self.__closed = False

        except Exception:
            custom_logging_callback(_log, logging.ERROR, *sys.exc_info())
            raise

    def __del__(self):
        _log.debug("Destroying proxy with id: {:d}".format(self.__counter))
        self.__stream_client.stop()
        if not self.__closed:
            self.__socket.shutdown(socket.SHUT_RDWR)
            self.__socket.close()

    def __getattr__(self, func_name):
        if func_name not in _requests:
            raise AttributeError("Proxy object has no attribute '{:s}'".format(func_name))
        if self.__closed:
            raise Exception("Socket Closed")

        def remote_method_call(*args, **kwargs):
            try:
                _log.debug(
                    "Peer Proxy ({:d}) connected to {:s}, requesting \"{:s}\" with args {:s}".format(
                        self.__counter,
                        str(self.__location),
                        func_name,
                        str((args, kwargs))
                    )
                )
                encoded_request_bytes = blosc.compress(pickle.dumps((func_name, args, kwargs)))
                self.__socket.sendall(
                    struct.pack(
                        "!H{:d}s".format(len(encoded_request_bytes)),
                        len(encoded_request_bytes),
                        encoded_request_bytes
                    )
                )

                # First, receive the length of the request
                received_bytes = 0
                buf = bytearray(2)
                while received_bytes < 2:
                    data_bytes = self.__socket.recv(2 - received_bytes, socket.MSG_WAITALL)
                    if data_bytes:
                        memoryview(buf)[received_bytes:received_bytes+len(data_bytes)] = data_bytes
                        received_bytes += len(data_bytes)
                    else:
                        raise Exception("Socket Closed")
                    # _log.debug("data_bytes: {:d}".format(len(data_bytes)))
                msg_len = struct.unpack("!H", buf)[0]

                # Then, receive the encoded request
                received_bytes = 0
                buf = bytearray(msg_len)
                while received_bytes < msg_len:
                    data_bytes = self.__socket.recv(msg_len - received_bytes, socket.MSG_WAITALL)
                    if data_bytes:
                        memoryview(buf)[received_bytes:received_bytes+len(data_bytes)] = data_bytes
                        received_bytes += len(data_bytes)
                    else:
                        raise Exception("Socket Closed")
                    # _log.debug("data_bytes: {:d}".format(len(data_bytes)))
                answer = pickle.loads(blosc.decompress(buf))

                if not isinstance(answer, tuple):
                    raise UnexpectedResponse("Received wrong data type.")
                if answer[0]:
                    raise UnexpectedResponse(answer[1])

                _log.debug(
                    "Peer Proxy ({:d}) answer is \"{:s}\"".format(
                        self.__counter,
                        str(answer)
                    )
                )
                return answer[1]

            except Exception:
                custom_logging_callback(_log, logging.ERROR, *sys.exc_info())
                self.__socket.shutdown(socket.SHUT_RDWR)
                self.__socket.close()
                raise
        return remote_method_call


def get_controller_proxy(controller_id):
    assert isinstance(controller_id, UUID), "controller_id expected to be UUID. Got {:s}".format(repr(controller_id))

    controller_info = central.query_controller_info(controller_id)

    if controller_info.ipv6:
        location = (controller_info.ipv6, controller_info.ipv6_port)
    elif controller_info.ipv4:
        location = (controller_info.ipv4, controller_info.ipv4_port)
    else:
        raise AttributeError("Cannot acquire the controller {:s} network address.".format(str(controller_id)))

    return __PeerProxy(location)


def initialize_server(ip, port):
    global _server_stream
    assert isinstance(ip, (IPv4Address, IPv6Address)), \
        "ip is not a valid IPv4Address or IPv6Address object. Got instead {:s}".format(repr(ip))
    assert isinstance(port, int), \
        "port is not a valid int object. Got instead {:s}".format(repr(port))
    assert 0 < port < 0xFFFF, \
        "port range invalid. Should be between 0 and 0xFFFF. Got {:d}".format(port)

    def client_handler(client_skt, client_addr):
        client_skt.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        client_skt.settimeout(_socket_timeout)

        try:
            while True:
                _log.debug("Serving {:s}".format(str(client_addr)))
                # First, receive the length of the request
                received_bytes = 0
                buf = bytearray(2)
                while received_bytes < 2:
                    data_bytes = client_skt.recv(2-received_bytes, socket.MSG_WAITALL)
                    if data_bytes:
                        memoryview(buf)[received_bytes:received_bytes+len(data_bytes)] = data_bytes
                        received_bytes += len(data_bytes)
                    else:
                        return
                    # _log.debug("need_to_receive: {:d} data_bytes received: {:d};".format(2-received_bytes, len(data_bytes)))
                msg_len = struct.unpack("!H", buf)[0]

                # Then, receive the encoded request
                received_bytes = 0
                buf = bytearray(msg_len)
                while received_bytes < msg_len:
                    data_bytes = client_skt.recv(msg_len - received_bytes, socket.MSG_WAITALL)
                    if data_bytes:
                        memoryview(buf)[received_bytes:received_bytes+len(data_bytes)] = data_bytes
                        received_bytes += len(data_bytes)
                    else:
                        return
                    # _log.debug("data_bytes: {:d} - {}".format(len(data_bytes), data_bytes))

                func_name = None
                try:
                    request = pickle.loads(blosc.decompress(buf))
                    _log.debug("Request received: {:s}".format(str(request)))

                    assert isinstance(request, tuple), "request type is not tuple"
                    assert len(request) == 3, "request length is not equal to 3"
                    assert isinstance(request[0], str), "request function name parameter is not string"
                    assert request[0] in _requests, "function name is not registered "

                    func_name = request[0]
                    args = request[1]
                    kwargs = request[2]

                    _log.debug(
                        "Client is requesting {:s} with data {:s}.".format(
                            func_name,
                            str((args, kwargs))
                        )
                    )
                    answer = (0, _requests[func_name](*args, **kwargs))

                except KeyError:
                    answer = (1, "Unknown Request: '{:s}'".format(repr(func_name)))

                except Exception as ex:
                    custom_logging_callback(_log, logging.ERROR, *sys.exc_info())
                    if sys.flags.debug:
                        answer = (1, "Unknown Request: {}".format(str(ex)))
                    else:
                        answer = (1, "Internal Error. Cannot process request.")

                answer_data = blosc.compress(pickle.dumps(answer))
                client_skt.sendall(
                    struct.pack("!H{:d}s".format(len(answer_data)), len(answer_data), answer_data)
                )

        except Exception:
            custom_logging_callback(_log, logging.ERROR, *sys.exc_info())
    try:
        _server_stream = hub.StreamServer((str(ip), port), client_handler)
        hub.spawn(_server_stream.serve_forever)
        _log.warning("P2P Server initialized.")
    except OSError as ex:
        if ex.errno == 98:
            _log.error(
                "Cannot initialize P2P server. Address {:s} with port {:d} is already being used.".format(
                    str(ip),
                    port
                )
            )
        else:
            _log.error(str(ex))
        raise


def shutdown_server():
    _server_stream.server.close()
    #_clients_context.destroy()
    pass


def __req_local_time(*args, **kwargs):
    return time.asctime()


def __publish_event(*args, **kwargs):
    pass


def __query_address_info(*args, **kwargs):
    return database.query_address_info(*args, **kwargs)


def __activate_scenario(scenario_request):
    from archsdn import database
    from archsdn.engine import sector
    from archsdn.engine import globals
    from archsdn.engine import services

    assert isinstance(scenario_request, dict), \
        "scenario_request is expected to be of dict type. Got {:s}.".format(repr(scenario_request))
    missing_args = tuple(
        filter(
            (lambda arg: arg not in scenario_request.keys()),
            ('global_path_search_id', 'mpls_label', 'sector_requesting_service', 'hash_val')
        )
    )
    if missing_args:
        raise TypeError("The following arguments are missing: {:s}".format(", ".join(missing_args)))

    global_path_search_id = scenario_request['global_path_search_id']
    sector_requesting_service_id = UUID(scenario_request['sector_requesting_service'])
    scenario_mpls_label = scenario_request['mpls_label']
    scenario_hash_val = scenario_request['hash_val'] # hash value which identifies the switch that sends the traffic
#    _log.debug("scenario_hash_val: {:x}".format(scenario_hash_val))

    assert isinstance(scenario_mpls_label, int) and scenario_mpls_label >= 0, \
        "scenario_mpls_label expected to be non negative  int"
    assert isinstance(scenario_hash_val, int) and scenario_hash_val >= 0, \
        "scenario_hash_val expected to be non negative int"


    source_controller_id = UUID(global_path_search_id[0])
    source_ipv4_str = global_path_search_id[1]
    target_ipv4_str = global_path_search_id[2]
    target_ipv4 = IPv4Address(global_path_search_id[2])
    scenario_type = global_path_search_id[3]
    target_host_info = central.query_address_info(ipv4=target_ipv4_str)

    this_controller_id = database.get_database_info()['uuid']

    # _log.debug(
    #     "__activate_scenario\n"
    #     "  global_path_search_id: {:s}\n"
    #     "  sector_requesting_service_id: {:s}\n"
    #     "  scenario_mpls_label: {:d}\n"
    #     "  scenario_hash_val: {:x}\n"
    #     "  source_controller_id: {:s}\n"
    #     "  source_ipv4_str: {:s}\n"
    #     "  target_ipv4_str: {:s}\n"
    #     "  scenario_type: {:s}\n"
    #     "  target_host_info: {:s}\n"
    #     "  this_controller_id: {:s}\n".format(
    #         str(global_path_search_id),
    #         str(sector_requesting_service_id),
    #         scenario_mpls_label,
    #         scenario_hash_val,
    #         str(source_controller_id),
    #         str(source_ipv4_str),
    #         str(target_ipv4_str),
    #         scenario_type,
    #         str(target_host_info),
    #         str(this_controller_id)
    #     )
    # )

    assert isinstance(this_controller_id, UUID), "this_controller_id expected to be UUID."
    assert isinstance(scenario_type, str), "scenario_type expected to be str"

    if source_controller_id == this_controller_id:
        error_str = "Path search has reached the source controller. Cancelling..."
        _log.warning(error_str)
        return {"success": False, "reason": error_str}

    if globals.is_scenario_active(global_path_search_id):
        error_str = "Scenario with ID {:s} is already implemented.".format(str(global_path_search_id))
        _log.warning(error_str)
        return {"success": False, "reason": error_str}

    if scenario_type == 'ICMPv4':
        try:
            # Maintain an active token during the duration of this task. When the task is terminated, the token will
            #   be removed.
            active_task_token = globals.register_implementation_task(global_path_search_id, "IPv4", "ICMP")

            if target_host_info.controller_id == this_controller_id:
                # This IS the target sector

                # Trying to activate the local path, to finish the global path.
                bidirectional_path = sector.construct_bidirectional_path(
                    sector_requesting_service_id,  # Sector from which the scenario request came
                    target_host_info.name,  # Target Hostname which identifies the host entity in this sector.
                    allocated_bandwith=100,  # Requested bandwidth for ICMP path
                    previous_sector_hash=scenario_hash_val  # hash value which identifies the switch that sends the traffic
                )
                # Implementation notes:
                #  'sector_a_hash_val' is necessary for the sector controller. In the use-case where multiple switches
                #   are connected to the same sector, the controller from that sector uses the 'sector_a_hash_val' to
                #   distinguish between switches. The 'sector_a_hash_val' is sent in each discovery beacon, and stored
                #   by the controller which receives them.

                assert len(bidirectional_path), "bidirectional_path path length cannot be zero."

                # Allocate MPLS label for tunnel
                if len(bidirectional_path) >= 3:
                    local_mpls_label = globals.alloc_mpls_label_id()
                else:
                    local_mpls_label = None

                local_service_scenario = services.icmpv4_flow_activation(
                    bidirectional_path, local_mpls_label, scenario_mpls_label, source_ipv4=source_ipv4_str
                )
                # If it reached here, then it means the path was successfully activated.
                globals.set_active_scenario(
                    global_path_search_id,
                    ((id(local_service_scenario),), (sector_requesting_service_id,))
                )

                # kspl = globals.get_known_shortest_path(
                #     this_controller_id,
                #     target_ipv4_str
                # )
                # if kspl and kspl > len(bidirectional_path):
                #     globals.set_known_shortest_path(
                #         this_controller_id,
                #         target_ipv4_str,
                #         len(bidirectional_path)
                #     )
                # else:
                #     globals.set_known_shortest_path(
                #         this_controller_id,
                #         target_ipv4_str,
                #         len(bidirectional_path)
                #     )
                # kspl = globals.get_known_shortest_path(
                #     this_controller_id,
                #     target_ipv4_str
                # )
                # assert kspl, "kspl cannot be Zero or None."
                #
                # reward = bidirectional_path.remaining_bandwidth_average/kspl*len(bidirectional_path)
                #
                # old_q_value = globals.get_q_value(this_controller_id, target_ipv4_str)
                # new_q_value = globals.calculate_new_qvalue(old_q_value, 1, reward)
                # globals.set_q_value(this_controller_id, target_ipv4_str, new_q_value)
                #
                # _log.info(
                #     "Updated Q-Values -> "
                #     "Old Q-Value: {:f}; "
                #     "New Q-Value: {:f}; "
                #     "Reward: {:f}; "
                #     "Forward Q-Value: {:f}; "
                #     "KSPL: {:d};"
                #     "".format(
                #         old_q_value, new_q_value, reward, 1, kspl
                #     )
                # )

                _log.debug(
                    "Local Scenario with Global ID {:s} and local length {:d} is now active.".format(
                        str(global_path_search_id),
                        len(bidirectional_path)
                    )
                )

                return {
                    "success": True,
                    "global_path_search_id": global_path_search_id,
                    "q_value": 1,
                    "path_length": len(bidirectional_path) - 1
                }

            else:
                # This IS NOT the target sector
                adjacent_sectors_ids = sector.query_sectors_ids()

                adjacent_sectors_ids.remove(sector_requesting_service_id)

                if len(adjacent_sectors_ids) == 0:
                    return {"success": False, "reason": "No available sectors to explore."}

                if target_host_info.controller_id in adjacent_sectors_ids:
                    # The possible communication links to the target sector
                    possible_links = sector.query_edges_to_sector(target_host_info.controller_id)
                    selected_link = tuple()
                    bidirectional_path = tuple()

                    while possible_links:
                        # First, lets choose a link to the adjacent sector, according to the q-value
                        links_never_used = tuple(
                            filter(
                                (lambda link: globals.get_q_value((link[0], link[1]), target_ipv4) == 0),
                                possible_links
                            )
                        )
                        if len(links_never_used):
                            selected_link = links_never_used[0]
                        else:
                            selected_link = max(
                                possible_links,
                                key=(lambda link: globals.get_q_value((link[0], link[1]), target_ipv4))
                            )
                        try:
                            # If the target sector IS adjacent to this sector, contact it directly and establish path
                            bidirectional_path = sector.construct_bidirectional_path(
                                sector_requesting_service_id,
                                target_host_info.controller_id,
                                allocated_bandwith=100,
                                previous_sector_hash=scenario_hash_val,
                                next_sector_hash=selected_link[2]
                            )
                            break

                        except PathNotFound:
                            possible_links.remove(selected_link)
                            if len(possible_links) == 0:
                                raise

                    assert len(bidirectional_path), "bidirectional_path path length cannot be zero."
                    assert isinstance(selected_link, tuple), "selected_link expected to be tuple"

                    # Allocate MPLS label for tunnel (required when communicating with Sectors)
                    local_mpls_label = globals.alloc_mpls_label_id()
                    chosen_edge = (selected_link[0], selected_link[1])
                    selected_sector_proxy = get_controller_proxy(target_host_info.controller_id)
                    try:
                        service_activation_result = selected_sector_proxy.activate_scenario(
                            {
                                "global_path_search_id": global_path_search_id,
                                "sector_requesting_service": str(this_controller_id),
                                "mpls_label": local_mpls_label,
                                "hash_val": globals.get_hash_val(*chosen_edge),
                            }
                        )
                    except Exception as ex:
                        service_activation_result = {"success": False, "reason": str(ex)}

                    forward_q_value = 0 if "q_value" not in service_activation_result else service_activation_result[
                        "q_value"]

                    if service_activation_result["success"]:
                        kspl = globals.get_known_shortest_path(
                            chosen_edge,
                            target_ipv4
                        )
                        if kspl and kspl > service_activation_result["path_length"] + 1:
                            globals.set_known_shortest_path(
                                chosen_edge,
                                target_ipv4,
                                service_activation_result["path_length"] + 1
                            )
                        else:
                            globals.set_known_shortest_path(
                                chosen_edge,
                                target_ipv4,
                                service_activation_result["path_length"] + 1
                            )
                        kspl = globals.get_known_shortest_path(
                            chosen_edge,
                            target_ipv4
                        )
                        assert kspl, "kspl cannot be Zero or None."

                        reward = bidirectional_path.remaining_bandwidth_average / kspl

                        old_q_value = globals.get_q_value(chosen_edge, target_ipv4)
                        new_q_value = globals.calculate_new_qvalue(old_q_value, forward_q_value, reward)
                        globals.set_q_value(chosen_edge, target_ipv4, new_q_value)

                        local_service_scenario = services.sector_to_sector_mpls_flow_activation(
                            bidirectional_path, local_mpls_label, scenario_mpls_label
                        )

                        globals.set_active_scenario(
                            global_path_search_id,
                            (
                                (id(local_service_scenario),),
                                (sector_requesting_service_id, target_host_info.controller_id)
                            )
                        )

                        _log.info(
                            "Adjacent Sector: {:s}; "
                            "Chosen link: {:s}; "
                            "Updated Q-Values -> "
                            "Old Q-Value: {:f}; "
                            "New Q-Value: {:f}; "
                            "Reward: {:f}; "
                            "Forward Q-Value: {:f};"
                            "KSPL: {:d};"
                            "".format(
                                str(target_host_info.controller_id), str(chosen_edge),
                                old_q_value, new_q_value, reward, forward_q_value, kspl
                            )
                        )

                        _log.debug(
                            "Local Scenario with Global ID {:s} with local length {:d} is now active.".format(
                                str(global_path_search_id),
                                len(bidirectional_path)
                            )
                        )

                        return {
                            "success": True,
                            "global_path_search_id": global_path_search_id,
                            "q_value": new_q_value,
                            "path_length": len(bidirectional_path) + service_activation_result["path_length"] - 1
                        }
                    else:
                        old_q_value = globals.get_q_value(target_host_info.controller_id, target_ipv4)
                        new_q_value = globals.calculate_new_qvalue(old_q_value, forward_q_value, -1)
                        globals.set_q_value(target_host_info.controller_id, target_ipv4, new_q_value)

                        _log.info(
                            "Adjacent Sector: {:s}; "
                            "Chosen link: {:s}; "
                            "Updated Q-Values -> "
                            "Old Q-Value: {:f}; "
                            "New Q-Value: {:f}; "
                            "Reward: {:f}; "
                            "Forward Q-Value: {:f}.".format(
                                str(target_host_info.controller_id), str(chosen_edge),
                                old_q_value, new_q_value, -1, forward_q_value
                            )
                        )
                        _log.debug("Failed to activate Scenario with ID {:s} through sector {:s}. "
                                   "Reason: {:s}.".format(
                                        str(target_host_info.controller_id),
                                        str(global_path_search_id),
                                        service_activation_result["reason"]
                                    ),
                        )

                        return {
                            "success": False,
                            "reason": "No available sectors to explore.",
                        }

                else:

                    _log.debug("Available adjacent sectors for exploration: {}".format(adjacent_sectors_ids))
                    # The possible communication links to the target sector
                    possible_links = []
                    for adjacent_sector in adjacent_sectors_ids:
                        for edge in sector.query_edges_to_sector(adjacent_sector):
                            possible_links.append((edge[0], edge[1], edge[2], adjacent_sector))

                    _log.debug(
                        "Available Sector Links for exploration: [{:s}]".format(
                            "][".join(tuple((str(i) for i in possible_links)))
                        )
                    )

                    while possible_links:
                        # First, lets choose a link to the adjacent sector, according to the q-value
                        links_never_used = tuple(
                            filter(
                                (lambda link: globals.get_q_value((link[0], link[1]), target_ipv4) == 0),
                                possible_links
                            )
                        )
                        if len(links_never_used):
                            selected_link = links_never_used[0]
                        else:
                            selected_link = max(
                                possible_links,
                                key=(lambda link: globals.get_q_value((link[0], link[1]), target_ipv4))
                            )

                        possible_links.remove(selected_link)
                        chosen_edge = selected_link[0:2]
                        selected_sector_id = selected_link[3]

                        _log.debug(
                            "Selected Link {:s}{:s}".format(
                                str(selected_link),
                                " from {}.".format(possible_links) if len(possible_links) else "."
                            )
                        )

                        try:
                            # Acquire a bidirectional path
                            bidirectional_path = sector.construct_bidirectional_path(
                                sector_requesting_service_id,
                                selected_sector_id,
                                allocated_bandwith=100,
                                previous_sector_hash=scenario_hash_val,
                                next_sector_hash=selected_link[2]
                            )
                        except PathNotFound:
                            if len(possible_links) == 0:
                                raise
                            continue

                        assert len(bidirectional_path), "bidirectional_path path length cannot be zero."
                        assert isinstance(selected_link, tuple), "selected_link expected to be tuple"
                        assert selected_sector_id is not None, "selected_sector_id cannot be None"

                        # Allocate MPLS label for tunnel (required when communicating with Sectors)
                        local_mpls_label = globals.alloc_mpls_label_id()
                        try:
                            selected_sector_proxy = get_controller_proxy(selected_sector_id)
                            service_activation_result = selected_sector_proxy.activate_scenario(
                                {
                                    "global_path_search_id": global_path_search_id,
                                    "sector_requesting_service": str(this_controller_id),
                                    "mpls_label": local_mpls_label,
                                    "hash_val": globals.get_hash_val(*chosen_edge),
                                }
                            )
                        except Exception as ex:
                            service_activation_result = {"success": False, "reason": str(ex)}

                        forward_q_value = 0 if "q_value" not in service_activation_result else service_activation_result["q_value"]

                        if service_activation_result["success"]:
                            kspl = globals.get_known_shortest_path(
                                chosen_edge,
                                target_ipv4
                            )
                            if kspl and kspl > service_activation_result["path_length"] + 1:
                                globals.set_known_shortest_path(
                                    chosen_edge,
                                    target_ipv4,
                                    service_activation_result["path_length"] + 1
                                )
                            else:
                                globals.set_known_shortest_path(
                                    chosen_edge,
                                    target_ipv4,
                                    service_activation_result["path_length"] + 1
                                )
                            kspl = globals.get_known_shortest_path(
                                chosen_edge,
                                target_ipv4
                            )
                            assert kspl, "kspl cannot be Zero or None."

                            reward = bidirectional_path.remaining_bandwidth_average / kspl
                            old_q_value = globals.get_q_value(chosen_edge, target_ipv4)
                            new_q_value = globals.calculate_new_qvalue(old_q_value, forward_q_value, reward)
                            globals.set_q_value(chosen_edge, target_ipv4, new_q_value)

                            entity_a_obj = sector.query_entity(bidirectional_path.entity_a)
                            entity_b_obj = sector.query_entity(bidirectional_path.entity_b)

                            if isinstance(entity_a_obj, Sector) and isinstance(entity_b_obj, Sector):
                                local_service_scenario = services.sector_to_sector_mpls_flow_activation(
                                    bidirectional_path, local_mpls_label, scenario_mpls_label
                                )
                            else:
                                local_service_scenario = services.icmpv4_flow_activation(
                                    bidirectional_path, local_mpls_label, scenario_mpls_label
                                )

                            globals.set_active_scenario(
                                global_path_search_id,
                                (
                                    (id(local_service_scenario),),
                                    (sector_requesting_service_id, selected_sector_id)
                                )
                            )

                            _log.info(
                                "Adjacent Sector: {:s}; "
                                "Chosen link: {:s}; "
                                "Updated Q-Values -> "
                                "Old Q-Value: {:f}; "
                                "New Q-Value: {:f}; "
                                "Reward: {:f}; "
                                "Forward Q-Value: {:f}."
                                "KSPL: {:d};"
                                "".format(
                                    str(selected_sector_id), str(chosen_edge),
                                    old_q_value, new_q_value, reward, forward_q_value, kspl
                                )
                            )

                            _log.debug(
                                "Local Scenario with global ID {:s} with local length {:d} is now active.".format(
                                    str(global_path_search_id),
                                    len(bidirectional_path)
                                )
                            )
                            return {
                                "success": True,
                                "global_path_search_id": global_path_search_id,
                                "q_value": new_q_value,
                                "path_length": len(bidirectional_path) + service_activation_result["path_length"] - 1
                            }

                        else:
                            old_q_value = globals.get_q_value(chosen_edge, target_ipv4)
                            new_q_value = globals.calculate_new_qvalue(old_q_value, forward_q_value, -1)
                            globals.set_q_value(chosen_edge, target_ipv4, new_q_value)

                            _log.info(
                                "Adjacent Sector: {:s}; "
                                "Chosen link: {:s}; "
                                "Updated Q-Values -> "
                                "Old Q-Value: {:f}; "
                                "New Q-Value: {:f}; "
                                "Reward: {:f}; "
                                "Forward Q-Value: {:f}."
                                "".format(
                                    str(selected_sector_id), str(chosen_edge),
                                    old_q_value, new_q_value, -1, forward_q_value
                                )
                            )

                            _log.error(
                                "Failed to activate Scenario with Global ID {:s} through Sector {:s}. Reason {:s}.".format(
                                    str(global_path_search_id),
                                    str(selected_sector_id),
                                    service_activation_result["reason"]
                                )
                            )

                    error_str = "Failed to activate Scenario with ID {:s}. " \
                                "Alternative adjacent sectors options is exhausted.".format(
                                    str(global_path_search_id),
                                )
                    _log.error(error_str)
                    return {
                        "success": False,
                        "reason": error_str,
                    }

        except globals.ImplementationTaskExists:
            error_str = "Global task with ID {:s} is already being executed".format(str(global_path_search_id))
            _log.error(error_str)
            custom_logging_callback(_log, logging.ERROR, *sys.exc_info())
            return {"success": False, "reason": error_str}

        except PathNotFound:
            error_str = "Failed to implement path to sector {:s}. " \
                        "An available path was not found in the network.".format(
                            str(target_host_info.controller_id)
                        )
            _log.error(error_str)
            custom_logging_callback(_log, logging.ERROR, *sys.exc_info())
            return {"success": False, "reason": error_str}

        except Exception as ex:
            error_str = "Failed to implement path to host {:s} at sector {:s}. Reason {:s}.".format(
                target_host_info.name,
                str(target_host_info.controller_id),
                str(type(ex))
            )
            _log.error(error_str)
            custom_logging_callback(_log, logging.ERROR, *sys.exc_info())
            return {"success": False, "reason": error_str}

    elif scenario_type == 'IPv4':
        try:
            # Maintain an active token during the duration of this task. When the task is terminated, the token will
            #   be removed.
            active_task_token = globals.register_implementation_task(global_path_search_id, "IPv4", "*")

            if target_host_info.controller_id == this_controller_id:
                # This IS the target sector

                # Trying to activate the local path, to finish the global path.
                unidirectional_path = sector.construct_unidirectional_path(
                    sector_requesting_service_id,  # Sector from which the scenario request came
                    target_host_info.name,  # Target Hostname which identifies the host entity in this sector.
                    previous_sector_hash=scenario_hash_val  # hash value which identifies the switch that sends the traffic
                )
                # Implementation notes:
                #  'sector_a_hash_val' is necessary for the sector controller. In the use-case where multiple switches
                #   are connected to the same sector, the controller from that sector uses the 'sector_a_hash_val' to
                #   distinguish between switches. The 'sector_a_hash_val' is sent in each discovery beacon, and stored
                #   by the controller which receives them.

                assert len(unidirectional_path), "unidirectional_path path length cannot be zero."

                # Allocate MPLS label for tunnel
                if len(unidirectional_path) >= 3:
                    local_mpls_label = globals.alloc_mpls_label_id()
                else:
                    local_mpls_label = None

                local_service_scenario = services.ipv4_generic_flow_activation(
                    unidirectional_path, local_mpls_label, scenario_mpls_label, source_ipv4=source_ipv4_str
                )
                # If it reached here, then it means the path was successfully activated.
                globals.set_active_scenario(
                    global_path_search_id,
                    ((id(local_service_scenario),), (sector_requesting_service_id,))
                )

                # kspl = globals.get_known_shortest_path(
                #     this_controller_id,
                #     target_ipv4_str
                # )
                # if kspl and kspl > len(unidirectional_path):
                #         globals.set_known_shortest_path(
                #             this_controller_id,
                #             target_ipv4_str,
                #             len(unidirectional_path)
                #         )
                # else:
                #     globals.set_known_shortest_path(
                #         this_controller_id,
                #         target_ipv4_str,
                #         len(unidirectional_path)
                #     )
                # kspl = globals.get_known_shortest_path(
                #     this_controller_id,
                #     target_ipv4_str
                # )
                # assert kspl, "kspl cannot be Zero or None."
                #
                # reward = unidirectional_path.remaining_bandwidth_average/kspl*len(unidirectional_path)
                #
                # old_q_value = globals.get_q_value(this_controller_id, target_ipv4_str)
                # new_q_value = globals.calculate_new_qvalue(old_q_value, 1, reward)
                # globals.set_q_value(this_controller_id, target_ipv4_str, new_q_value)
                #
                # _log.info(
                #     "Updated Q-Values -> "
                #     "Old Q-Value: {:f}; "
                #     "New Q-Value: {:f}; "
                #     "Reward: {:f}; "
                #     "Forward Q-Value: {:f}."
                #     "KSPL: {:d};"
                #     "".format(
                #         old_q_value, new_q_value, reward, 1, kspl
                #     )
                # )

                _log.info(
                    "Local Scenario with Global ID {:s} and local length {:d} is now active.".format(
                        str(global_path_search_id),
                        len(unidirectional_path)
                    )
                )

                return {
                    "success": True,
                    "global_path_search_id": global_path_search_id,
                    "q_value": 1,
                    "path_length": len(unidirectional_path) - 1
                }

            else:
                # This IS NOT the target sector
                adjacent_sectors_ids = sector.query_sectors_ids()

                adjacent_sectors_ids.remove(sector_requesting_service_id)

                if len(adjacent_sectors_ids) == 0:
                    return {"success": False, "reason": "No available sectors to explore."}

                if target_host_info.controller_id in adjacent_sectors_ids:
                    # If the target sector IS adjacent to this sector, contact it directly and establish path
                    unidirectional_path = sector.construct_unidirectional_path(
                        sector_requesting_service_id,
                        target_host_info.controller_id,
                        previous_sector_hash=scenario_hash_val
                    )
                    assert len(unidirectional_path), "unidirectional_path path length cannot be zero."

                    # Allocate MPLS label for tunnel (required when communicating with Sectors)
                    local_mpls_label = globals.alloc_mpls_label_id()

                    (switch_id, _, port_out) = unidirectional_path.path[-2]
                    selected_sector_proxy = get_controller_proxy(target_host_info.controller_id)
                    try:
                        service_activation_result = selected_sector_proxy.activate_scenario(
                            {
                                "global_path_search_id": global_path_search_id,
                                "sector_requesting_service": str(this_controller_id),
                                "mpls_label": local_mpls_label,
                                "hash_val": globals.get_hash_val(switch_id, port_out),
                            }
                        )
                    except Exception as ex:
                        service_activation_result = {"success": False, "reason": str(ex)}

                    forward_q_value = 0 if "q_value" not in service_activation_result else service_activation_result[
                        "q_value"]
                    if service_activation_result["success"]:
                        kspl = globals.get_known_shortest_path(
                            target_host_info.controller_id,
                            target_ipv4
                        )
                        if kspl and kspl > service_activation_result["path_length"] + 1:
                                globals.set_known_shortest_path(
                                    target_host_info.controller_id,
                                    target_ipv4,
                                    service_activation_result["path_length"] + 1
                                )
                        else:
                            globals.set_known_shortest_path(
                                target_host_info.controller_id,
                                target_ipv4,
                                service_activation_result["path_length"] + 1
                            )
                        kspl = globals.get_known_shortest_path(
                            target_host_info.controller_id,
                            target_ipv4
                        )
                        assert kspl, "kspl cannot be Zero or None."

                        reward = unidirectional_path.remaining_bandwidth_average / kspl

                        old_q_value = globals.get_q_value(target_host_info.controller_id, target_ipv4_str)
                        new_q_value = globals.calculate_new_qvalue(old_q_value, forward_q_value, reward)
                        globals.set_q_value(target_host_info.controller_id, target_ipv4_str, new_q_value)

                        local_service_scenario = services.sector_to_sector_mpls_flow_activation(
                            unidirectional_path, local_mpls_label, scenario_mpls_label
                        )

                        globals.set_active_scenario(
                            global_path_search_id,
                            (
                                (id(local_service_scenario),),
                                (sector_requesting_service_id, target_host_info.controller_id)
                            )
                        )

                        _log.info(
                            "Adjacent Sector: {:s}; "
                            "Updated Q-Values -> "
                            "Old Q-Value: {:f}; "
                            "New Q-Value: {:f}; "
                            "Reward: {:f}; "
                            "Forward Q-Value: {:f}."
                            "KSPL: {:d};"
                            "".format(
                                str(target_host_info.controller_id),
                                old_q_value, new_q_value, reward, forward_q_value, kspl
                            )
                        )

                        _log.debug(
                            "Local Scenario with Global ID {:s} and local length {:d} is now active.".format(
                                str(global_path_search_id),
                                len(unidirectional_path)
                            )
                        )

                        return {
                            "success": True,
                            "global_path_search_id": global_path_search_id,
                            "q_value": new_q_value,
                            "path_length": len(unidirectional_path) + service_activation_result["path_length"] - 1
                        }

                    else:
                        old_q_value = globals.get_q_value(target_host_info.controller_id, target_ipv4_str)
                        new_q_value = globals.calculate_new_qvalue(old_q_value, forward_q_value, -1)
                        globals.set_q_value(target_host_info.controller_id, target_ipv4_str, new_q_value)

                        _log.info(
                            "Adjacent Sector: {:s}; "
                            "Updated Q-Values -> "
                            "Old Q-Value: {:f}; "
                            "New Q-Value: {:f}; "
                            "Reward: {:f}; "
                            "Forward Q-Value: {:f}."
                            "".format(
                                str(target_host_info.controller_id),
                                old_q_value, new_q_value, -1, forward_q_value
                            )
                        )
                        _log.error("Failed to activate Scenario with ID {:s} through sector {:s}. Reason: {:s}."
                                   "".format(
                                        str(target_host_info.controller_id),
                                        str(global_path_search_id),
                                        service_activation_result["reason"]
                                    ),
                        )

                        return {
                            "success": False,
                            "reason": "No available sectors to explore.",
                        }

                else:
                    while len(adjacent_sectors_ids):
                        _log.debug("Available adjacent sectors for exploration: {}".format(adjacent_sectors_ids))

                        # Selecting a Sector based on the Q-Value
                        sectors_never_used = tuple(
                            filter(
                                (lambda sector_id: globals.get_q_value(sector_id, target_ipv4) == 0),
                                adjacent_sectors_ids
                            )
                        )
                        if len(sectors_never_used):
                            selected_sector_id = sectors_never_used[0]
                        else:
                            selected_sector_id = max(
                                adjacent_sectors_ids,
                                key=(lambda sector_id: globals.get_q_value(sector_id, target_ipv4))
                            )

                        adjacent_sectors_ids.remove(selected_sector_id)
                        _log.debug(
                            "Selected sector {:s}".format(
                                str(selected_sector_id),
                                " from {}.".format(adjacent_sectors_ids) if len(adjacent_sectors_ids) else "."
                            )
                        )
                        ####################

                        # Acquire a unidirectional path
                        unidirectional_path = sector.construct_unidirectional_path(
                            sector_requesting_service_id,
                            selected_sector_id,
                            previous_sector_hash=scenario_hash_val
                        )
                        assert len(unidirectional_path), "unidirectional_path path length cannot be zero."

                        # Allocate MPLS label for tunnel (required when communicating with Sectors)
                        local_mpls_label = globals.alloc_mpls_label_id()

                        (switch_id, _, port_out) = unidirectional_path.path[-2]
                        try:
                            selected_sector_proxy = get_controller_proxy(selected_sector_id)
                            service_activation_result = selected_sector_proxy.activate_scenario(
                                {
                                    "global_path_search_id": global_path_search_id,
                                    "sector_requesting_service": str(this_controller_id),
                                    "mpls_label": local_mpls_label,
                                    "hash_val": globals.get_hash_val(switch_id, port_out),
                                }
                            )
                        except Exception as ex:
                            service_activation_result = {"success": False, "reason": str(ex)}

                        forward_q_value = 0 if "q_value" not in service_activation_result else service_activation_result["q_value"]

                        if service_activation_result["success"]:
                            kspl = globals.get_known_shortest_path(
                                selected_sector_id,
                                target_ipv4
                            )
                            if kspl and kspl > service_activation_result["path_length"] + 1:
                                globals.set_known_shortest_path(
                                    selected_sector_id,
                                    target_ipv4,
                                    service_activation_result["path_length"] + 1
                                )
                            else:
                                globals.set_known_shortest_path(
                                    selected_sector_id,
                                    target_ipv4,
                                    service_activation_result["path_length"] + 1
                                )
                            kspl = globals.get_known_shortest_path(
                                selected_sector_id,
                                target_ipv4
                            )
                            assert kspl, "kspl cannot be Zero or None."

                            reward = unidirectional_path.remaining_bandwidth_average / kspl
                            old_q_value = globals.get_q_value(selected_sector_id, target_ipv4_str)
                            new_q_value = globals.calculate_new_qvalue(old_q_value, forward_q_value, reward)
                            globals.set_q_value(selected_sector_id, target_ipv4_str, new_q_value)

                            _log.info(
                                "Selected Sector: {:s}; "
                                "Updated Q-Values -> "
                                "Old Q-Value: {:f}; "
                                "New Q-Value: {:f}; "
                                "Reward: {:f}; "
                                "Forward Q-Value: {:f}."
                                "KSPL: {:d};"
                                "".format(
                                    str(selected_sector_id),
                                    old_q_value, new_q_value, reward, forward_q_value, kspl
                                )
                            )
                            entity_a_obj = sector.query_entity(unidirectional_path.entity_a)
                            entity_b_obj = sector.query_entity(unidirectional_path.entity_b)

                            if isinstance(entity_a_obj, Sector) and isinstance(entity_b_obj, Sector):
                                local_service_scenario = services.sector_to_sector_mpls_flow_activation(
                                    unidirectional_path, local_mpls_label, scenario_mpls_label
                                )
                            else:
                                local_service_scenario = services.ipv4_generic_flow_activation(
                                    unidirectional_path, local_mpls_label, scenario_mpls_label
                                )

                            globals.set_active_scenario(
                                global_path_search_id,
                                (
                                    (id(local_service_scenario),),
                                    (sector_requesting_service_id, selected_sector_id)
                                )
                            )

                            _log.info(
                                "Local Scenario with ID {:s} and local length {:d} is now active.".format(
                                    str(global_path_search_id),
                                    len(unidirectional_path)
                                )
                            )
                            return {
                                "success": True,
                                "global_path_search_id": global_path_search_id,
                                "q_value": new_q_value,
                                "path_length": len(unidirectional_path) + service_activation_result["path_length"] - 1
                            }

                        else:
                            old_q_value = globals.get_q_value(selected_sector_id, target_ipv4_str)
                            new_q_value = globals.calculate_new_qvalue(old_q_value, forward_q_value, -1)
                            globals.set_q_value(selected_sector_id, target_ipv4_str, new_q_value)

                            _log.info(
                                "Selected Sector: {:s}; "
                                "Updated Q-Values -> "
                                "Old Q-Value: {:f}; "
                                "New Q-Value: {:f}; "
                                "Reward: {:f}; "
                                "Forward Q-Value: {:f}."
                                "".format(
                                    str(selected_sector_id),
                                    old_q_value, new_q_value, -1, forward_q_value
                                )
                            )

                            _log.error(
                                "Failed to activate Scenario with ID {:s} through Sector {:s}. Reason {:s}.".format(
                                    str(global_path_search_id),
                                    str(selected_sector_id),
                                    service_activation_result["reason"]
                                )
                            )

                    error_str = "Failed to activate Scenario with ID {:s}. " \
                                "Alternative adjacent sectors options is exhausted.".format(
                                    str(global_path_search_id),
                                )
                    _log.error(error_str)
                    return {
                        "success": False,
                        "reason": error_str,
                    }

        except globals.ImplementationTaskExists:
            error_str = "Global task with ID {:s} is already being executed".format(str(global_path_search_id))
            _log.error(error_str)
            custom_logging_callback(_log, logging.ERROR, *sys.exc_info())
            return {"success": False, "reason": error_str}

        except PathNotFound:
            error_str = "Failed to implement path to sector {:s}. " \
                        "An available path was not found in the network.".format(
                            str(target_host_info.controller_id)
                        )
            _log.error(error_str)
            custom_logging_callback(_log, logging.ERROR, *sys.exc_info())
            return {"success": False, "reason": error_str}

        except Exception as ex:
            error_str = "Failed to implement path to host {:s} at sector {:s}. Reason {:s}.".format(
                target_host_info.name,
                str(target_host_info.controller_id),
                str(type(ex))
            )
            _log.error(error_str)
            custom_logging_callback(_log, logging.ERROR, *sys.exc_info())
            return {"success": False, "reason": error_str}

    else:
        error_str = "Failed to activate Scenario with ID {:s}. Invalid Scenario Type: {:s}".format(
                        str(global_path_search_id),
                        scenario_type
                    )

        _log.error(error_str)
        return {
            "success": False,
            "reason": error_str,
        }


def __terminate_scenario(scenario_request):
    from archsdn.engine import globals

    assert isinstance(scenario_request, dict), \
        "scenario_request is expected to be of dict type. Got {:s}.".format(repr(scenario_request))
    missing_args = tuple(
        filter(
            (lambda arg: arg not in scenario_request.keys()),
            ('global_path_search_id', 'requesting_sector_id')
        )
    )

    if missing_args:
        raise TypeError("The following arguments are missing: {:s}".format(", ".join(missing_args)))

    global_path_search_id = scenario_request["global_path_search_id"]

    if not globals.is_scenario_active(global_path_search_id):
        return {
            "success": False,
            "reason": "Path with ID {:s} registration does not exist.".format(str(global_path_search_id))
        }

    try:
        this_controller_id = database.get_database_info()['uuid']
        requesting_sector_id = UUID(scenario_request["requesting_sector_id"])
        (local_scenarios_ids_list, adjacent_sectors_ids) = globals.get_active_scenario(global_path_search_id, True)
        local_scenarios_to_kill = []

        for network_service in globals.mapped_services:
            for service_type in globals.mapped_services[network_service]:
                for entities_ids_pair in tuple(globals.mapped_services[network_service][service_type]):
                    scenario = globals.mapped_services[network_service][service_type][entities_ids_pair]

                    if id(scenario) in local_scenarios_ids_list:
                        local_scenarios_to_kill.append(scenario)
                        del globals.mapped_services[network_service][service_type][entities_ids_pair]

        _log.debug(
            "Local Scenarios to be destroyed: {:s}".format(
                str(tuple((id(i) for i in local_scenarios_to_kill)))
            )
        )

        for sector_id in set(adjacent_sectors_ids) - {requesting_sector_id}:
            sector_proxy = get_controller_proxy(sector_id)
            _log.debug(
                "Contacting Sector {:s} to destroy path {:s}...".format(
                    str(sector_id),
                    str(global_path_search_id)
                )
            )
            res = sector_proxy.terminate_scenario(
                {
                    "global_path_search_id": global_path_search_id,
                    "requesting_sector_id": str(this_controller_id)
                }
            )
            _log.debug(
                "Sector {:s} answer is: {:s}".format(
                    str(sector_id),
                    str(res)
                )
            )
        _log.warning(
            "Global scenario {:s} destroyed.".format(str(global_path_search_id))
        )
        return {"success": True, "global_path_search_id": global_path_search_id}

    except Exception as ex:
        error_str = "Failed to terminate scenario with ID {:s}. Reason {:s}.".format(
            str(global_path_search_id),
            str(type(ex))
        )
        _log.error(error_str)
        custom_logging_callback(_log, logging.DEBUG, *sys.exc_info())
        raise Exception(error_str)


_requests = {
    "req_local_time": __req_local_time,
    "publish_event": __publish_event,
    "query_address_info": __query_address_info,
    "activate_scenario": __activate_scenario,
    "terminate_scenario": __terminate_scenario
}
