import logging
import sys
import signal
from pathlib import Path

from uuid import uuid4, UUID
from ipaddress import IPv4Address, IPv6Address, ip_address
from netaddr import mac_eui48

from ryu.cfg import CONF
from ryu.base.app_manager import RyuApp
from ryu.controller.dpset import EventDP
from ryu.controller import ofp_event
from ryu.controller.handler import MAIN_DISPATCHER
from ryu.controller.handler import set_ev_cls
from ryu.ofproto import ofproto_v1_3
import ryu.app.ofctl.api as ryu_api

from archsdn import central
from archsdn import database
from archsdn.helpers import custom_logging_callback, logger_module_name

# MAC Sword separator definition
mac_eui48.word_sep = ":"

# Initialize Exception Hook
sys.excepthook = (lambda tp, val, tb: custom_logging_callback(logging.getLogger(), logging.ERROR, tp, val, tb))

# Initialize logger for this module
_log_format = '[{asctime:^s}][{levelname:^8s}][{name:s}|{funcName:s}|{lineno:d}]: {message:s}'
_log_datefmt = '%Y/%m/%d|%H:%M:%S.%f (%Z)'

_log = logging.getLogger(logger_module_name(__file__))

default_configs = {
    "id": uuid4(),
    "controllerIP": IPv4Address("0.0.0.0"),
    "controllerPort": 12345,
    "centralIP": None,
    "centralIP_port": 12345,
    "dbLocation": ":memory:",
    "logLevel": 'DEBUG' if sys.flags.debug else 'INFO'
}


def _quit_callback(signum, frame):
    logging.shutdown()
    sys.exit()


class ArchSDN(RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        global _log, _log_format, _log_datefmt

        try:
            super(ArchSDN, self).__init__(*args, **kwargs)
            signal.signal(signal.SIGINT, _quit_callback)
            signal.signal(signal.SIGTERM, _quit_callback)

            def send_msg(*args, **kwargs):
                return ryu_api.send_msg(self, *args, **kwargs)

            def get_datapath(*args, **kwargs):
                return ryu_api.get_datapath(self, *args, **kwargs)

            default_configs["send_msg"] = send_msg
            default_configs["get_datapath"] = get_datapath

            # ArchSDN CLI options evaluation
            archSDN_cli_opts = dict(tuple(((key, CONF[key]) for key in CONF if "archSDN" in key and CONF[key])))

            if 'archSDN_id' in archSDN_cli_opts:
                default_configs['id'] = UUID(archSDN_cli_opts['archSDN_id'])
                
            if 'archSDN_controllerIP' in archSDN_cli_opts:
                default_configs['controllerIP'] = ip_address(archSDN_cli_opts['archSDN_controllerIP'])

            if 'archSDN_controllerIPPort' in archSDN_cli_opts:
                default_configs['controllerPort'] = int(archSDN_cli_opts['archSDN_controllerPort'])

            if 'archSDN_centralIP' in archSDN_cli_opts:
                default_configs['centralIP'] = ip_address(archSDN_cli_opts['archSDN_centralIP'])

            if 'archSDN_centralPort' in archSDN_cli_opts:
                default_configs['centralPort'] = int(archSDN_cli_opts['archSDN_centralPort'])

            if 'archSDN_dbLocation' in archSDN_cli_opts:
                default_configs['dbLocation'] = archSDN_cli_opts['archSDN_dbLocation']

            if 'archSDN_logLevel' in archSDN_cli_opts:
                default_configs['logLevel'] = archSDN_cli_opts['archSDN_logLevel']

            # This is a hack of the logging mechanism, to override ryu default configuration
            root_log = logging.getLogger()
            for handler in root_log.handlers:
                handler.setFormatter(
                    logging.Formatter(
                        fmt=_log_format, datefmt=_log_datefmt, style='{'
                    )
                )
                handler.setLevel = default_configs['logLevel']
            _log.setLevel = default_configs['logLevel']
            #  End logging hack

            _log.info(
                "Default Configurations: {:s}".format(
                    "; ".join(list(("{}: {}".format(key, default_configs[key]) for key in default_configs)))
                )
            )

            # Initialising database
            if default_configs['dbLocation'] != ':memory:':
                default_configs['dbLocation'] = Path(default_configs['dbLocation'])
                if not default_configs['dbLocation'].parent.exists():
                    raise SystemError(
                        "Database location directory does not exist: {:s}".format(
                            str(default_configs['dbLocation'].parent)
                        )
                    )
            database.initialise(
                location=default_configs['dbLocation'],
                controller_id=default_configs['id']
            )

            central.initialise(  # Initialising communication with central
                central_ip=default_configs['centralIP'],
                central_port=default_configs['centralPort']
            )

            ipv4_info = (default_configs['controllerIP'], default_configs['controllerPort']) \
                if default_configs['controllerIP'].version == 4 else None
            ipv6_info = (default_configs['controllerIP'], default_configs['controllerPort']) \
                if default_configs['controllerIP'].version == 6 else None
            try:
                central.register_controller(
                    controller_id=default_configs['id'],
                    ipv4_info=ipv4_info,
                    ipv6_info=ipv6_info,
                )
            except central.ControllerAlreadyRegistered:
                _log.warning("This controller was already registered at the Central somewhere in the past.")
                if default_configs["dbLocation"] == ":memory:":
                    # If the database is in-memory, remove all client registrations at Central
                    try:
                        central.update_controller_address(
                            controller_id=default_configs['id'],
                            ipv4_info=ipv4_info,
                            ipv6_info=ipv6_info,
                        )
                    except (central.IPv4InfoAlreadyRegistered, central.IPv6InfoAlreadyRegistered):
                        _log.warning("Controller IP address did not change at the Central Manager.")
                else:
                    # If the database is in-file, sync all local client registrations with Central information
                    for datapath_id in database.dump_datapth_registered_ids():
                        for client_id in database.dump_datapth_registered_clients_ids(datapath_id):
                            try:
                                local_client_info = database.query_client_info(client_id)
                                central_client_info = central.query_client_info(default_configs['id'], client_id)

                                if (local_client_info["ipv4"] != central_client_info.ipv4) or \
                                        (local_client_info["ipv6"] != central_client_info.ipv6):
                                    if central_client_info.ipv4 is not None:
                                        _log.info(
                                            "Updating client {:d} with new IPv4 {:s}".format(
                                                client_id, str(central_client_info.ipv4)
                                            )
                                        )
                                    if central_client_info.ipv6 is not None:
                                        _log.info(
                                            "Updating client {:d} with new IPv6 {:s}".format(
                                                client_id, str(central_client_info.ipv6)
                                            )
                                        )
                                    database.update_client_addresses(
                                        client_id, ipv4=central_client_info.ipv4, ipv6=central_client_info.ipv6
                                    )

                            except central.ClientNotRegistered:
                                _log.warning(
                                    "Client with ID {:d} was not registered at the Central database".format(client_id)
                                )
                            except Exception:
                                custom_logging_callback(_log, logging.ERROR, *sys.exc_info())

            hosts_network_addresses = central.query_central_network_policies()
            database.update_volatile_information(
                ipv4_network=hosts_network_addresses.ipv4_network,
                ipv6_network=hosts_network_addresses.ipv6_network,
                ipv4_service=hosts_network_addresses.ipv4_service,
                ipv6_service=hosts_network_addresses.ipv6_service,
                mac_service=hosts_network_addresses.mac_service
            )

        except Exception as ex:
            custom_logging_callback(_log, logging.ERROR, *sys.exc_info())
            sys.exit(str(ex))

    @set_ev_cls(EventDP, MAIN_DISPATCHER)
    def switch_connect_event(self, ev):
        global _log

        try:
            if ev.enter:
                _log.info("Switch Connect Event: {}".format(str(ev)))
            else:
                _log.info("Switch Disconnect Event: {}".format(str(ev)))

        except Exception:
            custom_logging_callback(_log, logging.ERROR, *sys.exc_info())

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in_event(self, ev):
        global _log

        try:
            _log.info("Packet In Event: {}".format(str(ev)))
        except Exception:
            custom_logging_callback(_log, logging.ERROR, *sys.exc_info())

    @set_ev_cls(ofp_event.EventOFPPortStateChange, MAIN_DISPATCHER)
    def port_status_event(self, ev):
        global _log

        try:
            ofp_port_reason = {
                0: "The port was added",
                1: "The port was removed",
                2: "Some attribute of the port has changed"
            }

            if ev.reason in ofp_port_reason:
                _log.info(
                    "Port Status Event at Switch {:d} Port {:d} Reason: {:s}".format(
                        ev.datapath.id, ev.port_no, ofp_port_reason[ev.reason]
                    )
                )
            else:
                raise Exception("Reason with value {:d} is unknown to specification.".format(ev.reason))

        except Exception:
            custom_logging_callback(_log, logging.ERROR, *sys.exc_info())



